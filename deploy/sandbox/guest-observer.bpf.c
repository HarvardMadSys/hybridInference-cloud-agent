// SPDX-License-Identifier: GPL-2.0 OR BSD-3-Clause
/* Process lifecycle events from the Kata guest kernel, not Docker polling. */

#include <linux/bpf.h>
#include <bpf/bpf_core_read.h>
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>

#include "guest-observer.h"

/* Minimal CO-RE shapes.  Field offsets are relocated from the guest's BTF. */
struct signal_struct {
    unsigned long maxrss;
} __attribute__((preserve_access_index));

struct task_io_accounting {
    __u64 rchar;
    __u64 wchar;
    __u64 read_bytes;
    __u64 write_bytes;
} __attribute__((preserve_access_index));

struct task_struct {
    int pid;
    int tgid;
    struct task_struct *real_parent;
    struct signal_struct *signal;
    __u64 utime;
    __u64 stime;
    struct task_io_accounting ioac;
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

/* What this task consumed, read at the one moment the kernel has settled it.
 *
 * Every field is guarded by its presence in the guest's BTF: a kernel built
 * without task I/O accounting still loads this program and the loader reports
 * that counter as unavailable, never as zero. utime/stime are this task's own
 * share and exclude children; the host sums a group's threads and never adds
 * signal->utime on top, which would count the dead threads twice. maxrss is
 * settled by whichever thread empties the group, so the host keeps the
 * maximum seen across the group's exits. */
static __always_inline void fill_exit_counters(
    struct fi_process_event *event,
    struct task_struct *task)
{
    if (bpf_core_field_exists(task->utime) && bpf_core_field_exists(task->stime)) {
        event->cpu_ns = BPF_CORE_READ(task, utime) + BPF_CORE_READ(task, stime);
        event->counters |= FI_COUNTER_CPU;
    }
    if (bpf_core_field_exists(task->signal)) {
        struct signal_struct *signal = BPF_CORE_READ(task, signal);

        if (signal && bpf_core_field_exists(signal->maxrss)) {
            event->maxrss_pages = BPF_CORE_READ(signal, maxrss);
            event->counters |= FI_COUNTER_MAXRSS;
        }
    }
    if (bpf_core_field_exists(task->ioac.read_bytes) &&
        bpf_core_field_exists(task->ioac.write_bytes)) {
        event->read_bytes = BPF_CORE_READ(task, ioac.read_bytes);
        event->write_bytes = BPF_CORE_READ(task, ioac.write_bytes);
        event->counters |= FI_COUNTER_IO;
    }
    if (bpf_core_field_exists(task->ioac.rchar) && bpf_core_field_exists(task->ioac.wchar)) {
        event->rchar = BPF_CORE_READ(task, ioac.rchar);
        event->wchar = BPF_CORE_READ(task, ioac.wchar);
        event->counters |= FI_COUNTER_XACCT;
    }
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
    fill_exit_counters(event, task);
    bpf_ringbuf_submit(event, 0);
    return 0;
}

char LICENSE[] SEC("license") = "Dual BSD/GPL";
