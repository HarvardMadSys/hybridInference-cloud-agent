#ifndef FREEINFERENCE_GUEST_OBSERVER_H
#define FREEINFERENCE_GUEST_OBSERVER_H

#include <linux/types.h>

#define FI_OBSERVER_VERSION 1
#define FI_TASK_COMM_LEN 16
#define FI_EXECUTABLE_LEN 192

enum fi_process_event_kind {
    FI_PROCESS_FORK = 1,
    FI_PROCESS_EXEC = 2,
    FI_PROCESS_EXIT = 3,
};

/* Fixed-width ring-buffer ABI shared by the BPF program and trusted loader. */
struct fi_process_event {
    __u64 mono_ns;
    __u32 uid;
    __u32 pid;
    __u32 tgid;
    __u32 ppid;
    __s32 exit_code;
    __u8 kind;
    __u8 reserved[3];
    char comm[FI_TASK_COMM_LEN];
    char filename[FI_EXECUTABLE_LEN];
};

#endif
