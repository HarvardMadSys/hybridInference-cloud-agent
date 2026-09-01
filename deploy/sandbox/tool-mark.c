// SPDX-License-Identifier: Apache-2.0
/* Passive Claude hook helper: one process, one bounded boundary mark. */

#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/file.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

#include "tool-mark.h"

#define MAX_HOOK_PAYLOAD_BYTES (64U * 1024U)
#define MAX_LEGACY_MARK_BYTES (1024U * 1024U)
#define DEFAULT_MARK_PATH "/agent-state/tool-marks.log"

static bool id_byte(unsigned char value)
{
    return (value >= 'a' && value <= 'z') ||
           (value >= 'A' && value <= 'Z') ||
           (value >= '0' && value <= '9') || value == '_' || value == '-';
}

static bool extract_tool_use_id(const char *payload, size_t length,
                                char output[FI_TOOL_MARK_ID_BYTES])
{
    static const char key[] = "\"tool_use_id\"";
    bool found = false;

    for (size_t start = 0; start + sizeof(key) - 1 < length; ++start) {
        char candidate[FI_TOOL_MARK_ID_BYTES] = {0};
        size_t cursor;
        size_t used = 0;

        if (memcmp(payload + start, key, sizeof(key) - 1) != 0)
            continue;
        cursor = start + sizeof(key) - 1;
        while (cursor < length &&
               (payload[cursor] == ' ' || payload[cursor] == '\t' ||
                payload[cursor] == '\r' || payload[cursor] == '\n'))
            cursor += 1;
        if (cursor >= length || payload[cursor++] != ':')
            continue;
        while (cursor < length &&
               (payload[cursor] == ' ' || payload[cursor] == '\t' ||
                payload[cursor] == '\r' || payload[cursor] == '\n'))
            cursor += 1;
        if (cursor >= length || payload[cursor++] != '"')
            continue;
        while (cursor < length && id_byte((unsigned char)payload[cursor]) &&
               used < FI_TOOL_MARK_ID_BYTES - 1) {
            candidate[used++] = payload[cursor++];
        }
        if (used == 0 || cursor >= length || payload[cursor] != '"')
            continue;
        candidate[used] = '\0';
        memcpy(output, candidate, used + 1);
        found = true;
        start = cursor;
    }
    return found;
}

static size_t read_payload(char payload[MAX_HOOK_PAYLOAD_BYTES + 1])
{
    size_t used = 0;

    while (used < MAX_HOOK_PAYLOAD_BYTES + 1) {
        ssize_t result = read(STDIN_FILENO, payload + used,
                              MAX_HOOK_PAYLOAD_BYTES + 1 - used);

        if (result > 0) {
            used += (size_t)result;
            continue;
        }
        if (result < 0 && errno == EINTR)
            continue;
        if (result < 0)
            return MAX_HOOK_PAYLOAD_BYTES + 1;
        break;
    }
    return used;
}

static bool send_trusted_mark(const struct fi_tool_mark *mark)
{
    struct sockaddr_un server = {.sun_family = AF_UNIX};
    struct sockaddr_un client = {.sun_family = AF_UNIX};
    struct timeval timeout = {.tv_sec = 0, .tv_usec = 10000};
    int fd = socket(AF_UNIX, SOCK_DGRAM | SOCK_CLOEXEC, 0);
    char acknowledged = 0;
    int client_name_length;
    ssize_t sent;

    if (fd < 0)
        return false;
    client.sun_path[0] = '\0';
    client_name_length = snprintf(client.sun_path + 1, sizeof(client.sun_path) - 1,
                                  "fi-tool-mark-%ld-%" PRIu64, (long)getpid(),
                                  mark->mono_ns);
    if (client_name_length <= 0 ||
        (size_t)client_name_length >= sizeof(client.sun_path) - 1 ||
        bind(fd, (const struct sockaddr *)&client,
             offsetof(struct sockaddr_un, sun_path) + 1 +
                 (size_t)client_name_length) < 0)
        goto done;
    memcpy(server.sun_path, FI_TOOL_MARK_SOCKET, sizeof(FI_TOOL_MARK_SOCKET));
    if (connect(fd, (const struct sockaddr *)&server,
                offsetof(struct sockaddr_un, sun_path) +
                    sizeof(FI_TOOL_MARK_SOCKET)) < 0 ||
        setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout)) < 0)
        goto done;
    sent = send(fd, mark, sizeof(*mark), MSG_NOSIGNAL);
    if (sent == (ssize_t)sizeof(*mark) && recv(fd, &acknowledged, 1, 0) == 1 &&
        acknowledged == 1) {
        close(fd);
        return true;
    }

done:
    close(fd);
    return false;
}

static int write_all(int fd, const char *data, size_t length)
{
    size_t written = 0;

    while (written < length) {
        ssize_t result = write(fd, data + written, length - written);

        if (result > 0) {
            written += (size_t)result;
            continue;
        }
        if (result < 0 && errno == EINTR)
            continue;
        return -1;
    }
    return 0;
}

static void write_legacy_mark(const char *path, const char *phase,
                              const char *tool_use_id,
                              const struct timespec *wall)
{
    char line[512];
    int length = snprintf(line, sizeof(line), "%" PRIu64 ".%09ld %s %s\n",
                          (uint64_t)wall->tv_sec, wall->tv_nsec, phase,
                          tool_use_id);
    int fd;
    struct stat status;

    if (length <= 0 || (size_t)length >= sizeof(line))
        return;
    umask(0007);
    fd = open(path, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC | O_NOFOLLOW |
                        O_NONBLOCK,
              0660);
    if (fd < 0)
        return;
    if (flock(fd, LOCK_EX | LOCK_NB) < 0 || fstat(fd, &status) < 0 ||
        !S_ISREG(status.st_mode) || status.st_size < 0 ||
        (uint64_t)status.st_size + (uint64_t)length > MAX_LEGACY_MARK_BYTES) {
        (void)close(fd);
        return;
    }
    (void)write_all(fd, line, (size_t)length);
    (void)close(fd);
}

int main(int argc, char **argv)
{
    char payload[MAX_HOOK_PAYLOAD_BYTES + 1];
    struct fi_tool_mark mark = {.version = FI_TOOL_MARK_VERSION};
    struct timespec monotonic = {0};
    struct timespec wall = {0};
    const char *phase;
    const char *legacy_path;
    size_t payload_size;

    if (argc == 2 && strcmp(argv[1], "--self-test") == 0) {
        static const char sample[] =
            "{\"tool_input\":\"\\\"tool_use_id\\\":\\\"fake\\\"\","
            "\"tool_use_id\":\"toolu_self_test\"}";
        char parsed[FI_TOOL_MARK_ID_BYTES] = {0};

        if (!extract_tool_use_id(sample, sizeof(sample) - 1, parsed) ||
            strcmp(parsed, "toolu_self_test") != 0)
            return 1;
        printf("freeinference-tool-mark-v%d\n", FI_TOOL_MARK_VERSION);
        return 0;
    }

    /* A marker must never block or fail the tool call. Invalid invocation,
     * input, clocks, or output therefore all deliberately return success. */
    if (argc != 3)
        return 0;
    phase = argv[1];
    legacy_path = argv[2];
    if (strcmp(phase, "pre") == 0)
        mark.phase = FI_TOOL_MARK_PRE;
    else if (strcmp(phase, "post") == 0)
        mark.phase = FI_TOOL_MARK_POST;
    else
        return 0;
    if (strcmp(legacy_path, DEFAULT_MARK_PATH) != 0)
        return 0;
    if (clock_gettime(CLOCK_MONOTONIC, &monotonic) < 0 ||
        clock_gettime(CLOCK_REALTIME, &wall) < 0)
        return 0;
    mark.mono_ns = (uint64_t)monotonic.tv_sec * 1000000000ULL +
                   (uint64_t)monotonic.tv_nsec;
    payload_size = read_payload(payload);
    if (payload_size > MAX_HOOK_PAYLOAD_BYTES ||
        !extract_tool_use_id(payload, payload_size, mark.tool_use_id))
        return 0;
    if (!send_trusted_mark(&mark))
        write_legacy_mark(legacy_path, phase, mark.tool_use_id, &wall);
    return 0;
}
