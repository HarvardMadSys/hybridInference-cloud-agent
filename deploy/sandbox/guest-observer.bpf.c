// SPDX-License-Identifier: GPL-2.0 OR BSD-3-Clause
/* Process lifecycle events from the Kata guest kernel, not Docker polling. */

#include <linux/bpf.h>
#include <bpf/bpf_core_read.h>
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>

#include "guest-observer.h"

/* Minimal CO-RE shapes.  Field offsets are relocated from the guest's BTF. */
struct task_struct {
    int pid;
    int tgid;
    struct task_struct *real_parent;
    int exit_code;
    char comm[FI_TASK_COMM_LEN];
} __attribute__((preserve_access_index));

struct linux_binprm {
    const char *filename;
} __attribute__((preserve_access_index));

struct {
    __uint(type, BPF_MAP_TYPE_RINGBUF);
    __uint(max_entries, 1 << 20);
} process_events SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, __u64);
} lost_events SEC(".maps");

static __always_inline void record_lost(void)
{
    __u32 key = 0;
    __u64 *lost = bpf_map_lookup_elem(&lost_events, &key);

    if (lost)
        *lost += 1;
}

static __always_inline struct fi_process_event *reserve_event(__u8 kind)
{
    struct fi_process_event *event;

    event = bpf_ringbuf_reserve(&process_events, sizeof(*event), 0);
    if (!event) {
        record_lost();
        return 0;
    }
    __builtin_memset(event, 0, sizeof(*event));
    event->kind = kind;
    event->mono_ns = bpf_ktime_get_ns();
    event->uid = (__u32)bpf_get_current_uid_gid();
    return event;
}

static __always_inline void fill_task(
    struct fi_process_event *event,
    struct task_struct *task)
{
    struct task_struct *parent = BPF_CORE_READ(task, real_parent);

    event->pid = (__u32)BPF_CORE_READ(task, pid);
    event->tgid = (__u32)BPF_CORE_READ(task, tgid);
    if (parent)
        event->ppid = (__u32)BPF_CORE_READ(parent, tgid);
    bpf_core_read_str(event->comm, sizeof(event->comm), &task->comm);
}

SEC("raw_tracepoint/sched_process_fork")
int observe_fork(struct bpf_raw_tracepoint_args *ctx)
{
    struct task_struct *child = (struct task_struct *)ctx->args[1];
    struct fi_process_event *event = reserve_event(FI_PROCESS_FORK);

    if (!event)
        return 0;
    fill_task(event, child);
    bpf_ringbuf_submit(event, 0);
    return 0;
}

SEC("raw_tracepoint/sched_process_exec")
int observe_exec(struct bpf_raw_tracepoint_args *ctx)
{
    struct task_struct *task = (struct task_struct *)ctx->args[0];
    struct linux_binprm *bprm = (struct linux_binprm *)ctx->args[2];
    struct fi_process_event *event = reserve_event(FI_PROCESS_EXEC);
    const char *filename;

    if (!event)
        return 0;
    fill_task(event, task);
    filename = BPF_CORE_READ(bprm, filename);
    if (filename)
        bpf_probe_read_kernel_str(event->filename, sizeof(event->filename), filename);
    bpf_ringbuf_submit(event, 0);
    return 0;
}

SEC("raw_tracepoint/sched_process_exit")
int observe_exit(struct bpf_raw_tracepoint_args *ctx)
{
    struct task_struct *task = (struct task_struct *)ctx->args[0];
    struct fi_process_event *event = reserve_event(FI_PROCESS_EXIT);

    if (!event)
        return 0;
    fill_task(event, task);
    event->exit_code = BPF_CORE_READ(task, exit_code);
    bpf_ringbuf_submit(event, 0);
    return 0;
}

char LICENSE[] SEC("license") = "Dual BSD/GPL";
