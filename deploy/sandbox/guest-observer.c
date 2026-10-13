// SPDX-License-Identifier: Apache-2.0
/* Trusted libbpf loader and bounded JSONL journal for the Kata guest. */

#define _GNU_SOURCE

#include <bpf/bpf.h>
#include <bpf/libbpf.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <grp.h>
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
#include <sys/prctl.h>
#include <sys/resource.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#include "guest-observer.h"
#include "tool-mark.h"

/* The driver that asks for member windows is a different helper from the
 * tool marker, and only it may open or sample a window. */
#define WINDOW_HELPER_PATH FI_WINDOW_DRIVER

#define OBSERVER_ROOT "/run/freeinference-observer"
#define EVENTS_PATH OBSERVER_ROOT "/events.jsonl"
#define STATUS_PATH OBSERVER_ROOT "/status.json"
#define OBSERVER_BINARY_PATH "/usr/local/libexec/freeinference-guest-observer"
#define BPF_OBJECT_PATH "/usr/local/libexec/freeinference-guest-observer.bpf.o"
#define MAX_JOURNAL_BYTES (64ULL * 1024ULL * 1024ULL)
/* Tool marks come through a socket and a helper every member can use, so they
 * may fill the journal only this far; the rest is kept for the kernel's
 * process records. A flood of optional marks must never be what makes a
 * sibling's process census drop events. The host replaces an idle journal at
 * this same size (GUEST_OBSERVER_ROTATE_BYTES), so one that marks have filled
 * is replaced at the next idle turn rather than left refusing marks. */
#define MAX_TOOL_MARK_JOURNAL_BYTES (48ULL * 1024ULL * 1024ULL)
#define MAX_LINKS 8
#define MAX_TOOL_MARKS_PER_POLL 1024
#define SELF_TEST_TIMEOUT_MS 2000
#define SELF_TEST_UID 65534U

/* One census cycle may touch this many tasks before it declares itself
 * truncated. The bound exists so a fork storm makes the cycle say so instead
 * of running long enough to delay the agent it is measuring. */
#define MAX_WINDOW_TASKS 4096U
/* A cycle reads a bounded prefix of each proc file and opens it O_NONBLOCK.
 * A proc read that would block is reported as an unread counter rather than
 * waited on: this loop shares the observer's only thread with the eBPF drain,
 * so one wedged read would stall every lifecycle event behind it. */
#define WINDOW_STATUS_BYTES 4096U
#define WINDOW_IO_BYTES 1024U
#define WINDOW_SCHEDSTAT_BYTES 256U
#define UPROC_CHUNK_BYTES 8192U
#define MAX_WINDOW_ADDRESS_SPACES 4096U
/* One record is bounded, so a fork storm makes the cycle report itself
 * truncated instead of writing a line no reader could parse. */
#define MAX_WINDOW_LINE_BYTES 262144U
#define MAX_DENTRIES_PER_TASK 8192U

enum self_test_event_flag {
    SELF_TEST_FORK = 1U << 0,
    SELF_TEST_EXEC = 1U << 1,
    SELF_TEST_EXIT = 1U << 2,
    SELF_TEST_DONE = 1U << 3,
};

struct self_test_state {
    uint32_t target_pid;
    uint8_t events;
    char target_comm[FI_TASK_COMM_LEN];
    bool malformed;
    bool exec_mismatch;
    bool uid_mismatch;
    /* The records must carry the counters every Linux kernel has, with
     * values a child that exec'd and returned cannot have: a zero peak RSS
     * or a zero scheduler runtime means a BTF relocation silently read the
     * wrong thing, and a single-threaded child's exit must empty its group. */
    bool counters_missing;
    uint8_t counters;
};

static volatile sig_atomic_t exiting;

/* Guest page size, so maxrss leaves this process in bytes rather than in a
 * unit the host would have to guess. */
static uint64_t page_bytes = 4096;

/* One member window this guest is measuring right now. A member's own turn is
 * serialized by its turn lock and fork siblings have their own UIDs, so a
 * handful of concurrent entries is the expected shape rather than one. */
struct member_window {
    bool open;
    uint32_t uid;
    char measurement_id[FI_WINDOW_MEASUREMENT_ID_BYTES];
};

struct observer {
    int events_fd;
    int lost_map_fd;
    int tool_mark_fd;
    int window_fd;
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
    uint64_t window_dropped;
    struct member_window windows[FI_WINDOW_MAX_OPEN];
};

/* One task as the census read it. Kept outside the journal so a cycle can be
 * reduced before anything is written: the window total is what the host
 * needs, and a per-task record would be a per-second dump of every PID in the
 * guest. */
struct window_task {
    uint64_t cpu_ns;
    uint64_t read_bytes;
    uint64_t write_bytes;
    uint64_t starttime_ticks;
    uint64_t pss_bytes;
    uint32_t uid;
    uint32_t tid;
    uint32_t tgid;
    uint8_t counters;
    bool pss_counted;
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

/* Format into a fixed buffer, rejecting rather than silently shortening.
 *
 * Every path and record this helper builds is bounded on purpose, so a
 * conversion that does not fit is a bug in the bound and not a value to be
 * abbreviated: a truncated path names a different file and a truncated record
 * cannot be parsed back. Returning the length keeps the call sites explicit,
 * and -Wformat-truncation stays quiet because the failure is handled here
 * rather than left to chance.
 */
static int format_into(char *output, size_t capacity, const char *format, ...)
{
    va_list args;
    int written;

    va_start(args, format);
    written = vsnprintf(output, capacity, format, args);
    va_end(args);
    if (written < 0 || (size_t)written >= capacity) {
        if (capacity > 0)
            output[0] = '\0';
        return -1;
    }
    return written;
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
    case FI_PROCESS_DONE:
        return "done";
    default:
        return NULL;
    }
}

static int self_test_event(void *context, void *payload, size_t payload_size)
{
    struct self_test_state *state = context;
    const struct fi_process_event *event = payload;
    uint8_t flag;

    if (payload_size != sizeof(*event)) {
        state->malformed = true;
        return 0;
    }
    /* Thread lifecycle is intentionally outside the process census. The PID
     * returned by fork is namespaced while task_struct.pid is guest-global,
     * so the uniquely named fork event is the bridge between those views. */
    if (event->pid == 0 || event->pid != event->tgid)
        return 0;
    if (state->target_pid == 0) {
        if (event->kind != FI_PROCESS_FORK ||
            strncmp(event->comm, state->target_comm, sizeof(event->comm)) != 0)
            return 0;
        state->target_pid = event->pid;
    } else if (event->pid != state->target_pid) {
        return 0;
    }
    if (event->uid != SELF_TEST_UID)
        state->uid_mismatch = true;
    switch (event->kind) {
    case FI_PROCESS_FORK:
        flag = SELF_TEST_FORK;
        break;
    case FI_PROCESS_EXEC:
        if (strncmp(event->filename, OBSERVER_BINARY_PATH, sizeof(event->filename)) != 0)
            state->exec_mismatch = true;
        flag = SELF_TEST_EXEC;
        break;
    case FI_PROCESS_EXIT:
        state->counters |= event->counters;
        if ((event->counters & FI_COUNTER_MAXRSS) == 0 || event->maxrss_pages == 0 ||
            (event->counters & FI_COUNTER_GROUP) == 0 || !event->group_dead)
            state->counters_missing = true;
        flag = SELF_TEST_EXIT;
        break;
    case FI_PROCESS_DONE:
        /* The child runs for well under a millisecond. Tick-based CPU time
         * would read zero here; the scheduler runtime must not. */
        state->counters |= event->counters;
        if ((event->counters & FI_COUNTER_CPU) == 0 || event->cpu_ns == 0)
            state->counters_missing = true;
        flag = SELF_TEST_DONE;
        break;
    default:
        state->malformed = true;
        return 0;
    }
    state->events |= flag;
    return 0;
}

static bool self_test_complete(const struct self_test_state *state)
{
    const uint8_t expected =
        SELF_TEST_FORK | SELF_TEST_EXEC | SELF_TEST_EXIT | SELF_TEST_DONE;

    return state->target_pid != 0 && !state->malformed && !state->exec_mismatch &&
           !state->uid_mismatch && !state->counters_missing && state->events == expected;
}
static int monotonic_ms(uint64_t *value)
{
    struct timespec now;

    if (clock_gettime(CLOCK_MONOTONIC, &now) < 0)
        return -1;
    *value = (uint64_t)now.tv_sec * 1000ULL + (uint64_t)now.tv_nsec / 1000000ULL;
    return 0;
}

/* The guest's monotonic clock in nanoseconds.
 *
 * The records this helper writes share one time base, and it is this one:
 * the kernel stamps its lifecycle events from the same clock, so a census and
 * an exit can be ordered against each other and against the window's own
 * boundaries. Reading the BPF helper's clock here would work too, but this is
 * the loader's userspace clock and needs no kernel-side declaration. */
static int monotonic_ns(uint64_t *value)
{
    struct timespec now;

    if (clock_gettime(CLOCK_MONOTONIC, &now) < 0)
        return -1;
    *value = (uint64_t)now.tv_sec * 1000000000ULL + (uint64_t)now.tv_nsec;
    return 0;
}

static int exercise_lifecycle_events(int ring_map_fd, struct ring_buffer **ring_out)
{
    struct self_test_state state = {0};
    struct ring_buffer *ring;
    pid_t child;
    int child_status = 0;
    bool child_reaped = false;
    uint64_t started_ms;

    ring = ring_buffer__new(ring_map_fd, self_test_event, &state, NULL);
    if (!ring) {
        fprintf(stderr, "cannot open guest observer ring buffer during self-test\n");
        return -1;
    }
    *ring_out = ring;
    if (snprintf(state.target_comm, sizeof(state.target_comm), "fi-p-%08x",
                 (unsigned int)getpid()) <= 0 ||
        prctl(PR_SET_NAME, state.target_comm, 0, 0, 0) < 0) {
        perror("name guest observer self-test child");
        return -1;
    }
    /* Prove the UID filter agents rely on, not merely a privileged root
     * event path. The BPF object and links are already loaded; closing their
     * fds during cleanup does not require retaining privilege. */
    if (setgroups(0, NULL) < 0 || setgid((gid_t)SELF_TEST_UID) < 0 ||
        setuid((uid_t)SELF_TEST_UID) < 0) {
        perror("drop guest observer self-test privilege");
        return -1;
    }
    child = fork();
    if (child < 0) {
        perror("fork guest observer self-test child");
        return -1;
    }
    if (child == 0) {
        execl(OBSERVER_BINARY_PATH, OBSERVER_BINARY_PATH, "--self-test-child", NULL);
        _exit(127);
    }
    if (monotonic_ms(&started_ms) < 0) {
        perror("read guest observer self-test clock");
        goto child_cleanup;
    }

    while (true) {
        int consumed;
        uint64_t current_ms;

        consumed = ring_buffer__poll(ring, 50);
        if (consumed < 0 && consumed != -EINTR) {
            fprintf(stderr, "guest observer ring poll failed during self-test: %d\n", consumed);
            goto child_cleanup;
        }
        if (!child_reaped) {
            pid_t waited = waitpid(child, &child_status, WNOHANG);

            if (waited == child)
                child_reaped = true;
            else if (waited < 0 && errno != EINTR) {
                perror("wait for guest observer self-test child");
                goto child_cleanup;
            }
        }
        if (child_reaped && self_test_complete(&state))
            break;
        if (monotonic_ms(&current_ms) < 0) {
            perror("read guest observer self-test clock");
            goto child_cleanup;
        }
        if (current_ms - started_ms >= SELF_TEST_TIMEOUT_MS)
            break;
    }
    if (!child_reaped) {
        fprintf(stderr, "guest observer self-test child did not exit\n");
        goto child_cleanup;
    }
    if (!WIFEXITED(child_status) || WEXITSTATUS(child_status) != 0) {
        fprintf(stderr, "guest observer self-test child failed\n");
        return -1;
    }
    if (!self_test_complete(&state)) {
        if (state.counters_missing)
            fprintf(stderr,
                    "guest observer records lack settled CPU, peak-RSS or group-exit "
                    "accounting\n");
        else
            fprintf(stderr,
                    "guest observer did not deliver fork/exec/exit/done through the ring "
                    "buffer\n");
        return -1;
    }
    /* Storage and syscall I/O accounting are kernel configuration, not a
     * lifecycle guarantee; missing ones arrive as unavailable, so say so
     * here where an operator reads the preflight output. */
    if ((state.counters & FI_COUNTER_IO) == 0)
        fprintf(stderr, "guest kernel exposes no per-task storage I/O accounting\n");
    if ((state.counters & FI_COUNTER_XACCT) == 0)
        fprintf(stderr, "guest kernel exposes no per-task read/write byte accounting\n");
    return 0;

child_cleanup:
    if (!child_reaped) {
        (void)kill(child, SIGKILL);
        while (waitpid(child, &child_status, 0) < 0 && errno == EINTR)
            ;
    }
    return -1;
}

static int journal_event(void *context, void *payload, size_t payload_size)
{
    struct observer *observer = context;
    const struct fi_process_event *event = payload;
    char comm[FI_TASK_COMM_LEN * 6 + 1];
    char filename[FI_EXECUTABLE_LEN * 6 + 1];
    char counters[256];
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
    /* Exit and done records carry what the task consumed. Each counter is
     * written only when the kernel exposed it; the host treats an absent key
     * as unavailable, which is the honest reading of a kernel built without
     * that accounting, and different from a process that used nothing. */
    if (event->kind == FI_PROCESS_EXIT || event->kind == FI_PROCESS_DONE) {
        size_t used = 0;
        int written;

        written = snprintf(counters, sizeof(counters), ",\"counters\":%u", event->counters);
        if (written > 0 && (size_t)written < sizeof(counters))
            used = (size_t)written;
        if (event->counters & FI_COUNTER_GROUP) {
            written = snprintf(counters + used, sizeof(counters) - used,
                               ",\"group_dead\":%s", event->group_dead ? "true" : "false");
            if (written > 0 && (size_t)written < sizeof(counters) - used)
                used += (size_t)written;
        }
        if (event->counters & FI_COUNTER_CPU) {
            written = snprintf(counters + used, sizeof(counters) - used,
                               ",\"cpu_ns\":%" PRIu64, (uint64_t)event->cpu_ns);
            if (written > 0 && (size_t)written < sizeof(counters) - used)
                used += (size_t)written;
        }
        if (event->counters & FI_COUNTER_MAXRSS) {
            written = snprintf(counters + used, sizeof(counters) - used,
                               ",\"maxrss_bytes\":%" PRIu64,
                               (uint64_t)event->maxrss_pages * page_bytes);
            if (written > 0 && (size_t)written < sizeof(counters) - used)
                used += (size_t)written;
        }
        if (event->counters & FI_COUNTER_IO) {
            written = snprintf(counters + used, sizeof(counters) - used,
                               ",\"read_bytes\":%" PRIu64 ",\"write_bytes\":%" PRIu64,
                               (uint64_t)event->read_bytes, (uint64_t)event->write_bytes);
            if (written > 0 && (size_t)written < sizeof(counters) - used)
                used += (size_t)written;
        }
        if (event->counters & FI_COUNTER_XACCT) {
            written = snprintf(counters + used, sizeof(counters) - used,
                               ",\"rchar\":%" PRIu64 ",\"wchar\":%" PRIu64,
                               (uint64_t)event->rchar, (uint64_t)event->wchar);
            if (written > 0 && (size_t)written < sizeof(counters) - used)
                used += (size_t)written;
        }
        counters[used] = '\0';
    } else {
        counters[0] = '\0';
    }
    observer->event_seq += 1;
    length = snprintf(
        line,
        sizeof(line),
        "{\"version\":%d,\"seq\":%" PRIu64 ",\"kind\":\"%s\","
        "\"mono_ns\":%" PRIu64 ",\"uid\":%u,\"pid\":%u,\"tgid\":%u,"
        "\"ppid\":%u,\"comm\":\"%s\",\"filename\":\"%s\","
        "\"exit_code\":%d%s}\n",
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
        event->exit_code,
        counters);
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

/* A measurement identifier reaches the journal, so it is restricted to a
 * charset that cannot break out of the JSON string or the line: the host
 * generates it as a UUID, and anything else is refused rather than escaped. */
static bool measurement_id_valid(const char *measurement_id)
{
    size_t length = strnlen(measurement_id, FI_WINDOW_MEASUREMENT_ID_BYTES);

    if (length < 8 || length >= FI_WINDOW_MEASUREMENT_ID_BYTES)
        return false;
    for (size_t index = 0; index < length; ++index) {
        unsigned char byte = (unsigned char)measurement_id[index];

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
        observer->journal_bytes + (uint64_t)length > MAX_TOOL_MARK_JOURNAL_BYTES)
        return -1;
    if (write_all(observer->events_fd, line, (size_t)length) < 0)
        return -1;
    observer->journal_bytes += (uint64_t)length;
    return 0;
}

/*
 * Member execution windows.
 *
 * A member window is measured from the guest, per member UID, for exactly as
 * long as the platform says one turn is running. Two sources are combined and
 * neither replaces the other:
 *
 *   - the eBPF exit records above, which are the only way a thread that was
 *     born and died between two samples can be measured at all; and
 *   - the live census below, which reads every task the member still has
 *     running, so a long-lived agent process is counted while it works
 *     instead of only when it eventually dies.
 *
 * Both deliberately read the scheduler's own accumulated runtime: the census
 * takes field 0 of /proc/<tid>/schedstat, the exit record takes
 * se.sum_exec_runtime at TASK_DEAD. Those are the same number in the same
 * unit. Substituting tick-accounted utime/stime for the live side would make
 * the two halves of one window incomparable.
 *
 * The census reads proc files rather than walking tasks in BPF. It adds no
 * kernel capability to the image and keeps the eBPF program - and therefore
 * the byte layout the host already parses - untouched. What it must prove at
 * preflight is that the keys exist and that reading them as guest root
 * actually works, which is a property of the guest kernel rather than of this
 * code.
 */

/* Read a decimal field of a proc file into a bounded buffer.
 *
 * The file is opened O_NONBLOCK so a task that is exiting cannot park this
 * loop in an uninterruptible read, and read with pread so one attempt sees a
 * consistent position. An empty or short body is reported as unread: the
 * caller must never mistake a truncated read for a zero counter.
 */
static ssize_t read_bounded(const char *path, char *buffer, size_t capacity)
{
    size_t used = 0;
    int fd = open(path, O_RDONLY | O_NONBLOCK | O_CLOEXEC);

    if (fd < 0)
        return -1;
    while (used + 1 < capacity) {
        ssize_t got = pread(fd, buffer + used, capacity - 1 - used, (off_t)used);

        if (got > 0) {
            used += (size_t)got;
            continue;
        }
        if (got < 0 && errno == EINTR)
            continue;
        if (got < 0) {
            int saved = errno;

            close(fd);
            errno = saved;
            return -1;
        }
        break;
    }
    close(fd);
    buffer[used] = '\0';
    return (ssize_t)used;
}

static bool parse_u64(const char *text, uint64_t *value)
{
    char *end = NULL;
    unsigned long long parsed;

    if (text == NULL || *text == '\0')
        return false;
    errno = 0;
    parsed = strtoull(text, &end, 10);
    if (errno != 0 || end == text)
        return false;
    *value = (uint64_t)parsed;
    return true;
}

/* Field N of a whitespace-separated line, counting from zero. */
static bool field_at(const char *cursor, unsigned int index, uint64_t *value)
{
    for (unsigned int current = 0; current <= index; ++current) {
        while (*cursor == ' ' || *cursor == '\t' || *cursor == '\n')
            ++cursor;
        if (*cursor == '\0')
            return false;
        if (current == index) {
            char token[32];
            size_t length = 0;

            while (cursor[length] != '\0' && cursor[length] != ' ' && cursor[length] != '\t' &&
                   cursor[length] != '\n') {
                if (length + 1 >= sizeof(token))
                    return false;
                token[length] = cursor[length];
                ++length;
            }
            token[length] = '\0';
            return parse_u64(token, value);
        }
        while (*cursor != '\0' && *cursor != ' ' && *cursor != '\t' && *cursor != '\n')
            ++cursor;
    }
    return false;
}

static bool field_u64(const char *line, unsigned int index, uint64_t *value)
{
    return field_at(line, index, value);
}

/* The task's start time, which is what makes its identity stable.
 *
 * The second field of /proc/<tid>/stat is the command name wrapped in
 * parentheses, and a command name may contain spaces and parentheses. Counting
 * whitespace-separated fields from the beginning of the line therefore reads a
 * different column for a process called ``node worker`` than for one called
 * ``node``, which would hand the wrong start time to the identity key and let a
 * reused TID inherit a dead thread's baseline. The kernel guarantees the last
 * ')' on the line closes that field, so counting starts after it.
 */
static bool task_starttime_ticks(const char *stat_line, uint64_t *value)
{
    const char *open = strchr(stat_line, '(');
    const char *close;

    if (open == NULL)
        return false;
    close = strrchr(open, ')');
    if (close == NULL || close[1] != ' ')
        return false;
    /* Field 3 of the line follows the ')', so field 22 is index 19 from here. */
    return field_at(close + 1, 19, value);
}

/* The value after a ``Name:`` label, or false when the label is absent. */
static bool labelled_u64(const char *body, const char *label, uint64_t *value)
{
    size_t label_length = strlen(label);
    const char *cursor = body;

    while (cursor != NULL && *cursor != '\0') {
        if (strncmp(cursor, label, label_length) == 0) {
            const char *rest = cursor + label_length;

            while (*rest == ' ' || *rest == '\t')
                ++rest;
            return parse_u64(rest, value);
        }
        cursor = strchr(cursor, '\n');
        if (cursor != NULL)
            ++cursor;
    }
    return false;
}

/* A thread's own counters.
 *
 * schedstat is the scheduler's total for that task, in nanoseconds, and is
 * the same quantity the exit record carries. /proc/<tid>/io is the kernel's
 * task I/O accounting. Both are per task, not per group, which is what makes
 * a thread-level baseline meaningful at all.
 */
static void read_task_counters(uint32_t tid, struct window_task *task)
{
    char path[64];
    char body[WINDOW_SCHEDSTAT_BYTES];
    uint64_t first_line = 0;

    snprintf(path, sizeof(path), "/proc/%u/schedstat", tid);
    if (read_bounded(path, body, sizeof(body)) > 0) {
        const char *newline = strchr(body, '\n');
        char line[WINDOW_SCHEDSTAT_BYTES];
        size_t length = newline != NULL ? (size_t)(newline - body) : strlen(body);

        if (length < sizeof(line)) {
            memcpy(line, body, length);
            line[length] = '\0';
            if (field_u64(line, 0, &first_line)) {
                task->cpu_ns = first_line;
                task->counters |= FI_WINDOW_CPU;
            }
        }
    }
    /* The task-local entry, reached through the task's own subdirectory.
     * ``/proc/<tid>/io`` would hand back the whole thread group's counters, so
     * summing it once per thread would charge one write to every sibling. */
    snprintf(path, sizeof(path), "/proc/%u/task/%u/io", tid, tid);
    if (read_bounded(path, body, sizeof(body)) > 0) {
        uint64_t read_bytes = 0;
        uint64_t write_bytes = 0;

        if (labelled_u64(body, "read_bytes:", &read_bytes) &&
            labelled_u64(body, "write_bytes:", &write_bytes)) {
            task->read_bytes = read_bytes;
            task->write_bytes = write_bytes;
            task->counters |= FI_WINDOW_STORAGE_IO;
        }
    }
}

/* Whether this task's scheduler counter is still there to be read.
 *
 * Asked only after a counter read came back empty, to separate the two reasons
 * it can: the task exited while the census was walking it, or this kernel does
 * not expose the counter at all. The first is not a hole in the cycle -- the
 * task's exit record accounts for it -- and the second is, so they must not
 * both mark the cycle short.
 */
static bool task_counters_present(uint32_t tid)
{
    char path[64];

    snprintf(path, sizeof(path), "/proc/%u/schedstat", tid);
    return access(path, F_OK) == 0;
}

/* The thread group and start time of one task.
 *
 * Tgid is what makes PSS summing correct: every thread of a group shares one
 * address space, so a group is read once no matter how many threads it has.
 * starttime_ticks is the task's own start time, which is what stops a
 * recycled TID from inheriting the counters of a thread that has gone.
 */
static bool read_task_identity(uint32_t tid, struct window_task *task)
{
    char path[64];
    char body[WINDOW_STATUS_BYTES];
    uint64_t tgid = 0;
    uint64_t uid = 0;
    uint64_t starttime = 0;

    snprintf(path, sizeof(path), "/proc/%u/status", tid);
    if (read_bounded(path, body, sizeof(body)) <= 0)
        return false;
    if (!labelled_u64(body, "Tgid:", &tgid) || !labelled_u64(body, "Uid:", &uid))
        return false;
    /* Field 22 of stat, counted from one, is the task's start time in clock
     * ticks. It is read from the same task as the counters above, so a task
     * that exits mid-census fails one of the two reads rather than pairing
     * one task's identity with another's counters. */
    snprintf(path, sizeof(path), "/proc/%u/stat", tid);
    if (read_bounded(path, body, sizeof(body)) <= 0)
        return false;
    if (!task_starttime_ticks(body, &starttime))
        return false;
    if (tgid > UINT32_MAX || uid > UINT32_MAX)
        return false;
    task->tgid = (uint32_t)tgid;
    task->uid = (uint32_t)uid;
    task->starttime_ticks = starttime;
    return true;
}

/* Sum a process's proportional resident set from smaps_rollup.
 *
 * PSS is read rather than RSS because shared pages are charged in proportion
 * to their users instead of in full to each of them. The summary lines come
 * first in the file, so a bounded chunk is enough; a process whose summary
 * does not fit the chunk is reported as unread rather than as a small number,
 * and the cycle it belongs to stops being complete.
 */
struct pss_scan {
    uint64_t pss_bytes;
    bool found;
};

static bool scan_pss_line(const char *line, size_t length, struct pss_scan *scan)
{
    static const char label[] = "Pss:";
    char text[WINDOW_STATUS_BYTES];
    uint64_t kilobytes = 0;

    if (length <= sizeof(label) || strncmp(line, label, sizeof(label) - 1) != 0)
        return false;
    if (length + 1 > sizeof(text))
        return false;
    memcpy(text, line, length);
    text[length] = '\0';
    if (!parse_u64(text + sizeof(label) - 1, &kilobytes))
        return false;
    scan->pss_bytes = kilobytes * 1024ULL;
    scan->found = true;
    return true;
}

static bool read_address_space_pss(uint32_t tgid, uint64_t *pss_bytes)
{
    char path[64];
    char chunk[UPROC_CHUNK_BYTES];
    char pending[WINDOW_STATUS_BYTES];
    size_t pending_length = 0;
    struct pss_scan scan = {0, false};
    off_t offset = 0;
    int fd;

    snprintf(path, sizeof(path), "/proc/%u/smaps_rollup", tgid);
    fd = open(path, O_RDONLY | O_NONBLOCK | O_CLOEXEC);
    if (fd < 0)
        return false;
    while (offset < (off_t)(WINDOW_STATUS_BYTES * 4)) {
        ssize_t got = pread(fd, chunk, sizeof(chunk), offset);

        if (got < 0 && errno == EINTR)
            continue;
        if (got <= 0)
            break;
        offset += got;
        for (ssize_t index = 0; index < got; ++index) {
            if (chunk[index] != '\n') {
                if (pending_length + 1 < sizeof(pending))
                    pending[pending_length++] = chunk[index];
                continue;
            }
            (void)scan_pss_line(pending, pending_length, &scan);
            pending_length = 0;
            if (scan.found)
                break;
        }
        if (scan.found)
            break;
    }
    close(fd);
    if (!scan.found)
        return false;
    *pss_bytes = scan.pss_bytes;
    return true;
}

/* Burn a little CPU so the live scheduler counter has something to show. */
static uint64_t burn_cpu_once(uint64_t seed)
{
    for (unsigned int index = 0; index < 400000U; ++index)
        seed = seed * 6364136223846793005ULL + 1442695040888963407ULL;
    return seed;
}

static bool read_self_schedstat(uint64_t *cpu_ns)
{
    char body[WINDOW_SCHEDSTAT_BYTES];
    char line[WINDOW_SCHEDSTAT_BYTES];
    const char *newline;
    size_t length;

    if (read_bounded("/proc/self/schedstat", body, sizeof(body)) <= 0)
        return false;
    newline = strchr(body, '\n');
    length = newline != NULL ? (size_t)(newline - body) : strlen(body);
    if (length + 1 > sizeof(line))
        return false;
    memcpy(line, body, length);
    line[length] = '\0';
    return field_u64(line, 0, cpu_ns);
}

/* Does this guest actually support a member execution window?
 *
 * The three questions are separate because their answers are: a kernel can
 * expose the scheduler's per-task runtime and still refuse smaps_rollup, and
 * the platform has to report that rather than quietly fall back to RSS. Each
 * probe reads its own task, so a failure means the key or the permission is
 * missing rather than that some member happened to be idle. The CPU probe
 * burns a little work between two reads because a counter that never moves
 * proves nothing about which field was read.
 */
static uint8_t probe_window_capability(void)
{
    uint64_t before = 0;
    uint64_t after = 0;
    uint64_t ignored = 0;
    uint64_t pss = 0;
    char body[WINDOW_IO_BYTES];
    uint8_t capability = 0;

    if (read_self_schedstat(&before)) {
        uint64_t seed = burn_cpu_once(before);

        if (read_self_schedstat(&after) && after > before)
            capability |= FI_WINDOW_CPU;
        if (seed == 0)
            capability &= (uint8_t)~FI_WINDOW_CPU;
    }
    if (read_bounded("/proc/self/io", body, sizeof(body)) > 0 &&
        labelled_u64(body, "read_bytes:", &ignored) &&
        labelled_u64(body, "write_bytes:", &ignored))
        capability |= FI_WINDOW_STORAGE_IO;
    if (read_address_space_pss((uint32_t)getpid(), &pss))
        capability |= FI_WINDOW_PSS;
    return capability;
}

static struct member_window *find_window(struct observer *observer, const char *id){
    for (size_t index = 0; index < FI_WINDOW_MAX_OPEN; ++index) {
        if (observer->windows[index].open &&
            strncmp(observer->windows[index].measurement_id, id,
                    FI_WINDOW_MEASUREMENT_ID_BYTES) == 0)
            return &observer->windows[index];
    }
    return NULL;
}

static struct member_window *open_window(struct observer *observer,
                                                   uint32_t uid,
                                                   const char *id)
{
    for (size_t index = 0; index < FI_WINDOW_MAX_OPEN; ++index) {
        if (!observer->windows[index].open) {
            observer->windows[index].open = true;
            observer->windows[index].uid = uid;
            snprintf(observer->windows[index].measurement_id,
                     sizeof(observer->windows[index].measurement_id), "%s", id);
            return &observer->windows[index];
        }
    }
    return NULL;
}

/* One member's live tasks, with PSS summed once per address space.
 *
 * Every read failure is counted rather than hidden: ``unread`` is what tells
 * the host that this cycle is not a complete one and therefore may not set
 * the peak.
 */
struct window_census {
    struct window_task *tasks;
    size_t tasks_capacity;
    size_t tasks_count;
    uint64_t pss_bytes;
    uint64_t address_spaces;
    uint64_t pss_failed;
    bool pss_complete;
    bool deduped;
    bool truncated;
};

static void census_task_dir(struct window_census *census, uint32_t tgid)
{
    char directory[64];
    DIR *entries;
    struct dirent *entry;
    size_t seen = 0;

    snprintf(directory, sizeof(directory), "/proc/%u/task", tgid);
    entries = opendir(directory);
    if (entries == NULL)
        return;
    while ((entry = readdir(entries)) != NULL) {
        struct window_task *task;
        uint64_t tid = 0;

        if (++seen > MAX_DENTRIES_PER_TASK) {
            census->truncated = true;
            break;
        }
        if (entry->d_name[0] < '0' || entry->d_name[0] > '9')
            continue;
        if (!parse_u64(entry->d_name, &tid) || tid > UINT32_MAX)
            continue;
        if (census->tasks_count >= census->tasks_capacity) {
            census->truncated = true;
            break;
        }
        task = &census->tasks[census->tasks_count];
        memset(task, 0, sizeof(*task));
        task->tid = (uint32_t)tid;
        if (!read_task_identity((uint32_t)tid, task))
            continue;
        read_task_counters((uint32_t)tid, task);
        /* A task that left between the identity read and the counter read is
         * not a gap in this cycle: its own exit record is the kernel's final
         * accounting for it, which is more precise than a census reading would
         * have been. Keeping it here as a task with no counters would instead
         * mark the whole cycle short, and one tool child finishing mid-scan
         * would then cost the window its baseline for that second. A counter
         * that is missing while the task is still there is a real capability
         * gap and is still reported as one. */
        if ((task->counters & FI_WINDOW_CPU) == 0 && !task_counters_present(task->tid))
            continue;
        census->tasks_count += 1;
    }
    closedir(entries);
}

static void census_address_space(struct window_census *census, uint32_t tgid)
{
    uint64_t pss = 0;

    census->address_spaces += 1;
    if (!read_address_space_pss(tgid, &pss)) {
        census->pss_failed += 1;
        census->pss_complete = false;
        return;
    }
    census->pss_bytes += pss;
    census->deduped = true;
    for (size_t index = 0; index < census->tasks_count; ++index) {
        if (census->tasks[index].tgid == tgid && !census->tasks[index].pss_counted) {
            census->tasks[index].pss_bytes = pss;
            census->tasks[index].counters |= FI_WINDOW_PSS;
            census->tasks[index].pss_counted = true;
            return;
        }
    }
}

/* Census every live task of one member UID.
 *
 * The thread groups are collected first so each address space is read once:
 * summing PSS per thread would charge a multi-threaded process for the same
 * mapping many times over, which is the double count this function exists to
 * prevent.
 */
static int run_census(struct observer *observer,
                      uint32_t uid,
                      struct window_census *census,
                      struct fi_window_sample *sample)
{
    DIR *processes;
    struct dirent *entry;
    struct window_task *tasks;
    uint32_t *groups;
    size_t groups_count = 0;
    size_t seen = 0;
    int result = FI_WINDOW_OK;

    (void)observer;
    tasks = calloc(MAX_WINDOW_TASKS, sizeof(*tasks));
    groups = calloc(MAX_WINDOW_ADDRESS_SPACES, sizeof(*groups));
    if (tasks == NULL || groups == NULL) {
        free(tasks);
        free(groups);
        return FI_WINDOW_ERR_BUSY;
    }
    census->tasks = tasks;
    census->tasks_capacity = MAX_WINDOW_TASKS;
    census->pss_complete = true;
    processes = opendir("/proc");
    if (processes == NULL) {
        free(tasks);
        free(groups);
        return FI_WINDOW_ERR_UNKNOWN;
    }
    while ((entry = readdir(processes)) != NULL) {
        uint64_t pid = 0;
        char path[64];
        char body[WINDOW_STATUS_BYTES];
        uint64_t tgid = 0;
        uint64_t owner = 0;

        if (++seen > MAX_DENTRIES_PER_TASK * 2) {
            census->truncated = true;
            break;
        }
        if (entry->d_name[0] < '0' || entry->d_name[0] > '9')
            continue;
        if (!parse_u64(entry->d_name, &pid) || pid > UINT32_MAX)
            continue;
        /* The path is built from the number that was just parsed, not from
         * the directory entry text, so a name of any length is irrelevant
         * here. A PID is at most ten digits and the longest suffix is
         * "/status", which the buffer has room for by construction. */
        snprintf(path, sizeof(path), "/proc/%u/status", (uint32_t)pid);
        if (read_bounded(path, body, sizeof(body)) <= 0)
            continue;
        if (!labelled_u64(body, "Tgid:", &tgid) || !labelled_u64(body, "Uid:", &owner))
            continue;
        if (owner != uid)
            continue;
        if (tgid != pid)
            continue; /* a non-leader thread; its group is visited as itself */
        census_task_dir(census, (uint32_t)tgid);
        if (groups_count < MAX_WINDOW_ADDRESS_SPACES)
            groups[groups_count++] = (uint32_t)tgid;
        else
            census->truncated = true;
    }
    closedir(processes);
    for (size_t index = 0; index < groups_count; ++index)
        census_address_space(census, groups[index]);
    sample->tasks = (uint64_t)census->tasks_count;
    sample->address_spaces = census->address_spaces;
    sample->pss_bytes = census->pss_bytes;
    sample->pss_failed = census->pss_failed;
    /* The task array outlives this call: the caller journals one entry per
     * task so the host can attribute live growth to an instance instead of
     * to whichever child happened to arrive between two cycles. */
    free(groups);
    return result;
}

static void free_census(struct window_census *census)
{
    free(census->tasks);
    census->tasks = NULL;
    census->tasks_count = 0;
}

/* Write one census cycle.
 *
 * The record carries one compact entry per live task, not only the member's
 * totals. It has to: a total that moves can be moved by a new task starting
 * as easily as by work happening, so a host that saw only totals could not
 * tell a member's own growth from a child's arrival, and would end up
 * charging the same child again when its exit was settled. The entry is the
 * four fields that make an instance identifiable and the three counters the
 * window is about -- no command names, no paths, no per-thread history.
 */
static int journal_window_sample(struct observer *observer,
                                 uint32_t uid,
                                 const char *measurement_id,
                                 uint64_t sample_seq,
                                 uint8_t counters,
                                 uint64_t address_spaces,
                                 uint64_t pss_bytes,
                                 uint64_t pss_failed,
                                 uint64_t cpu_ns,
                                 uint64_t read_bytes,
                                 uint64_t write_bytes,
                                 const struct window_task *tasks,
                                 size_t task_count)
{
    char line[MAX_WINDOW_LINE_BYTES];
    uint64_t mono_ns = 0;
    size_t used;
    int length;

    /* A record with no timestamp cannot be ordered against the window's
     * boundaries, so the cycle is refused rather than stamped with a zero
     * that would read as "at the beginning of the boot". */
    if (monotonic_ns(&mono_ns) < 0)
        return -1;
    observer->event_seq += 1;
    length = snprintf(
        line,
        sizeof(line),
        "{\"version\":%d,\"seq\":%" PRIu64 ",\"kind\":\"window_sample\","
        "\"mono_ns\":%" PRIu64 ",\"measurement_id\":\"%s\",\"uid\":%u,"
        "\"window\":{\"sample_seq\":%" PRIu64 ",\"counters\":%u,"
        "\"tasks\":%zu,\"address_spaces\":%" PRIu64 ","
        "\"pss_bytes\":%" PRIu64 ",\"pss_failed\":%" PRIu64 ","
        "\"cpu_ns\":%" PRIu64 ",\"read_bytes\":%" PRIu64
        ",\"write_bytes\":%" PRIu64 ",\"task_detail\":[",
        FI_OBSERVER_VERSION,
        observer->event_seq,
        mono_ns,
        measurement_id,
        uid,
        sample_seq,
        counters,
        task_count,
        address_spaces,
        pss_bytes,
        pss_failed,
        cpu_ns,
        read_bytes,
        write_bytes);
    if (length <= 0 || (size_t)length >= sizeof(line))
        return -1;
    used = (size_t)length;
    for (size_t index = 0; index < task_count; ++index) {
        const struct window_task *task = &tasks[index];

        /* Per-task I/O is deliberately not carried. The platform reports
         * storage I/O as unmeasured until a deployment proves this filesystem
         * charges a member's writes back to the member, so shipping the bytes
         * would be payload nothing may act on -- and a figure that looks
         * measurable but is contractually refused is worse than absent. The
         * member totals below still carry the cycle's counters, so I/O can be
         * turned on without changing this record's shape. */
        length = snprintf(line + used,
                          sizeof(line) - used,
                          "%s{\"tid\":%u,\"tgid\":%u,\"uid\":%u,"
                          "\"starttime\":%" PRIu64 ",\"cpu_ns\":%" PRIu64
                          ",\"counters\":%u}",
                          index == 0 ? "" : ",",
                          task->tid,
                          task->tgid,
                          task->uid,
                          task->starttime_ticks,
                          task->cpu_ns,
                          (unsigned int)task->counters);
        if (length <= 0 || (size_t)length >= sizeof(line) - used)
            return -1;
        used += (size_t)length;
    }
    length = snprintf(line + used, sizeof(line) - used, "]}}\n");
    if (length <= 0 || (size_t)length >= sizeof(line) - used)
        return -1;
    used += (size_t)length;
    if (observer->journal_bytes + (uint64_t)used > MAX_JOURNAL_BYTES)
        return -1;
    if (write_all(observer->events_fd, line, used) < 0)
        return -1;
    observer->journal_bytes += (uint64_t)used;
    return 0;
}

/* Which per-cycle facts this census established.
 *
 * Its own function because these are the bits every later decision rests on,
 * and because a rule stated in one expression inside a hundred-line function
 * is a rule no test can reach. ``tests/unit/guest_window_counters.py`` calls
 * exactly this code.
 *
 * FI_WINDOW_READING_COMPLETE is what the host gates its per-instance ranges
 * on, so it is about the counter the platform actually reports. It used to
 * require the task I/O counters as well, which tied the CPU measurement to a
 * counter this platform declines to publish: a kernel built without task I/O
 * accounting, or one process whose io file could not be read, left every cycle
 * short and the window measured no CPU at all. An empty member also counts as
 * complete -- a turn that finished its work and went quiet is a measured zero,
 * which is the one case where a total of zero is a measurement rather than an
 * absence, and requiring a live task here made that state unreachable.
 */
static uint8_t window_counter_bits(const struct window_census *census,
                                   bool cpu_complete,
                                   bool io_complete)
{
    uint8_t counters = 0;

    if (census->tasks_count == 0 || cpu_complete)
        counters |= FI_WINDOW_CPU;
    if (census->tasks_count == 0 || io_complete)
        counters |= FI_WINDOW_STORAGE_IO;
    if (cpu_complete)
        counters |= FI_WINDOW_READING_COMPLETE;
    if (census->pss_complete && census->address_spaces > 0)
        counters |= (uint8_t)(FI_WINDOW_PSS | FI_WINDOW_PSS_COMPLETE);
    if (census->deduped)
        counters |= FI_WINDOW_ADDRESS_SPACES_DEDUPED;
    if (census->truncated)
        counters |= FI_WINDOW_TRUNCATED;
    return counters;
}

/* Reduce a census into the one window record the host reads.
 *
 * The per-task sums are computed here and only here. Writing one record per
 * task per second would turn a quiet machine into a journal of every PID in
 * the guest, which is both a privacy question and the exact thing that makes
 * a sampler too expensive to leave on.
 */
static int emit_window_sample(struct observer *observer,
                              struct member_window *window,
                              uint64_t sample_seq)
{
    struct window_census census = {0};
    struct fi_window_sample sample = {0};
    uint64_t cpu_ns = 0;
    uint64_t read_bytes = 0;
    uint64_t write_bytes = 0;
    uint8_t counters = 0;
    bool cpu_complete = true;
    bool io_complete = true;
    int status;

    status = run_census(observer, window->uid, &census, &sample);
    if (status != FI_WINDOW_OK)
        return status;
    /* The per-task figures are summed here, from the one array the identity
     * and counter reads both wrote to, so a task that exited between those
     * two reads contributes neither rather than one of them. CPU and storage
     * I/O are reported only when every task in the cycle yielded them: a sum
     * over a subset is a floor, and offering it as the window's total would
     * be the overclaim this whole shape exists to prevent. An empty member is
     * a genuine zero, which is why it takes the other branch. */
    for (size_t index = 0; index < census.tasks_count; ++index) {
        const struct window_task *task = &census.tasks[index];

        if ((task->counters & FI_WINDOW_CPU) != 0)
            cpu_ns += task->cpu_ns;
        else
            cpu_complete = false;
        if ((task->counters & FI_WINDOW_STORAGE_IO) != 0) {
            read_bytes += task->read_bytes;
            write_bytes += task->write_bytes;
        } else {
            io_complete = false;
        }
    }
    counters = window_counter_bits(&census, cpu_complete, io_complete);
    if (journal_window_sample(observer,
                              window->uid,
                              window->measurement_id,
                              sample_seq,
                              counters,
                              sample.address_spaces,
                              sample.pss_bytes,
                              sample.pss_failed,
                              cpu_ns,
                              read_bytes,
                              write_bytes,
                              census.tasks,
                              census.tasks_count) < 0) {
        free_census(&census);
        return FI_WINDOW_ERR_JOURNAL;
    }
    free_census(&census);
    return FI_WINDOW_OK;
}

static bool trusted_window_sender(pid_t pid)
{
    char executable[64];
    struct stat expected;
    struct stat actual;
    int length;

    if (pid <= 1 || stat(WINDOW_HELPER_PATH, &expected) < 0)
        return false;
    length = snprintf(executable, sizeof(executable), "/proc/%ld/exe", (long)pid);
    if (length <= 0 || (size_t)length >= sizeof(executable) || stat(executable, &actual) < 0)
        return false;
    return expected.st_dev == actual.st_dev && expected.st_ino == actual.st_ino;
}

static int open_window_socket(void)
{
    struct sockaddr_un address = {.sun_family = AF_UNIX};
    int enabled = 1;
    int fd = socket(AF_UNIX, SOCK_DGRAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);

    if (fd < 0)
        return -1;
    if (setsockopt(fd, SOL_SOCKET, SO_PASSCRED, &enabled, sizeof(enabled)) < 0)
        goto error;
    memcpy(address.sun_path, FI_WINDOW_SOCKET, sizeof(FI_WINDOW_SOCKET));
    if (unlink(FI_WINDOW_SOCKET) < 0 && errno != ENOENT)
        goto error;
    if (bind(fd, (const struct sockaddr *)&address,
             offsetof(struct sockaddr_un, sun_path) + sizeof(FI_WINDOW_SOCKET)) < 0)
        goto error;
    /* The socket lives in a root-only directory, so only root can reach it;
     * the credential check below is what decides whether the sender is the
     * reviewed driver rather than something else running as root. */
    if (chmod(FI_WINDOW_SOCKET, 0600) < 0)
        goto error;
    return fd;

error:
    close(fd);
    unlink(FI_WINDOW_SOCKET);
    return -1;
}

static uint32_t handle_window_request(struct observer *observer,
                                      const struct fi_window_request *request)
{
    struct member_window *window;
    char identifier[FI_WINDOW_MEASUREMENT_ID_BYTES];

    if (request->version != FI_WINDOW_REQUEST_VERSION)
        return FI_WINDOW_ERR_REQUEST;
    if (request->kind == FI_WINDOW_PROBE) {
        /* Probe reports what the guest can read without opening a window. */
        struct window_census census = {0};
        struct fi_window_sample sample = {0};
        int status = run_census(observer, request->uid, &census, &sample);
        bool pss_complete = census.pss_complete;

        if (status != FI_WINDOW_OK)
            return (uint32_t)status;
        /* A successful census leaves its task array to the caller, here as
         * in every cycle: this loader is long-lived, and a probe that kept
         * it would grow the guest for as long as probes arrive. */
        free_census(&census);
        return pss_complete ? FI_WINDOW_OK : FI_WINDOW_ERR_INCOMPLETE;
    }
    memcpy(identifier, request->measurement_id, sizeof(identifier));
    identifier[sizeof(identifier) - 1] = '\0';
    if (!measurement_id_valid(identifier))
        return FI_WINDOW_ERR_REQUEST;
    window = find_window(observer, identifier);
    switch (request->kind) {
    case FI_WINDOW_REGISTER:
        if (window == NULL) {
            window = open_window(observer, request->uid, identifier);
            if (window == NULL)
                return FI_WINDOW_ERR_LIMIT;
        } else if (window->uid != request->uid) {
            /* One measurement belongs to one member UID. Rebinding it would
             * bill a different member's work to the earlier member's turn. */
            return FI_WINDOW_ERR_IDENTITY;
        }
        return (uint32_t)emit_window_sample(observer, window, 0);
    case FI_WINDOW_SAMPLE:
        if (window == NULL)
            return FI_WINDOW_ERR_UNKNOWN;
        if (window->uid != request->uid)
            return FI_WINDOW_ERR_IDENTITY;
        return (uint32_t)emit_window_sample(observer, window, request->sample_seq);
    case FI_WINDOW_CLOSE:
        if (window == NULL)
            return FI_WINDOW_ERR_UNKNOWN;
        if (window->uid != request->uid)
            return FI_WINDOW_ERR_IDENTITY;
        window->open = false;
        return FI_WINDOW_OK;
    default:
        return FI_WINDOW_ERR_REQUEST;
    }
}

static void drain_window_requests(struct observer *observer)
{
    for (unsigned int count = 0; count < MAX_TOOL_MARKS_PER_POLL; ++count) {
        struct fi_window_request request = {0};
        struct iovec iov = {.iov_base = &request, .iov_len = sizeof(request)};
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
        uint32_t status;
        ssize_t received = recvmsg(observer->window_fd, &message, MSG_DONTWAIT);

        if (received < 0 && errno == EINTR)
            continue;
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
        if (received != (ssize_t)sizeof(request) || (message.msg_flags & MSG_TRUNC) != 0 ||
            message.msg_namelen == 0 || credential == NULL ||
            !trusted_window_sender(credential->pid)) {
            observer->window_dropped += 1;
            continue;
        }
        status = handle_window_request(observer, &request);
        (void)sendto(observer->window_fd, &status, sizeof(status), MSG_NOSIGNAL,
                     (const struct sockaddr *)&peer, message.msg_namelen);
    }
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
    if (unlink(FI_TOOL_MARK_SOCKET) < 0 && errno != ENOENT)        goto error;
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
        ",\"window_dropped\":%" PRIu64
        ",\"journal_bytes\":%" PRIu64 ",\"journal_dev\":%" PRIu64
        ",\"journal_ino\":%" PRIu64 "}\n",
        FI_OBSERVER_VERSION,
        (long)getpid(),
        observer->poll_seq,
        observer->event_seq,
        lost,
        observer->journal_dropped,
        observer->tool_mark_dropped,
        observer->window_dropped,
        observer->journal_bytes,
        observer->journal_dev,
        observer->journal_ino);
    if (length <= 0 || (size_t)length >= sizeof(status))
        return -1;
    if (format_into(temporary, sizeof(temporary), "%s.tmp.%ld", STATUS_PATH, (long)getpid()) < 0)
        return -1;
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
    struct observer observer = {
        .events_fd = -1,
        .lost_map_fd = -1,
        .tool_mark_fd = -1,
        .window_fd = -1,
    };
    size_t link_count = 0;
    bool self_test = argc == 2 && strcmp(argv[1], "--self-test") == 0;
    bool self_test_child = argc == 2 && strcmp(argv[1], "--self-test-child") == 0;
    int result = 1;

    if (self_test_child)
        return 0;
    if (argc > 2 || (argc == 2 && !self_test)) {
        fprintf(stderr, "usage: %s [--self-test]\n", argv[0]);
        return 2;
    }
    libbpf_set_print(quiet_libbpf);
    {
        long page = sysconf(_SC_PAGESIZE);

        if (page > 0)
            page_bytes = (uint64_t)page;
    }
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
    if (link_count != 4) {
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
        uint8_t capability;

        if (exercise_lifecycle_events(ring_map_fd, &ring) < 0)
            goto cleanup;
        /* The preflight reads the first line to identify the helper and the
         * second to learn what this guest can actually measure. The member
         * window is a separate capability from the lifecycle events: kernels
         * differ on per-task runtime and on smaps_rollup, and a host must not
         * advertise a metric this guest will only ever return as missing. */
        capability = probe_window_capability();
        printf("freeinference-guest-observer-v%d\n", FI_OBSERVER_VERSION);
        printf("{\"window\":{\"cpu\":%s,\"storage_io\":%s,\"pss\":%s,"
               "\"counters\":%u}}\n",
               (capability & FI_WINDOW_CPU) != 0 ? "true" : "false",
               (capability & FI_WINDOW_STORAGE_IO) != 0 ? "true" : "false",
               (capability & FI_WINDOW_PSS) != 0 ? "true" : "false",
               (unsigned int)capability);
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
    observer.window_fd = open_window_socket();
    if (observer.window_fd < 0) {
        perror("open member window socket");
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
            {.fd = observer.window_fd, .events = POLLIN},
        };
        int polled = poll(waiters, 3, 50);

        if (polled < 0 && errno != EINTR) {
            perror("guest observer poll");
            goto cleanup;
        }
        if ((waiters[0].revents | waiters[1].revents | waiters[2].revents) &
            (POLLERR | POLLHUP | POLLNVAL)) {
            fprintf(stderr, "guest observer poll descriptor failed\n");
            goto cleanup;
        }
        if ((waiters[1].revents & POLLIN) != 0)
            drain_tool_marks(&observer);
        /* The window census runs on the same thread as the ring drain, so it
         * is bounded in both tasks and bytes: one wedged proc read must not
         * hold back the lifecycle events that are the observer's main job. */
        if ((waiters[2].revents & POLLIN) != 0)
            drain_window_requests(&observer);
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
        unlink(FI_WINDOW_SOCKET);
    }
    ring_buffer__free(ring);
    if (observer.events_fd >= 0)
        close(observer.events_fd);
    if (observer.tool_mark_fd >= 0)
        close(observer.tool_mark_fd);
    if (observer.window_fd >= 0)
        close(observer.window_fd);
    destroy_links(links, link_count);
    bpf_object__close(object);
    return result;
}
