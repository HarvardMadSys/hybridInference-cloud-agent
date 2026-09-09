#ifndef FREEINFERENCE_GUEST_OBSERVER_H
#define FREEINFERENCE_GUEST_OBSERVER_H

#include <linux/types.h>

#define FI_OBSERVER_VERSION 1
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

#endif
