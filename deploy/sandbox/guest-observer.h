#ifndef FREEINFERENCE_GUEST_OBSERVER_H
#define FREEINFERENCE_GUEST_OBSERVER_H

#include <linux/types.h>

#define FI_OBSERVER_VERSION 2
#define FI_TASK_COMM_LEN 16
#define FI_EXECUTABLE_LEN 192

enum fi_process_event_kind {
    FI_PROCESS_FORK = 1,
    FI_PROCESS_EXEC = 2,
    /* sched_process_exit: the kernel announced the exit. exit_code, the
     * group's settled peak RSS and the task's I/O bytes are final here, and
     * group_dead says whether this exit emptied the thread group. */
    FI_PROCESS_EXIT = 3,
    /* sched_switch with the task in TASK_DEAD: it left the CPU for the last
     * time, so the scheduler's own runtime for it is final. CPU is read
     * here and nowhere else — the tick-based utime/stime a task carries at
     * sched_process_exit is zero for most sub-millisecond processes. */
    FI_PROCESS_DONE = 4,
};

/* Which exit-time counters the guest kernel actually exposed. A bit that is
 * clear means the field was absent from this kernel's BTF, and the loader
 * reports that counter as unavailable rather than as zero. */
enum fi_process_counter {
    FI_COUNTER_CPU = 1U << 0,    /* cpu_ns: se.sum_exec_runtime at TASK_DEAD */
    FI_COUNTER_MAXRSS = 1U << 1, /* maxrss_pages: the thread group's peak RSS */
    FI_COUNTER_IO = 1U << 2,     /* read_bytes / write_bytes: storage I/O */
    FI_COUNTER_XACCT = 1U << 3,  /* rchar / wchar: bytes through read/write */
    FI_COUNTER_GROUP = 1U << 4,  /* group_dead is known (signal->live read) */
};

/* Which live counters a member window census could actually read, and which
 * properties of that census held. These are per-cycle facts rather than
 * per-kernel ones: a window can read CPU fine and still fail to read one
 * process's PSS. A clear bit is unavailable, never zero. */
enum fi_window_counter {
    FI_WINDOW_CPU = 1U << 0,          /* cpu_ns, the scheduler's own total */
    FI_WINDOW_STORAGE_IO = 1U << 1,   /* read_bytes / write_bytes per task */
    FI_WINDOW_PSS = 1U << 2,          /* pss_bytes is a real member sum */
    FI_WINDOW_PSS_COMPLETE = 1U << 3, /* every member address space was read */
    /* Address spaces were summed once each, keyed by thread group rather than
     * by thread. Without it the PSS figure may count one mapping twice. */
    FI_WINDOW_ADDRESS_SPACES_DEDUPED = 1U << 4,
    FI_WINDOW_TRUNCATED = 1U << 5, /* the census hit its task bound */
    /* Every live task in this cycle yielded its scheduler runtime, or the
     * member had no live task at all. Clear means the CPU total above is a
     * floor over the tasks that did answer, and the cycle may not open or
     * close an instance's range. Deliberately says nothing about the task I/O
     * counters: they are reported through FI_WINDOW_STORAGE_IO, and a kernel
     * that does not account them must not cost the window its CPU baseline. */
    FI_WINDOW_READING_COMPLETE = 1U << 6,
};

/* Fixed-width ring-buffer ABI shared by the BPF program and trusted loader. */
struct fi_process_event {
    __u64 mono_ns;
    /* Exit-time accounting, per task. Threads report their own share; the
     * host folds every thread of a group into its leader. maxrss is only
     * settled by the last thread to leave the group and is in pages, which
     * the loader converts with the guest's page size. */
    __u64 cpu_ns;
    __u64 maxrss_pages;
    __u64 read_bytes;
    __u64 write_bytes;
    __u64 rchar;
    __u64 wchar;
    __u32 uid;
    __u32 pid;
    __u32 tgid;
    __u32 ppid;
    __s32 exit_code;
    __u8 kind;
    __u8 counters;
    /* On an exit record: this exit brought the group's live thread count
     * to zero. The kernel decrements it before the tracepoint, so a leader
     * whose threads live on reports 0 and the last thread to go reports 1. */
    __u8 group_dead;
    __u8 reserved;
    char comm[FI_TASK_COMM_LEN];
    char filename[FI_EXECUTABLE_LEN];
};

/* One live task's counters, as a member window census read them.
 *
 * Every field comes from the same kernel total the exit records use, so a
 * thread that is alive when one window closes and dead when the next opens is
 * measured by one method throughout. cpu_ns is the scheduler's own
 * accumulated runtime (field 0 of ``/proc/<tid>/schedstat``), which is the
 * same quantity ``se.sum_exec_runtime`` holds for a task that has just died.
 * Tick-accounted utime/stime would be a different and coarser number, and
 * mixing the two is the error this record exists to avoid.
 *
 * starttime_ticks is field 22 of ``/proc/<tid>/stat``: when the task started,
 * in clock ticks since boot. With the boot identifier and the TID it is the
 * stable instance key, so a recycled PID cannot inherit an older thread's
 * baseline. It is deliberately left in ticks: USER_HZ is a property of the
 * guest, and converting here would invite comparing it against a nanosecond
 * field later. */
struct fi_thread_sample {
    __u64 cpu_ns;
    __u64 read_bytes;
    __u64 write_bytes;
    __u64 starttime_ticks;
    __u64 pss_bytes;
    __u32 uid;
    __u32 tid;
    __u32 tgid;
    __u8 counters;
    __u8 reserved[3];
};

#define FI_WINDOW_MEASUREMENT_ID_BYTES 64
#define FI_WINDOW_MAX_OPEN 16
#define FI_WINDOW_SOCKET "/run/freeinference-observer/member-window.sock"
#define FI_WINDOW_DRIVER "/usr/local/libexec/freeinference-member-window"
#define FI_WINDOW_REQUEST_VERSION 1U

/* One census cycle of one member window.
 *
 * ``error`` is zero when the cycle ran to completion. A non-zero error still
 * carries whatever the cycle did read, because a partial cycle is evidence
 * and a discarded one is not; the host decides what a partial cycle may set.
 * ``sample_seq`` is the requester's own counter, echoed so the host can see
 * that a cycle went missing rather than silently treating two samples as
 * consecutive. */
struct fi_window_sample {
    __u64 mono_ns;
    __u64 sample_seq;
    __u64 tasks;
    __u64 address_spaces;
    __u64 pss_bytes;
    __u64 pss_failed;
    __u32 uid;
    __u32 error;
    char measurement_id[FI_WINDOW_MEASUREMENT_ID_BYTES];
};

/* The fixed request the trusted driver sends over FI_WINDOW_SOCKET. */
enum fi_window_request_kind {
    /* Bind a member UID to a measurement and take its opening census. This
     * is a window's baseline, so it happens before the prompt is sent. */
    FI_WINDOW_REGISTER = 1,
    /* Take one more census of an open window. */
    FI_WINDOW_SAMPLE = 2,
    /* Stop tracking the window. The final census is an ordinary sample the
     * caller takes first, so the caller owns where the window ends. */
    FI_WINDOW_CLOSE = 3,
    /* Report which live counters this guest can read; takes no window. */
    FI_WINDOW_PROBE = 4,
};

/* Acknowledged results. Success means the records reached the journal, not
 * that every counter inside them was available. */
enum fi_window_status {
    FI_WINDOW_OK = 0,
    FI_WINDOW_ERR_REQUEST = 1,
    FI_WINDOW_ERR_UNKNOWN = 2,
    FI_WINDOW_ERR_BUSY = 3,
    FI_WINDOW_ERR_LIMIT = 4,
    FI_WINDOW_ERR_IDENTITY = 5,
    FI_WINDOW_ERR_JOURNAL = 6,
    /* The census ran but could not read every address space it found. */
    FI_WINDOW_ERR_INCOMPLETE = 7,
};

struct fi_window_request {
    __u32 version;
    __u32 kind;
    __u32 uid;
    /* The requester's own cycle counter, echoed into the record it produces.
     * It is the caller's, not the observer's, because a window is sampled by
     * whichever host process owns it: a gap in this number is how the host
     * sees that a cycle it asked for never became a record, instead of
     * reading two samples as consecutive when they were not. */
    __u32 sample_seq;
    char measurement_id[FI_WINDOW_MEASUREMENT_ID_BYTES];
};

#endif
