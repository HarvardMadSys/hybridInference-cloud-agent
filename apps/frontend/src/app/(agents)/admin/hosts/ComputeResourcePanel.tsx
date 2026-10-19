'use client';

import { useRef, useState } from 'react';

import {
  extendComputeAllocation,
  getComputeOperations,
  getRetirementImpact,
  retireComputeAllocation,
  rotateComputeAllocation,
  setComputePolicy,
  type ComputeAllocation,
  type ComputeOperation,
  type ComputeRotation,
  type RenewalPolicy,
  type RetirementImpact,
} from '@/lib/api/compute';
import { DEFAULT_MOVE_OUT_BEFORE_SECONDS } from '@/lib/computeDefaults';
import { getErrorMessage } from '@/lib/utils/errors';

import {
  absoluteTime,
  buttonClass,
  ErrorNotice,
  Field,
  inputClass,
  neverCreated,
  primaryClass,
  Reasons,
  remainingTime,
} from './compute-ui';

export default function ComputeResourcePanel({
  allocation,
  onSaved,
  onOperation,
  section = 'all',
}: {
  allocation: ComputeAllocation;
  onSaved: (allocation: ComputeAllocation) => void;
  onOperation: (operation: ComputeOperation) => void;
  section?: 'all' | 'lease' | 'retire' | 'details';
}) {
  const [policy, setPolicy] = useState(allocation.policy);
  // Typed separately so commas and spaces can be entered before they parse.
  const [nodeTypes, setNodeTypes] = useState(allocation.policy.node_types.join(', '));
  const [adoptManagement, setAdoptManagement] = useState(false);
  const [revision, setRevision] = useState(allocation.revision);
  const [target, setTarget] = useState('');
  const [reason, setReason] = useState('');
  const [impact, setImpact] = useState<RetirementImpact | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const requestIds = useRef<{ extend?: string; retire?: string; rotate?: string }>({});
  const allows = (action: string) => allocation.allowed_actions.includes(action);
  const needsDeployment =
    allocation.management_mode === 'observe' && !allocation.nodes.some((node) => node.hosts.length);

  async function intent(kind: 'extend' | 'retire' | 'rotate') {
    if (busy) return;
    setBusy(kind);
    setError(null);
    const requestId = requestIds.current[kind] ?? crypto.randomUUID();
    requestIds.current[kind] = requestId;
    try {
      let result;
      if (kind === 'extend') {
        result = await extendComputeAllocation(allocation.id, {
          request_id: requestId,
          target_expires_at: new Date(target).toISOString(),
          reason,
        });
      } else if (kind === 'rotate') {
        result = await rotateComputeAllocation(allocation.id, {
          request_id: requestId,
          expected_revision: allocation.revision,
        });
      } else {
        result = await retireComputeAllocation(allocation.id, {
          request_id: requestId,
          expected_revision: impact!.revision,
        });
      }
      onOperation(result.operation);
    } catch (error) {
      try {
        const recovered = await getComputeOperations(requestId);
        const operation = recovered.operations.find((item) => item.request_id === requestId);
        if (operation) {
          onOperation(operation);
          return;
        }
      } catch {
        /* Preserve the same intent ID when the result is not yet known. */
      }
      setError(getErrorMessage(error));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="space-y-6">
      {(section === 'all' || section === 'lease') && (
        <div className="rounded-xl bg-gray-50 p-4 dark:bg-white/5">
          <p className="text-xs text-gray-500 dark:text-gray-400">
            {allocation.provider_id === 'existing_ssh' ? 'Lease' : 'Confirmed expiry'}
          </p>
          <p className="mt-1 text-xl font-semibold tracking-tight">
            {allocation.expires_at
              ? remainingTime(allocation.expires_at)
              : allocation.provider_id === 'existing_ssh'
                ? 'Not managed'
                : 'Not reported'}
          </p>
          {allocation.expires_at && (
            <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
              {absoluteTime(allocation.expires_at)}
            </p>
          )}
          {allocation.rotation && <RotationSummary rotation={allocation.rotation} />}
        </div>
      )}
      {(section === 'all' || section === 'details') && (
        <>
          <Reasons reasons={allocation.blocked_reasons} />
          {needsDeployment && (
            <p className="text-sm text-amber-800 dark:text-amber-300">
              Agent not installed. Use Add server → Connect existing with this resource ID to
              deploy.
            </p>
          )}
          <dl className="divide-y divide-gray-100 rounded-xl border border-gray-200 text-sm dark:divide-white/10 dark:border-white/10">
            <div className="flex justify-between gap-4 px-4 py-3">
              <dt className="shrink-0 text-gray-500 dark:text-gray-400">Resource ID</dt>
              <dd className="break-all text-right">
                {allocation.external_id ?? 'Pending acquisition'}
              </dd>
            </div>
            <div className="flex justify-between gap-4 px-4 py-3">
              <dt className="shrink-0 text-gray-500 dark:text-gray-400">Status</dt>
              <dd className="text-right">
                {allocation.state === 'absent'
                  ? neverCreated(allocation)
                    ? 'Not created'
                    : 'Deregistered (machine still exists)'
                  : allocation.state}
              </dd>
            </div>
            {allocation.sandbox_storage_path && (
              <div className="flex justify-between gap-4 px-4 py-3">
                <dt className="shrink-0 text-gray-500 dark:text-gray-400">Sandbox storage path</dt>
                <dd className="break-all text-right">{allocation.sandbox_storage_path}</dd>
              </div>
            )}
            {allocation.nodes.map((node) => (
              <div key={node.id} className="flex flex-wrap justify-between gap-2 px-4 py-3">
                <dt className="text-gray-500 dark:text-gray-400">
                  {allocation.nodes.length > 1 ? node.name : 'Host'}
                </dt>
                <dd className="min-w-0 break-all text-right">
                  {node.address ? (
                    <>
                      {node.username && `${node.username}@`}
                      {node.address}
                      {node.port && node.port !== 22 ? `:${node.port}` : ''}
                    </>
                  ) : (
                    node.name
                  )}
                  <span className="mt-1 block text-xs text-gray-500 dark:text-gray-400">
                    {node.hosts.join(', ') || 'Awaiting agent'}
                  </span>
                </dd>
              </div>
            ))}
            <div className="flex justify-between gap-4 px-4 py-3">
              <dt className="shrink-0 text-gray-500 dark:text-gray-400">Last observed</dt>
              <dd className="text-right">{absoluteTime(allocation.observed_at)}</dd>
            </div>
          </dl>
          {allocation.check_interval_seconds != null && (
            <p className="text-xs text-gray-500 dark:text-gray-400">
              Periodic provider checks: about every {allocation.check_interval_seconds} seconds.
              Pending operations can delay a check.
            </p>
          )}
        </>
      )}
      {(section === 'all' || section === 'lease') && allows('policy') && (
        <form
          className="space-y-3 border-t border-gray-200 pt-4 dark:border-white/10"
          onSubmit={async (event) => {
            event.preventDefault();
            if (busy) return;
            setBusy('policy');
            setError(null);
            setNotice(null);
            try {
              const result = await setComputePolicy(
                allocation.id,
                policy,
                revision,
                adoptManagement ? 'manage' : undefined,
              );
              setRevision(result.allocation.revision);
              setPolicy(result.allocation.policy);
              setNodeTypes(result.allocation.policy.node_types.join(', '));
              onSaved(result.allocation);
              setNotice(
                'Policy saved. The expiry updates after the provider confirms an extension.',
              );
            } catch (error) {
              setError(getErrorMessage(error));
            } finally {
              setBusy(null);
            }
          }}
        >
          <h3 className="font-semibold">Automatic renewal</h3>
          {allocation.management_mode === 'observe' && (
            <label className="flex items-start gap-2 text-sm">
              <input
                type="checkbox"
                checked={adoptManagement}
                onChange={(event) => setAdoptManagement(event.target.checked)}
              />
              I authorize lifecycle management of this entire allocation and all its nodes,
              including renewal and release after dependency checks.
            </label>
          )}
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={policy.enabled}
              onChange={(event) =>
                setPolicy({
                  ...policy,
                  enabled: event.target.checked,
                  rotation_enabled: event.target.checked && policy.rotation_enabled,
                })
              }
            />
            Request renewal automatically
          </label>
          <div className="grid gap-3 sm:grid-cols-2">
            {(
              [
                ['renew_before_seconds', 'Request before expiry (minutes)'],
                ['extend_to_horizon_seconds', 'Extend ahead of now (minutes)'],
              ] as const
            ).map(([key, label]) => (
              <PolicyNumber
                key={key}
                field={key}
                label={label}
                policy={policy}
                onChange={setPolicy}
              />
            ))}
          </div>
          <Field label="Renewal reason">
            <textarea
              className={inputClass}
              required={policy.enabled}
              maxLength={2000}
              value={policy.reason}
              onChange={(event) => setPolicy({ ...policy, reason: event.target.value })}
            />
          </Field>
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={policy.rotation_enabled}
              disabled={!policy.enabled}
              onChange={(event) =>
                setPolicy({
                  ...policy,
                  rotation_enabled: event.target.checked,
                  renew_before_seconds: event.target.checked
                    ? Math.max(policy.renew_before_seconds, 3 * 3600)
                    : policy.renew_before_seconds,
                  extend_to_horizon_seconds: event.target.checked
                    ? Math.max(policy.extend_to_horizon_seconds, 4 * 3600)
                    : policy.extend_to_horizon_seconds,
                })
              }
            />
            Move to a new node when renewal is refused or the node gets old
          </label>
          {policy.rotation_enabled && (
            <div className="grid gap-3 sm:grid-cols-2">
              <Field label="Replace a node after (days)">
                <input
                  className={inputClass}
                  type="number"
                  min={1}
                  max={30}
                  step={1}
                  required
                  value={policy.max_node_age_seconds / 86400}
                  onChange={(event) =>
                    setPolicy({
                      ...policy,
                      max_node_age_seconds: Number(event.target.value) * 86400,
                    })
                  }
                />
              </Field>
              <Field label="Node types to use, newest first">
                <input
                  className={inputClass}
                  required
                  placeholder="r6615, c6620, d6515"
                  value={nodeTypes}
                  onChange={(event) => {
                    setNodeTypes(event.target.value);
                    setPolicy({
                      ...policy,
                      node_types: event.target.value.split(/[\s,]+/).filter(Boolean),
                    });
                  }}
                />
              </Field>
            </div>
          )}
          {policy.rotation_enabled && (
            <p className="text-xs text-gray-500 dark:text-gray-400">
              Renewal is tried first. Sessions and sandboxes move together if a replacement is
              needed. New work queues while running turns finish and the server moves.
            </p>
          )}
          <details>
            <summary className="cursor-pointer text-sm">Advanced timing</summary>
            <div className="mt-3 grid gap-3 sm:grid-cols-2">
              <PolicyNumber
                field="stop_new_groups_before_seconds"
                label="Stop new sandboxes before expiry (minutes)"
                policy={policy}
                onChange={setPolicy}
              />
              <PolicyNumber
                field="stop_claims_before_seconds"
                label="Stop all new turns before expiry (minutes)"
                policy={policy}
                onChange={setPolicy}
              />
              {!policy.rotation_enabled && (
                <Field label="Move sandboxes to other servers before expiry (minutes, 0 = never)">
                  <input
                    className={inputClass}
                    type="number"
                    min={0}
                    step={1}
                    required
                    value={(policy.move_out_before_seconds ?? DEFAULT_MOVE_OUT_BEFORE_SECONDS) / 60}
                    onChange={(event) =>
                      setPolicy({
                        ...policy,
                        move_out_before_seconds: Number(event.target.value) * 60,
                      })
                    }
                  />
                </Field>
              )}
              <Field label="Node type parameter of the provider profile">
                <input
                  className={inputClass}
                  required
                  value={policy.node_type_binding}
                  onChange={(event) =>
                    setPolicy({ ...policy, node_type_binding: event.target.value })
                  }
                />
              </Field>
            </div>
            <p className="mt-3 text-xs text-gray-500 dark:text-gray-400">
              Renewal starts before sandbox admission closes. The extension horizon must exceed the
              renewal lead time.
            </p>
          </details>
          {allocation.policy.enabled && !policy.enabled && (
            <p className="text-xs text-amber-800 dark:text-amber-300">
              Requests already submitted will still complete.
            </p>
          )}
          {revision !== allocation.revision && (
            <div className="space-y-2 text-sm text-amber-800 dark:text-amber-300">
              <p>The resource changed since this form was opened. Your edits are preserved.</p>
              <button
                type="button"
                className={buttonClass}
                onClick={() => {
                  setRevision(allocation.revision);
                  setPolicy(allocation.policy);
                  setNodeTypes(allocation.policy.node_types.join(', '));
                  setError(null);
                }}
              >
                Discard edits and load current policy
              </button>
            </div>
          )}
          <button className={primaryClass} disabled={busy !== null} type="submit">
            Save renewal policy
          </button>
        </form>
      )}

      {(section === 'all' || section === 'lease') && allows('extend') && (
        <details className="border-t border-gray-200 pt-4 dark:border-white/10">
          <summary className="cursor-pointer text-sm font-medium">Extend now</summary>
          <form
            className="space-y-3 border-t border-gray-200 pt-4 dark:border-white/10"
            onSubmit={(event) => {
              event.preventDefault();
              void intent('extend');
            }}
          >
            <Field label="Requested expiry (your local time)">
              <input
                className={inputClass}
                type="datetime-local"
                required
                value={target}
                onChange={(event) => {
                  setTarget(event.target.value);
                  delete requestIds.current.extend;
                }}
              />
            </Field>
            <Field label="Extension reason">
              <textarea
                className={inputClass}
                maxLength={2000}
                required
                value={reason}
                onChange={(event) => {
                  setReason(event.target.value);
                  delete requestIds.current.extend;
                }}
              />
            </Field>
            <button className={buttonClass} type="submit" disabled={busy !== null}>
              Request extension
            </button>
          </form>
        </details>
      )}

      {(section === 'all' || section === 'lease') && allows('rotate') && (
        <details className="border-t border-gray-200 pt-4 dark:border-white/10">
          <summary className="cursor-pointer text-sm font-medium">Replace node now</summary>
          <div className="mt-4 space-y-3">
            <p className="text-sm">
              Moves this server&apos;s Host, with every session and sandbox, to a newly allocated
              node of the first available type, then releases this node. New work queues while
              running turns finish and the server moves.
            </p>
            <button
              type="button"
              className={buttonClass}
              disabled={busy !== null}
              onClick={() => void intent('rotate')}
            >
              Start replacement
            </button>
          </div>
        </details>
      )}

      {(section === 'all' || section === 'retire') && allows('retire') && (
        <details
          open={section === 'retire'}
          className="border-t border-gray-200 pt-4 dark:border-white/10"
        >
          <summary className="cursor-pointer text-sm font-medium text-red-600 dark:text-red-400">
            {allocation.provider_id === 'existing_ssh'
              ? 'Disconnect server'
              : 'Release cloud resource'}
          </summary>
          <div className="mt-4 space-y-3">
            <p className="text-sm">
              {allocation.provider_id === 'existing_ssh'
                ? 'Disconnects this server. The machine is not deleted.'
                : 'Releases the entire cloud resource and every node in it.'}{' '}
              Existing sandboxes must be cleared first.
            </p>
            <button
              type="button"
              className={buttonClass}
              disabled={busy !== null}
              onClick={async () => {
                setBusy('preview');
                setError(null);
                try {
                  setImpact(await getRetirementImpact(allocation.id));
                  delete requestIds.current.retire;
                } catch (error) {
                  setError(getErrorMessage(error));
                } finally {
                  setBusy(null);
                }
              }}
            >
              Preview retirement impact
            </button>
            {impact && (
              <div className="space-y-2 rounded-lg border border-amber-300 p-3 text-sm dark:border-amber-700">
                <p>
                  {impact.nodes.length} nodes · {impact.hosts.length} Host environments
                </p>
                <p>
                  {impact.active_attempts} running attempts · {impact.retained_groups} retained
                  sandbox groups
                </p>
                <p>Affected Hosts: {impact.hosts.join(', ') || 'None registered'}</p>
                {impact.global_active_host && (
                  <p>Global sandbox location refers to {impact.global_active_host}.</p>
                )}
                <Reasons reasons={impact.blocked_reasons} />
                <p>
                  Admission closes when retirement starts. The server rechecks dependencies before
                  release.
                </p>
                <button
                  className={buttonClass}
                  type="button"
                  disabled={busy !== null || !impact.allowed_actions.includes('retire')}
                  onClick={() => void intent('retire')}
                >
                  Start retirement for entire resource
                </button>
              </div>
            )}
          </div>
        </details>
      )}
      {notice && (
        <p role="status" className="text-sm">
          {notice}
        </p>
      )}
      <ErrorNotice error={error} />
    </div>
  );
}

const ROTATION_STATES: Record<string, string> = {
  pending: 'queued',
  running: 'in progress',
  waiting: 'in progress',
  blocked: 'needs attention',
  unknown: 'needs attention',
  failed: 'failed',
  succeeded: 'completed',
  cancelled: 'cancelled',
};

function RotationSummary({ rotation }: { rotation: ComputeRotation }) {
  const { last } = rotation;
  if (!rotation.enabled && !last) return null;
  return (
    <div className="mt-3 space-y-1 text-sm text-gray-500 dark:text-gray-400">
      {rotation.enabled && rotation.decide_at && (
        <p>Renewed or moved to a new node from {absoluteTime(rotation.decide_at)}</p>
      )}
      {rotation.enabled && rotation.replace_after && (
        <p>Moved to a new node after {absoluteTime(rotation.replace_after)}</p>
      )}
      {last && (
        <p>
          Last move {ROTATION_STATES[last.state] ?? last.state}
          {last.message ? ` · ${last.message}` : ''}
        </p>
      )}
    </div>
  );
}

function PolicyNumber({
  field,
  label,
  policy,
  onChange,
}: {
  field:
    | 'renew_before_seconds'
    | 'extend_to_horizon_seconds'
    | 'stop_new_groups_before_seconds'
    | 'stop_claims_before_seconds';
  label: string;
  policy: RenewalPolicy;
  onChange: (policy: RenewalPolicy) => void;
}) {
  return (
    <Field label={label}>
      <input
        className={inputClass}
        type="number"
        min={0}
        step={1}
        required
        value={policy[field] / 60}
        onChange={(event) => onChange({ ...policy, [field]: Number(event.target.value) * 60 })}
      />
    </Field>
  );
}
