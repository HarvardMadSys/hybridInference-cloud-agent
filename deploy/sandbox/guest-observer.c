// SPDX-License-Identifier: Apache-2.0
/* Trusted libbpf loader and bounded JSONL journal for the Kata guest. */

#define _GNU_SOURCE

#include <bpf/bpf.h>
#include <bpf/libbpf.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <poll.h>
#include <signal.h>
#include <stdbool.h>
#include <stdarg.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

#include "guest-observer.h"
#include "tool-mark.h"

#define OBSERVER_ROOT "/run/freeinference-observer"
#define EVENTS_PATH OBSERVER_ROOT "/events.jsonl"
#define STATUS_PATH OBSERVER_ROOT "/status.json"
#define BPF_OBJECT_PATH "/usr/local/libexec/freeinference-guest-observer.bpf.o"
#define MAX_JOURNAL_BYTES (64ULL * 1024ULL * 1024ULL)
#define MAX_LINKS 8
#define MAX_TOOL_MARKS_PER_POLL 1024

static volatile sig_atomic_t exiting;

struct observer {
    int events_fd;
    int lost_map_fd;
    int tool_mark_fd;
    uint64_t event_seq;
    uint64_t poll_seq;
    uint64_t journal_bytes;
    uint64_t journal_dev;
    uint64_t journal_ino;
    /* Process loss determines whether the eBPF census is complete. Malformed
     * or backpressured optional tool marks must not let an untrusted sender
     * relabel an otherwise complete process census as kernel-event loss. */
    uint64_t journal_dropped;
    uint64_t tool_mark_dropped;
};

static void handle_signal(int signo)
{
    (void)signo;
    exiting = 1;
}

static int quiet_libbpf(enum libbpf_print_level level, const char *format, va_list args)
{
    if (level == LIBBPF_DEBUG)
        return 0;
    return vfprintf(stderr, format, args);
}

static int raise_memlock(void)
{
    const struct rlimit limit = {RLIM_INFINITY, RLIM_INFINITY};

    if (setrlimit(RLIMIT_MEMLOCK, &limit) == 0 || errno == EPERM)
        return 0;
    perror("setrlimit(RLIMIT_MEMLOCK)");
    return -1;
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

static size_t json_escape(char *output, size_t capacity, const char *input, size_t input_limit)
{
    static const char hex[] = "0123456789abcdef";
    size_t used = 0;

    for (size_t index = 0; index < input_limit && input[index] != '\0'; ++index) {
        unsigned char byte = (unsigned char)input[index];
        const char *short_escape = NULL;

        if (byte == '"')
            short_escape = "\\\"";
        else if (byte == '\\')
            short_escape = "\\\\";
        else if (byte == '\n')
            short_escape = "\\n";
        else if (byte == '\r')
            short_escape = "\\r";
        else if (byte == '\t')
            short_escape = "\\t";

        if (short_escape) {
            if (used + 2 >= capacity)
                break;
            output[used++] = short_escape[0];
            output[used++] = short_escape[1];
        } else if (byte < 0x20 || byte >= 0x7f) {
            /* Linux names are byte strings, not guaranteed UTF-8.  Escaping
             * every non-ASCII byte keeps one hostile filename from making a
             * valid lifecycle event disappear during JSON decoding. */
            if (used + 6 >= capacity)
                break;
            output[used++] = '\\';
            output[used++] = 'u';
            output[used++] = '0';
            output[used++] = '0';
            output[used++] = hex[byte >> 4];
            output[used++] = hex[byte & 0x0f];
        } else {
            if (used + 1 >= capacity)
                break;
            output[used++] = (char)byte;
        }
    }
    output[used] = '\0';
    return used;
}

static const char *event_kind(uint8_t kind)
{
    switch (kind) {
    case FI_PROCESS_FORK:
        return "fork";
    case FI_PROCESS_EXEC:
        return "exec";
    case FI_PROCESS_EXIT:
        return "exit";
    default:
        return NULL;
    }
}

static int journal_event(void *context, void *payload, size_t payload_size)
{
    struct observer *observer = context;
    const struct fi_process_event *event = payload;
    char comm[FI_TASK_COMM_LEN * 6 + 1];
    char filename[FI_EXECUTABLE_LEN * 6 + 1];
    char line[2048];
    const char *kind;
    int length;

    if (payload_size != sizeof(*event)) {
        observer->journal_dropped += 1;
        return 0;
    }
    kind = event_kind(event->kind);
    if (!kind) {
        observer->journal_dropped += 1;
        return 0;
    }
    json_escape(comm, sizeof(comm), event->comm, sizeof(event->comm));
    json_escape(filename, sizeof(filename), event->filename, sizeof(event->filename));
    observer->event_seq += 1;
    length = snprintf(
        line,
        sizeof(line),
        "{\"version\":%d,\"seq\":%" PRIu64 ",\"kind\":\"%s\","
        "\"mono_ns\":%" PRIu64 ",\"uid\":%u,\"pid\":%u,\"tgid\":%u,"
        "\"ppid\":%u,\"comm\":\"%s\",\"filename\":\"%s\","
        "\"exit_code\":%d}\n",
        FI_OBSERVER_VERSION,
        observer->event_seq,
        kind,
        (uint64_t)event->mono_ns,
        event->uid,
        event->pid,
        event->tgid,
        event->ppid,
        comm,
        filename,
        event->exit_code);
    if (length <= 0 || (size_t)length >= sizeof(line) ||
        observer->journal_bytes + (uint64_t)length > MAX_JOURNAL_BYTES) {
        observer->journal_dropped += 1;
        return 0;
    }
    if (write_all(observer->events_fd, line, (size_t)length) < 0) {
        observer->journal_dropped += 1;
        return 0;
    }
    observer->journal_bytes += (uint64_t)length;
    return 0;
}

static bool tool_mark_id_valid(const char tool_use_id[FI_TOOL_MARK_ID_BYTES])
{
    size_t length = strnlen(tool_use_id, FI_TOOL_MARK_ID_BYTES);

    if (length == 0 || length >= FI_TOOL_MARK_ID_BYTES)
        return false;
    for (size_t index = 0; index < length; ++index) {
        unsigned char byte = (unsigned char)tool_use_id[index];

        if (!((byte >= 'a' && byte <= 'z') || (byte >= 'A' && byte <= 'Z') ||
              (byte >= '0' && byte <= '9') || byte == '_' || byte == '-'))
            return false;
    }
    return true;
}

static bool trusted_tool_mark_sender(pid_t pid)
{
    char executable[64];
    struct stat expected;
    struct stat actual;
    int length;

    if (pid <= 1 || stat(FI_TOOL_MARK_HELPER, &expected) < 0)
        return false;
    length = snprintf(executable, sizeof(executable), "/proc/%ld/exe", (long)pid);
    if (length <= 0 || (size_t)length >= sizeof(executable) ||
        stat(executable, &actual) < 0)
        return false;
    return expected.st_dev == actual.st_dev && expected.st_ino == actual.st_ino;
}

static int journal_tool_mark(struct observer *observer,
                             const struct fi_tool_mark *mark,
                             const struct ucred *credential)
{
    const char *kind = mark->phase == FI_TOOL_MARK_PRE ? "tool_pre" : "tool_post";
    char line[512];
    int length;

    observer->event_seq += 1;
    length = snprintf(
        line,
        sizeof(line),
        "{\"version\":%d,\"seq\":%" PRIu64 ",\"kind\":\"%s\","
        "\"mono_ns\":%" PRIu64 ",\"uid\":%u,\"pid\":%ld,"
        "\"tool_use_id\":\"%s\"}\n",
        FI_OBSERVER_VERSION,
        observer->event_seq,
        kind,
        mark->mono_ns,
        (unsigned int)credential->uid,
        (long)credential->pid,
        mark->tool_use_id);
    if (length <= 0 || (size_t)length >= sizeof(line) ||
        observer->journal_bytes + (uint64_t)length > MAX_JOURNAL_BYTES)
        return -1;
    if (write_all(observer->events_fd, line, (size_t)length) < 0)
        return -1;
    observer->journal_bytes += (uint64_t)length;
    return 0;
}

static int open_tool_mark_socket(void)
{
    struct sockaddr_un address = {.sun_family = AF_UNIX};
    int enabled = 1;
    int fd = socket(AF_UNIX, SOCK_DGRAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);

    if (fd < 0)
        return -1;
    if (setsockopt(fd, SOL_SOCKET, SO_PASSCRED, &enabled, sizeof(enabled)) < 0)
        goto error;
    memcpy(address.sun_path, FI_TOOL_MARK_SOCKET, sizeof(FI_TOOL_MARK_SOCKET));
    if (unlink(FI_TOOL_MARK_SOCKET) < 0 && errno != ENOENT)
        goto error;
    if (bind(fd, (const struct sockaddr *)&address,
             offsetof(struct sockaddr_un, sun_path) + sizeof(FI_TOOL_MARK_SOCKET)) < 0)
        goto error;
    if (chmod(FI_TOOL_MARK_SOCKET, 0222) < 0)
        goto error;
    return fd;

error:
    close(fd);
    unlink(FI_TOOL_MARK_SOCKET);
    return -1;
}

static void drain_tool_marks(struct observer *observer)
{
    for (unsigned int count = 0; count < MAX_TOOL_MARKS_PER_POLL; ++count) {
        struct fi_tool_mark mark = {0};
        struct iovec iov = {.iov_base = &mark, .iov_len = sizeof(mark)};
        struct sockaddr_un peer = {0};
        char control[CMSG_SPACE(sizeof(struct ucred))] = {0};
        struct msghdr message = {
            .msg_name = &peer,
            .msg_namelen = sizeof(peer),
            .msg_iov = &iov,
            .msg_iovlen = 1,
            .msg_control = control,
            .msg_controllen = sizeof(control),
        };
        struct ucred *credential = NULL;
        ssize_t received = recvmsg(observer->tool_mark_fd, &message, MSG_DONTWAIT);

        if (received < 0 && errno == EINTR) {
            count -= count > 0 ? 1U : 0U;
            continue;
        }
        if (received < 0 && (errno == EAGAIN || errno == EWOULDBLOCK))
            return;
        if (received < 0)
            return;
        for (struct cmsghdr *header = CMSG_FIRSTHDR(&message); header != NULL;
             header = CMSG_NXTHDR(&message, header)) {
            if (header->cmsg_level == SOL_SOCKET && header->cmsg_type == SCM_CREDENTIALS &&
                header->cmsg_len >= CMSG_LEN(sizeof(struct ucred))) {
                credential = (struct ucred *)CMSG_DATA(header);
                break;
            }
        }
        if (received != (ssize_t)sizeof(mark) || (message.msg_flags & MSG_TRUNC) != 0 ||
            message.msg_namelen == 0 ||
            credential == NULL || mark.version != FI_TOOL_MARK_VERSION ||
            (mark.phase != FI_TOOL_MARK_PRE && mark.phase != FI_TOOL_MARK_POST) ||
            mark.mono_ns == 0 || !tool_mark_id_valid(mark.tool_use_id) ||
            !trusted_tool_mark_sender(credential->pid)) {
            observer->tool_mark_dropped += 1;
            continue;
        }
        char acknowledged = 1;
        if (journal_tool_mark(observer, &mark, credential) < 0) {
            observer->tool_mark_dropped += 1;
            acknowledged = 2;
        }
        (void)sendto(observer->tool_mark_fd, &acknowledged, 1, MSG_NOSIGNAL,
                     (const struct sockaddr *)&peer, message.msg_namelen);
    }
}

static uint64_t lost_total(int map_fd)
{
    int cpus = libbpf_num_possible_cpus();
    uint64_t total = 0;
    uint64_t *values;
    uint32_t key = 0;

    if (cpus <= 0)
        return UINT64_MAX;
    values = calloc((size_t)cpus, sizeof(*values));
    if (!values)
        return UINT64_MAX;
    if (bpf_map_lookup_elem(map_fd, &key, values) < 0) {
        free(values);
        return UINT64_MAX;
    }
    for (int cpu = 0; cpu < cpus; ++cpu)
        total += values[cpu];
    free(values);
    return total;
}

static int publish_status(const struct observer *observer)
{
    char temporary[256];
    char status[512];
    uint64_t lost = lost_total(observer->lost_map_fd);
    int fd;
    int length;
    int write_result;
    int close_result;

    if (lost == UINT64_MAX)
        return -1;
    length = snprintf(
        status,
        sizeof(status),
        "{\"version\":%d,\"ready\":true,\"pid\":%ld,\"poll_seq\":%" PRIu64
        ",\"event_seq\":%" PRIu64 ",\"lost\":%" PRIu64
        ",\"journal_dropped\":%" PRIu64 ",\"tool_mark_dropped\":%" PRIu64
        ",\"journal_bytes\":%" PRIu64 ",\"journal_dev\":%" PRIu64
        ",\"journal_ino\":%" PRIu64 "}\n",
        FI_OBSERVER_VERSION,
        (long)getpid(),
        observer->poll_seq,
        observer->event_seq,
        lost,
        observer->journal_dropped,
        observer->tool_mark_dropped,
        observer->journal_bytes,
        observer->journal_dev,
        observer->journal_ino);
    if (length <= 0 || (size_t)length >= sizeof(status))
        return -1;
    snprintf(temporary, sizeof(temporary), "%s.tmp.%ld", STATUS_PATH, (long)getpid());
    fd = open(temporary, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0600);
    if (fd < 0)
        return -1;
    write_result = write_all(fd, status, (size_t)length);
    close_result = close(fd);
    if (write_result < 0 || close_result < 0) {
        int saved = errno;
        unlink(temporary);
        errno = saved;
        return -1;
    }
    if (rename(temporary, STATUS_PATH) < 0) {
        int saved = errno;
        unlink(temporary);
        errno = saved;
        return -1;
    }
    return 0;
}

static void destroy_links(struct bpf_link **links, size_t count)
{
    while (count > 0)
        bpf_link__destroy(links[--count]);
}

int main(int argc, char **argv)
{
    struct bpf_object *object = NULL;
    struct ring_buffer *ring = NULL;
    struct bpf_program *program;
    struct bpf_link *links[MAX_LINKS] = {0};
    struct observer observer = {.events_fd = -1, .lost_map_fd = -1, .tool_mark_fd = -1};
    size_t link_count = 0;
    bool self_test = argc == 2 && strcmp(argv[1], "--self-test") == 0;
    int result = 1;

    if (argc > 2 || (argc == 2 && !self_test)) {
        fprintf(stderr, "usage: %s [--self-test]\n", argv[0]);
        return 2;
    }
    libbpf_set_print(quiet_libbpf);
    if (raise_memlock() < 0)
        goto cleanup;
    object = bpf_object__open_file(BPF_OBJECT_PATH, NULL);
    if (libbpf_get_error(object)) {
        object = NULL;
        fprintf(stderr, "cannot open the guest observer BPF object\n");
        goto cleanup;
    }
    if (bpf_object__load(object) < 0) {
        fprintf(stderr, "cannot load guest eBPF (the Kata kernel needs BPF and BTF support)\n");
        goto cleanup;
    }
    bpf_object__for_each_program(program, object) {
        struct bpf_link *link;

        if (link_count >= MAX_LINKS) {
            fprintf(stderr, "guest observer contains too many programs\n");
            goto cleanup;
        }
        link = bpf_program__attach(program);
        if (libbpf_get_error(link)) {
            fprintf(stderr, "cannot attach guest process tracepoint %s\n",
                    bpf_program__name(program));
            goto cleanup;
        }
        links[link_count++] = link;
    }
    if (link_count != 3) {
        fprintf(stderr, "guest observer did not attach all lifecycle tracepoints\n");
        goto cleanup;
    }
    observer.lost_map_fd = bpf_object__find_map_fd_by_name(object, "lost_events");
    int ring_map_fd = bpf_object__find_map_fd_by_name(object, "process_events");
    if (observer.lost_map_fd < 0 || ring_map_fd < 0) {
        fprintf(stderr, "guest observer maps are missing\n");
        goto cleanup;
    }
    if (self_test) {
        printf("freeinference-guest-observer-v%d\n", FI_OBSERVER_VERSION);
        result = 0;
        goto cleanup;
    }
    if (mkdir(OBSERVER_ROOT, 0700) < 0 && errno != EEXIST) {
        perror("mkdir observer root");
        goto cleanup;
    }
    observer.events_fd = open(EVENTS_PATH, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0600);
    if (observer.events_fd < 0) {
        perror("open observer journal");
        goto cleanup;
    }
    struct stat journal;
    if (fstat(observer.events_fd, &journal) < 0 || journal.st_size < 0) {
        perror("stat observer journal");
        goto cleanup;
    }
    observer.journal_bytes = (uint64_t)journal.st_size;
    observer.journal_dev = (uint64_t)journal.st_dev;
    observer.journal_ino = (uint64_t)journal.st_ino;
    observer.tool_mark_fd = open_tool_mark_socket();
    if (observer.tool_mark_fd < 0) {
        perror("open tool mark socket");
        goto cleanup;
    }
    ring = ring_buffer__new(ring_map_fd, journal_event, &observer, NULL);
    if (!ring) {
        fprintf(stderr, "cannot open guest observer ring buffer\n");
        goto cleanup;
    }
    int ring_poll_fd = ring_buffer__epoll_fd(ring);
    if (ring_poll_fd < 0) {
        fprintf(stderr, "cannot poll guest observer ring buffer\n");
        goto cleanup;
    }
    signal(SIGINT, handle_signal);
    signal(SIGTERM, handle_signal);
    observer.poll_seq = 1;
    if (publish_status(&observer) < 0) {
        perror("publish guest observer readiness");
        goto cleanup;
    }
    while (!exiting) {
        struct pollfd waiters[] = {
            {.fd = ring_poll_fd, .events = POLLIN},
            {.fd = observer.tool_mark_fd, .events = POLLIN},
        };
        int polled = poll(waiters, 2, 50);

        if (polled < 0 && errno != EINTR) {
            perror("guest observer poll");
            goto cleanup;
        }
        if ((waiters[0].revents | waiters[1].revents) &
            (POLLERR | POLLHUP | POLLNVAL)) {
            fprintf(stderr, "guest observer poll descriptor failed\n");
            goto cleanup;
        }
        if ((waiters[1].revents & POLLIN) != 0)
            drain_tool_marks(&observer);
        if ((waiters[0].revents & POLLIN) != 0) {
            int consumed = ring_buffer__consume(ring);

            if (consumed < 0) {
                fprintf(stderr, "guest observer ring consume failed: %d\n", consumed);
                goto cleanup;
            }
        }
        observer.poll_seq += 1;
        if (publish_status(&observer) < 0) {
            perror("publish guest observer status");
            goto cleanup;
        }
    }
    result = 0;

cleanup:
    if (!self_test) {
        unlink(STATUS_PATH);
        unlink(FI_TOOL_MARK_SOCKET);
    }
    ring_buffer__free(ring);
    if (observer.events_fd >= 0)
        close(observer.events_fd);
    if (observer.tool_mark_fd >= 0)
        close(observer.tool_mark_fd);
    destroy_links(links, link_count);
    bpf_object__close(object);
    return result;
}
