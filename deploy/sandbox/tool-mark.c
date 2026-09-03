// SPDX-License-Identifier: Apache-2.0
/* Passive Claude hook helper: one process, one bounded boundary mark. */

#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <poll.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/file.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

#include "tool-mark.h"

#define MAX_HOOK_PAYLOAD_BYTES (64U * 1024U)
#define MAX_LEGACY_MARK_BYTES (1024U * 1024U)
#define MAX_STDIN_WAIT_MS 25U
#define DEFAULT_MARK_PATH "/agent-state/tool-marks.log"

static bool id_byte(unsigned char value)
{
    return (value >= 'a' && value <= 'z') ||
           (value >= 'A' && value <= 'Z') ||
           (value >= '0' && value <= '9') || value == '_' || value == '-';
}

static bool json_space(unsigned char value)
{
    return value == ' ' || value == '\t' || value == '\r' || value == '\n';
}

static bool hex_byte(unsigned char value)
{
    return (value >= '0' && value <= '9') ||
           (value >= 'a' && value <= 'f') ||
           (value >= 'A' && value <= 'F');
}

static bool scan_json_string(const char *payload, size_t length, size_t *cursor,
                             size_t *content_start, size_t *content_length,
                             bool *escaped)
{
    size_t start;

    if (*cursor >= length || payload[*cursor] != '"')
        return false;
    *cursor += 1;
    start = *cursor;
    *escaped = false;
    while (*cursor < length) {
        unsigned char byte = (unsigned char)payload[*cursor];

        if (byte == '"') {
            *content_start = start;
            *content_length = *cursor - start;
            *cursor += 1;
            return true;
        }
        if (byte < 0x20)
            return false;
        if (byte != '\\') {
            *cursor += 1;
            continue;
        }
        *escaped = true;
        *cursor += 1;
        if (*cursor >= length)
            return false;
        byte = (unsigned char)payload[*cursor];
        if (byte == 'u') {
            if (length - *cursor <= 4)
                return false;
            for (size_t index = 1; index <= 4; ++index) {
                if (!hex_byte((unsigned char)payload[*cursor + index]))
                    return false;
            }
            *cursor += 5;
        } else if (strchr("\"\\/bfnrt", (int)byte) != NULL) {
            *cursor += 1;
        } else {
            return false;
        }
    }
    return false;
}

static bool extract_tool_use_id(const char *payload, size_t length,
                                char output[FI_TOOL_MARK_ID_BYTES])
{
    static const char key[] = "tool_use_id";
    char stack[64];
    size_t cursor = 0;
    size_t depth = 0;
    size_t matches = 0;
    bool found = false;

    memset(output, 0, FI_TOOL_MARK_ID_BYTES);
    while (cursor < length && json_space((unsigned char)payload[cursor]))
        cursor += 1;
    if (cursor >= length || payload[cursor] != '{')
        return false;
    stack[depth++] = '{';
    cursor += 1;

    while (cursor < length && depth > 0) {
        unsigned char byte = (unsigned char)payload[cursor];

        if (byte == '"') {
            size_t string_cursor = cursor;
            size_t content_start = 0;
            size_t content_length = 0;
            bool escaped = false;

            if (!scan_json_string(payload, length, &string_cursor,
                                  &content_start, &content_length, &escaped))
                return false;
            size_t after = string_cursor;
            while (after < length && json_space((unsigned char)payload[after]))
                after += 1;
            if (depth == 1 && !escaped && content_length == sizeof(key) - 1 &&
                memcmp(payload + content_start, key, sizeof(key) - 1) == 0 &&
                after < length && payload[after] == ':') {
                size_t value_cursor = after + 1;
                size_t value_start = 0;
                size_t value_length = 0;
                bool value_escaped = false;

                matches += 1;
                if (matches != 1)
                    return false;
                while (value_cursor < length &&
                       json_space((unsigned char)payload[value_cursor]))
                    value_cursor += 1;
                if (!scan_json_string(payload, length, &value_cursor,
                                      &value_start, &value_length, &value_escaped) ||
                    value_escaped || value_length == 0 ||
                    value_length >= FI_TOOL_MARK_ID_BYTES)
                    return false;
                for (size_t index = 0; index < value_length; ++index) {
                    if (!id_byte((unsigned char)payload[value_start + index]))
                        return false;
                }
                memcpy(output, payload + value_start, value_length);
                output[value_length] = '\0';
                found = true;
                cursor = value_cursor;
                continue;
            }
            cursor = string_cursor;
            continue;
        }
        if (byte == '{' || byte == '[') {
            if (depth >= sizeof(stack))
                return false;
            stack[depth++] = (char)byte;
        } else if (byte == '}' || byte == ']') {
            char expected = byte == '}' ? '{' : '[';

            if (depth == 0 || stack[depth - 1] != expected)
                return false;
            depth -= 1;
        }
        cursor += 1;
    }
    if (depth != 0)
        return false;
    while (cursor < length && json_space((unsigned char)payload[cursor]))
        cursor += 1;
    return found && cursor == length;
}

static uint64_t timespec_ns(const struct timespec *value)
{
    return (uint64_t)value->tv_sec * 1000000000ULL + (uint64_t)value->tv_nsec;
}

static size_t read_payload(char payload[MAX_HOOK_PAYLOAD_BYTES + 1])
{
    size_t used = 0;
    struct timespec started;
    uint64_t started_ns;
    uint64_t deadline_ns;
    int flags = fcntl(STDIN_FILENO, F_GETFL, 0);

    /* Claude normally writes the complete JSON document before this process
     * gets scheduled, so the fast path below never waits. A broken producer
     * that leaves the pipe open must cost exact-boundary coverage, not hold a
     * tool call until Claude's outer hook timeout. */
    if (flags < 0 || fcntl(STDIN_FILENO, F_SETFL, flags | O_NONBLOCK) < 0 ||
        clock_gettime(CLOCK_MONOTONIC, &started) < 0)
        return MAX_HOOK_PAYLOAD_BYTES + 1;
    started_ns = timespec_ns(&started);
    deadline_ns = started_ns + (uint64_t)MAX_STDIN_WAIT_MS * 1000000ULL;

    while (used < MAX_HOOK_PAYLOAD_BYTES + 1) {
        struct timespec now;
        uint64_t now_ns;
        ssize_t result = read(STDIN_FILENO, payload + used,
                              MAX_HOOK_PAYLOAD_BYTES + 1 - used);

        if (clock_gettime(CLOCK_MONOTONIC, &now) < 0)
            return MAX_HOOK_PAYLOAD_BYTES + 1;
        now_ns = timespec_ns(&now);
        if (now_ns >= deadline_ns)
            return MAX_HOOK_PAYLOAD_BYTES + 1;
        if (result > 0) {
            used += (size_t)result;
            continue;
        }
        if (result < 0 && errno == EINTR)
            continue;
        if (result < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
            struct pollfd waiter = {.fd = STDIN_FILENO, .events = POLLIN | POLLHUP};
            uint64_t remaining_ns;
            int wait_ms;
            int polled;

            remaining_ns = deadline_ns - now_ns;
            wait_ms = (int)((remaining_ns + 999999ULL) / 1000000ULL);
            polled = poll(&waiter, 1, wait_ms);
            if (polled < 0 && errno == EINTR)
                continue;
            if (polled <= 0 ||
                (waiter.revents & (POLLIN | POLLHUP)) == 0)
                return MAX_HOOK_PAYLOAD_BYTES + 1;
            continue;
        }
        if (result < 0)
            return MAX_HOOK_PAYLOAD_BYTES + 1;
        break;
    }
    return used;
}

static bool bounded_stdin_self_test(void)
{
    char payload[MAX_HOOK_PAYLOAD_BYTES + 1];
    struct timespec started;
    struct timespec finished;
    int pipes[2];
    size_t result;
    uint64_t elapsed_ms;

    if (pipe2(pipes, O_CLOEXEC) < 0 ||
        dup2(pipes[0], STDIN_FILENO) < 0) {
        return false;
    }
    close(pipes[0]);
    /* Keep the write end open and empty: a blocking reader never sees EOF. */
    if (clock_gettime(CLOCK_MONOTONIC, &started) < 0) {
        close(pipes[1]);
        return false;
    }
    result = read_payload(payload);
    if (clock_gettime(CLOCK_MONOTONIC, &finished) < 0) {
        close(pipes[1]);
        return false;
    }
    close(pipes[1]);
    elapsed_ms = (timespec_ns(&finished) - timespec_ns(&started)) / 1000000ULL;
    return result > MAX_HOOK_PAYLOAD_BYTES && elapsed_ms <= 250;
}

static bool send_trusted_mark(const struct fi_tool_mark *mark)
{
    struct sockaddr_un server = {.sun_family = AF_UNIX};
    struct sockaddr_un client = {.sun_family = AF_UNIX};
    struct pollfd waiter;
    int fd = socket(AF_UNIX, SOCK_DGRAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    char acknowledged = 0;
    int client_name_length;
    int polled;
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
                    sizeof(FI_TOOL_MARK_SOCKET)) < 0)
        goto done;
    sent = send(fd, mark, sizeof(*mark), MSG_NOSIGNAL);
    if (sent != (ssize_t)sizeof(*mark))
        goto done;
    waiter.fd = fd;
    waiter.events = POLLIN;
    waiter.revents = 0;
    polled = poll(&waiter, 1, 10);
    if (polled > 0 && (waiter.revents & POLLIN) != 0 &&
        recv(fd, &acknowledged, 1, MSG_DONTWAIT) == 1 && acknowledged == 1) {
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
    bool observer_socket_present;
    size_t payload_size;

    if (argc == 2 && strcmp(argv[1], "--self-test") == 0) {
        static const char sample[] =
            "{\"tool_input\":\"\\\"tool_use_id\\\":\\\"fake\\\"\","
            "\"tool_use_id\":\"toolu_self_test\"}";
        static const char nested_after[] =
            "{\"tool_use_id\":\"top_level\","
            "\"tool_input\":{\"tool_use_id\":\"nested_fake\"}}";
        static const char nested_before[] =
            "{\"tool_input\":{\"tool_use_id\":\"nested_fake\"},"
            "\"tool_use_id\":\"top_level\"}";
        static const char duplicate[] =
            "{\"tool_use_id\":\"one\",\"tool_use_id\":\"two\"}";
        char parsed[FI_TOOL_MARK_ID_BYTES] = {0};

        if (!extract_tool_use_id(sample, sizeof(sample) - 1, parsed) ||
            strcmp(parsed, "toolu_self_test") != 0 ||
            !extract_tool_use_id(nested_after, sizeof(nested_after) - 1, parsed) ||
            strcmp(parsed, "top_level") != 0 ||
            !extract_tool_use_id(nested_before, sizeof(nested_before) - 1, parsed) ||
            strcmp(parsed, "top_level") != 0 ||
            extract_tool_use_id(duplicate, sizeof(duplicate) - 1, parsed))
            return 1;
        if (!bounded_stdin_self_test())
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
    /* Kata ignores the agent-writable compatibility file. If its trusted
     * observer socket exists but is backpressured, fail open immediately
     * instead of adding a filesystem write that cannot recover precision. */
    observer_socket_present = access(FI_TOOL_MARK_SOCKET, F_OK) == 0;
    if (!send_trusted_mark(&mark) && !observer_socket_present)
        write_legacy_mark(legacy_path, phase, mark.tool_use_id, &wall);
    return 0;
}
