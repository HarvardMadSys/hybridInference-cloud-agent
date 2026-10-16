# CloudLab autoscaling design

**Status:** implemented for the administrator-managed staging pool

## Purpose

The existing **Admin → Servers** page already creates and retires individual
CloudLab experiments. Autoscaling adds a durable CloudLab pool policy without
turning the browser into an infrastructure controller. Closing the page does
not stop reconciliation, and refreshing it cannot submit a second experiment.

The pool has two deliberately separate ownership classes:

- manually added servers remain operator-owned and are never automatically
  retired;
- autoscaler members are recorded at allocation creation and are the only
  resources eligible for automatic scale-in.

## Policy

One revisioned CloudLab policy records:

- minimum and maximum member counts;
- verified CloudLab and SSH connection references;
- the CloudLab project, profile, duration and profile bindings;
- a set of exclusively reserved deployment profiles;
- sandbox storage and automatic lease-renewal policy;
- scale-up sustain, scale-down idle and cooldown intervals;
- an opt-in scale-down switch; and
- a versioned host-capacity slot policy.

Enabling the policy requires automatic placement. A pinned Host would make new
capacity unreachable, so the write is refused rather than silently accepting a
policy that cannot serve work. CloudLab nodes must register their SSH host keys
at first boot. An unchecked first connection is not accepted for unattended
provisioning.

Disabling the policy pauses future decisions. It does not release experiments,
remove members or give their deployment profiles to manual allocation. This
makes a pause reversible and prevents a checkbox from becoming a destructive
action.

## Profile ownership

Each live Host needs an exclusive deployment profile because the profile owns
its sandbox manager route and Host inputs. Policy saves reserve the selected profiles in
the database under the compute-topology lock. Manual implicit selection skips
reserved profiles, and an explicit attempt to use one is refused.

Automatic node rotation is intentionally not part of v1. The existing manual
server workflow can rotate an individually managed node, but autoscaler-owned
membership and profile ownership must transfer atomically before the pool can
support overlapping replacement. The API rejects `rotation_enabled=true`
rather than accepting a policy it cannot execute safely. A future rotation
version will require a compatible spare profile in addition to serving
capacity.

Instead, a member whose lease enters its move-out window moves its groups out
one at a time through their checkpoints, and stops counting toward
`min_hosts` so its replacement is acquired while it still serves. The
replacement needs room under `max_hosts` and a free reserved profile. See the
[host move-out design](2026-10-16-host-move-out-design.zh-CN.md).

An autoscaler member does not expose the individual server's policy or rotate
actions. Both commands fail closed against the durable membership record, and
the worker refuses any stale rotation operation that predates this ownership
check. Renewal changes for members go through the singleton pool policy so an
individual edit cannot silently escape pool ownership.

## Reconciliation

The control-plane compute worker runs the controller. Every side effect begins
with persisted intent and a generation check. A scale-out request uses a stable
request ID derived from that generation and creates the allocation, ownership
record and acquire operation in one transaction.

The controller restores `min_hosts` immediately. Above the minimum, it scales
out one member after sustained new-conversation queue pressure cannot be
matched to current fresh, claimable slots. Matching uses each job's required
capabilities and execution profile, including the Kata requirement for
untrusted work, so unrelated manual capacity cannot hide real demand. Once an
autoscaler-owned Host is available as a template, demand that none of the pool
templates can serve is not treated as scale-out pressure. Before a template
exists, demand may create at most one probe member beyond a zero minimum.
Provisioning members count toward the maximum. A blocked, failed or
outcome-unknown acquisition opens a circuit breaker; the controller does not
create another experiment while the result needs attention. This is required
because CloudLab has no idempotency key for experiment creation.

Scale-in is disabled by default. When enabled, it starts only after the idle
window and cooldown, never below `min_hosts`, and only for an autoscaler-owned
member with no running slots or retained sandbox groups. It queues the existing
guarded retirement flow. That flow rechecks the complete inventory before the
provider release and has no autoscaling force path.
An active deployment drain fence excludes the Host from selection and blocks
the irreversible release boundary until the deployment owner releases its
fence.

## Automatic slots

Autoscaler members use fresh Host heartbeat capacity, not browser arithmetic.
Version 1 computes:

```text
cpu_slots    = floor(cpu_reservation_fraction * logical_cpus / cpu_per_slot)
memory_slots = floor((memory_gib - memory_reserve_gib) / memory_per_slot_gib)
desired      = min(cpu_slots, memory_slots, max_slots)
```

The staging defaults are a 75% CPU allowance, two logical CPUs per slot, 8 GiB
reserved memory and 4 GiB per slot. A 16-thread, 64-GiB m510 therefore receives
six slots. A result below two blocks readiness instead of rounding up beyond
the machine's safe capacity. Missing or stale telemetry never increases a
limit. An autoscaler-owned Host remains under an all-claims admission hold from
bootstrap until fresh telemetry produces a valid target and the Host reports
that target on a later heartbeat. This prevents jobs from racing ahead of the
reviewed concurrency limit. Desired-slot writes and the exact hold clear share
one repository transaction that rechecks the policy revision, runtime
generation, automatic placement, owned binding, launch health, heartbeat, and
the exact fresh capacity sample under the compute lock. A concurrent pause or
placement pin wins before any slot or hold mutation: an existing sizing hold
remains, while a Host whose hold was already cleared stays serving without a
new restriction. Generic per-Host slot edits take the same compute/topology
lock and reject every non-released autoscaler member; only the fenced derived
target may size an owned Host. Neither mode silently admits an unsized Host.

## Preferred node types

The dashboard reads CloudLab's public hardware inventory in the same curated
order used for new allocations. CloudLab defaults supply the ordered
`node_types` list and `node_type_binding` (normally `phystype`); the administrator
can edit the list or pin one type using an explicit profile binding.

The v1 shortlist includes only curated, general-purpose, non-GPU CloudLab
families introduced in 2021 or later with at least 16 physical cores and 128
GiB RAM and more than 20 total nodes. The default order is `r6615`, `c6620`,
`r650`, `r6525`, `c6525-100g`, `c6525-25g`: newer families first, then more CPU
cores and RAM within the same introduction year. Current free counts do not
change the order or hide a family. Each row displays its
site, total inventory, cores, threads, memory and introduction year so the
filter remains reviewable.

Each new manual or autoscaled allocation snapshots the preference list. The
provider tries it from top to bottom, advancing only after a confirmed capacity
rejection. It stops when an experiment is created or the outcome is ambiguous;
credentials, profile errors and other failures also stop the sequence. If every
type is refused for capacity, the durable operation waits and retries from the
top. An explicit nonempty hardware binding pins the request to that type; an
empty candidate list leaves hardware selection to the profile. Existing
allocations keep their recorded configuration.

Availability comes from the inert `amlist-json` payload on CloudLab's public
hardware page. The control plane uses a fixed HTTPS URL, follows no redirects,
sends no CloudLab credential, bounds the response, validates the expected site
and count shapes, and caches a successful observation for 60 seconds. It never
serves an expired observation after a refresh failure. A malformed, redirected
or unavailable source produces a non-blocking unavailable state in the panel;
policy reads and edits continue independently.

## Administrative surface

The policy is available only on the existing admin-authenticated compute
surface:

```text
GET /admin/agent/compute-autoscaling/cloudlab
PUT /admin/agent/compute-autoscaling/cloudlab
GET /admin/agent/compute-availability/cloudlab
```

Responses are `no-store`. They expose connection IDs, non-secret CloudLab
configuration, pool/member state, aggregate queue/slot counts and public
blockers. They never expose connection secret material, deployment environment
contents, source bundles or worker fences. Writes use compare-and-swap
revisions and the ordinary admin audit trail.
