'use client';

import { useRef, useState } from 'react';

import {
  extendComputeAllocation,
  getComputeOperations,
  getRetirementImpact,
  retireComputeAllocation,
  setComputePolicy,
  type ComputeAllocation,
  type ComputeOperation,
  type RenewalPolicy,
  type RetirementImpact,
} from '@/lib/api/compute';
import { getErrorMessage } from '@/lib/utils/errors';

import {
  absoluteTime,
  buttonClass,
  ErrorNotice,
  Field,
  inputClass,
  primaryClass,
  Reasons,
  remainingTime,
} from './compute-ui';

export default function ComputeResourcePanel({
  allocation,
  onSaved,
  onOperation,
}: {
  allocation: ComputeAllocation;
  onSaved: (allocation: ComputeAllocation) => void;
  onOperation: (operation: ComputeOperation) => void;
}) {
  const [policy, setPolicy] = useState(allocation.policy);
  const [adoptManagement, setAdoptManagement] = useState(false);
  const [revision, setRevision] = useState(allocation.revision);
  const [target, setTarget] = useState('');
  const [reason, setReason] = useState('');
  const [impact, setImpact] = useState<RetirementImpact | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const requestIds = useRef<{ extend?: string; retire?: string }>({});
  const allows = (action: string) => allocation.allowed_actions.includes(action);

  async function intent(kind: 'extend' | 'retire') {
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
      <div className="space-y-2 text-sm">
        <p>
          Resource: {allocation.external_id ?? 'Pending acquisition'} ·{' '}
          {allocation.state === 'absent' ? 'Deregistered (machine still exists)' : allocation.state}
        </p>
        {allocation.expires_at ? (
          <p>
            Confirmed expiry: {absoluteTime(allocation.expires_at)} (
            {remainingTime(allocation.expires_at)})
          </p>
        ) : (
          <p>No managed lease has been observed.</p>
        )}
        <p className="text-gray-500 dark:text-gray-400">
          Provider observation: {absoluteTime(allocation.observed_at)}. The countdown is based on
          this observation; it does not confirm a new extension.
        </p>
        {allocation.next_check_at && <p>Next check: {absoluteTime(allocation.next_check_at)}</p>}
        <p>Nodes and execution environments affected by resource operations:</p>
        <ul className="list-inside list-disc">
          {allocation.nodes.map((node) => (
            <li key={node.id}>
              {node.name} — {node.hosts.join(', ') || 'No Host registered yet'}
            </li>
          ))}
        </ul>
        <Reasons reasons={allocation.blocked_reasons} />
      </div>

      {allows('policy') && (
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
              onSaved(result.allocation);
              setNotice(
                'Renewal policy saved. This does not confirm an extension. Background renewal continues when this page is closed.',
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
              I authorize lifecycle management of this entire allocation and every node listed
              above, including renewal and eventual release after dependency checks.
            </label>
          )}
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={policy.enabled}
              onChange={(event) => setPolicy({ ...policy, enabled: event.target.checked })}
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
          <details>
            <summary className="cursor-pointer text-sm">Admission deadlines</summary>
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
            </div>
          </details>
          <p className="text-xs text-gray-500 dark:text-gray-400">
            Extension horizon must exceed renewal lead time, which must exceed the new sandbox
            deadline. The all-turn deadline must be at or after the new sandbox deadline. Disabling
            automatic renewal does not cancel an already submitted request.
          </p>
          {revision !== allocation.revision && (
            <div className="space-y-2 text-sm text-amber-800 dark:text-amber-300">
              <p>The resource changed since this form was opened. Your edits are preserved.</p>
              <button
                type="button"
                className={buttonClass}
                onClick={() => {
                  setRevision(allocation.revision);
                  setPolicy(allocation.policy);
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

      {allows('extend') && (
        <form
          className="space-y-3 border-t border-gray-200 pt-4 dark:border-white/10"
          onSubmit={(event) => {
            event.preventDefault();
            void intent('extend');
          }}
        >
          <h3 className="font-semibold">Request an extension now</h3>
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
      )}

      {allows('retire') && (
        <div className="space-y-3 border-t border-gray-200 pt-4 dark:border-white/10">
          <h3 className="font-semibold">Retire and release resource</h3>
          <p className="text-sm">
            This affects the entire allocation and all its nodes. Retained sandboxes prevent
            release. Moving the global location does not migrate them.
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
