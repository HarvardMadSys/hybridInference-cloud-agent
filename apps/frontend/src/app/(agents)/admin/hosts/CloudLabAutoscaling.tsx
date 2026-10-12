'use client';

import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';

import {
  getCloudLabAutoscaling,
  getCloudLabPreferredNodes,
  updateCloudLabAutoscaling,
  type CloudLabAutoscalingMember,
  type CloudLabAutoscalingPolicy,
  type CloudLabAutoscalingSnapshot,
  type CloudLabPreferredNodesSnapshot,
  type ComputeCatalog,
  type ComputeConnection,
  type RenewalPolicy,
} from '@/lib/api/compute';
import { APIError, getErrorMessage } from '@/lib/utils/errors';

import {
  absoluteTime,
  buttonClass,
  ErrorNotice,
  Field,
  inputClass,
  primaryClass,
  Reasons,
} from './compute-ui';

interface Draft {
  policy: CloudLabAutoscalingPolicy;
  bindings: string;
}

const READY_STATES = new Set(['active', 'online', 'ready']);
const PROVISIONING_STATES = new Set([
  'acquiring',
  'deploying',
  'enrolling',
  'pending',
  'provisioning',
]);
const RETIRING_STATES = new Set(['releasing', 'retiring']);

function asDraft(policy: CloudLabAutoscalingPolicy): Draft {
  return {
    policy,
    bindings: JSON.stringify(policy.configuration.bindings ?? {}, null, 2),
  };
}

function signature(draft: Draft): string {
  return JSON.stringify(draft);
}

function parseBindings(value: string): Record<string, unknown> {
  const parsed = JSON.parse(value) as unknown;
  if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') {
    throw new Error('Profile parameters must be a JSON object.');
  }
  return parsed as Record<string, unknown>;
}

function bindingsHaveValues(value: string): boolean {
  try {
    return Object.keys(parseBindings(value)).length > 0;
  } catch {
    return value.trim().length > 0;
  }
}

function selectedNodeType(value: string): string | null {
  try {
    const selected = parseBindings(value).phystype;
    return typeof selected === 'string' ? selected : null;
  } catch {
    return null;
  }
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="space-y-4 border-t border-border pt-5" aria-label={title}>
      <h3 className="text-sm font-semibold">{title}</h3>
      {children}
    </section>
  );
}

function Count({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="rounded-xl border border-border bg-surface px-3 py-3">
      <dt className="text-xs text-muted">{label}</dt>
      <dd className="mt-1 text-lg font-semibold">{value}</dd>
    </div>
  );
}

function NumericField({
  label,
  value,
  min,
  max,
  step,
  onChange,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  step?: number;
  onChange: (value: number) => void;
}) {
  return (
    <Field label={label}>
      <input
        className={inputClass}
        type="number"
        required
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(event) => onChange(Number(event.target.value))}
      />
    </Field>
  );
}

function memberSlots(
  member: CloudLabAutoscalingMember,
  snapshot: CloudLabAutoscalingSnapshot,
): string {
  const status = snapshot.runtime.slot_status.find(
    (candidate) => candidate.allocation_id === member.allocation_id,
  );
  const applied = status?.applied_slots ?? member.slots_total;
  const desired = status?.desired_slots ?? member.slots_desired;
  if (applied != null && desired != null) {
    return `${applied} applied / ${desired} desired`;
  }
  if (applied != null) return `${applied} applied`;
  if (desired != null) return `${desired} desired`;
  return 'Awaiting capacity report';
}

function formatDecision(decision: Record<string, unknown>): string {
  const action = typeof decision.action === 'string' ? decision.action : '';
  const detail =
    typeof decision.reason === 'string'
      ? decision.reason
      : typeof decision.message === 'string'
        ? decision.message
        : '';
  if (action && detail) return `${action} · ${detail}`;
  if (action || detail) return action || detail;
  return Object.keys(decision).length ? JSON.stringify(decision) : 'No decision yet';
}

function renewalPolicyError(policy: RenewalPolicy): string | null {
  if (
    !(
      policy.extend_to_horizon_seconds > policy.renew_before_seconds &&
      policy.renew_before_seconds > policy.stop_new_groups_before_seconds &&
      policy.stop_new_groups_before_seconds >= policy.stop_claims_before_seconds
    )
  ) {
    return 'Lease timing must satisfy horizon > renewal lead > stop new sandboxes ≥ stop new turns.';
  }
  if (policy.enabled && !policy.reason.trim()) {
    return 'Enter a reason before enabling automatic lease renewal.';
  }
  return null;
}

export default function CloudLabAutoscaling({
  catalog,
  connections,
  activeHost,
}: {
  catalog: ComputeCatalog;
  connections: ComputeConnection[];
  activeHost: string | null;
}) {
  const [snapshot, setSnapshot] = useState<CloudLabAutoscalingSnapshot | null>(null);
  const [availability, setAvailability] = useState<CloudLabPreferredNodesSnapshot | null>(null);
  const [availabilityLoading, setAvailabilityLoading] = useState(true);
  const [availabilityError, setAvailabilityError] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [conflict, setConflict] = useState(false);
  const mounted = useRef(true);
  const draftRef = useRef<Draft | null>(null);
  const baseline = useRef<string | null>(null);
  const loadGeneration = useRef(0);
  const availabilityGeneration = useRef(0);
  const savingRef = useRef(false);

  const updateDraft = useCallback((change: (current: Draft) => Draft) => {
    const current = draftRef.current;
    if (current === null) return;
    const next = change(current);
    draftRef.current = next;
    setDraft(next);
  }, []);

  const acceptSnapshot = useCallback((next: CloudLabAutoscalingSnapshot, replaceDraft: boolean) => {
    setSnapshot(next);
    const current = draftRef.current;
    const unchanged = current !== null && signature(current) === baseline.current;
    if (replaceDraft || current === null || unchanged) {
      const nextDraft = asDraft(next.policy);
      draftRef.current = nextDraft;
      baseline.current = signature(nextDraft);
      setDraft(nextDraft);
      setConflict(false);
    }
  }, []);

  const load = useCallback(
    async (replaceDraft: boolean, signal?: AbortSignal) => {
      const generation = ++loadGeneration.current;
      try {
        const next = await getCloudLabAutoscaling(signal);
        if (!mounted.current || generation !== loadGeneration.current) return;
        acceptSnapshot(next, replaceDraft);
        setError(null);
      } catch (nextError) {
        if (
          !mounted.current ||
          generation !== loadGeneration.current ||
          (nextError instanceof DOMException && nextError.name === 'AbortError')
        )
          return;
        setError(getErrorMessage(nextError));
      } finally {
        if (mounted.current && generation === loadGeneration.current) setLoading(false);
      }
    },
    [acceptSnapshot],
  );

  const loadAvailability = useCallback(async (signal?: AbortSignal) => {
    const generation = ++availabilityGeneration.current;
    try {
      const next = await getCloudLabPreferredNodes(signal);
      if (!mounted.current || generation !== availabilityGeneration.current) return;
      setAvailability(next);
      setAvailabilityError(null);
    } catch (nextError) {
      if (
        !mounted.current ||
        generation !== availabilityGeneration.current ||
        (nextError instanceof DOMException && nextError.name === 'AbortError')
      )
        return;
      setAvailability(null);
      setAvailabilityError(getErrorMessage(nextError));
    } finally {
      if (mounted.current && generation === availabilityGeneration.current) {
        setAvailabilityLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    const controller = new AbortController();
    void load(false, controller.signal);
    const timer = setInterval(() => {
      if (!savingRef.current) void load(false);
    }, 10_000);
    return () => {
      mounted.current = false;
      controller.abort();
      clearInterval(timer);
    };
  }, [load]);

  useEffect(() => {
    const controller = new AbortController();
    void loadAvailability(controller.signal);
    const timer = setInterval(() => void loadAvailability(), 60_000);
    return () => {
      controller.abort();
      clearInterval(timer);
    };
  }, [loadAvailability]);

  const changePolicy = (
    change: (policy: CloudLabAutoscalingPolicy) => CloudLabAutoscalingPolicy,
  ) => {
    updateDraft((current) => ({ ...current, policy: change(current.policy) }));
    setSaved(false);
    setError(null);
  };
  const changeConfiguration = (
    change: (
      configuration: CloudLabAutoscalingPolicy['configuration'],
    ) => CloudLabAutoscalingPolicy['configuration'],
  ) =>
    changePolicy((policy) => ({
      ...policy,
      configuration: change(policy.configuration),
    }));
  const changeRenewalPolicy = (change: (renewal: RenewalPolicy) => RenewalPolicy) =>
    changePolicy((policy) => ({
      ...policy,
      renewal_policy: {
        ...change(policy.renewal_policy),
        rotation_enabled: false,
      },
    }));

  const changeRenewalMinutes = (
    field:
      | 'renew_before_seconds'
      | 'extend_to_horizon_seconds'
      | 'stop_new_groups_before_seconds'
      | 'stop_claims_before_seconds',
    minutes: number,
  ) => changeRenewalPolicy((renewal) => ({ ...renewal, [field]: minutes * 60 }));

  const accountConnections = connections.filter(
    (connection) => connection.provider_id === 'cloudlab',
  );
  const sshConnections = connections.filter(
    (connection) => connection.provider_id === 'existing_ssh',
  );
  const profileChoices = useMemo(() => {
    const configured = new Map(catalog.deployment_profiles.map((profile) => [profile.id, profile]));
    for (const id of draft?.policy.deployment_profiles ?? []) {
      if (!configured.has(id)) configured.set(id, { id, label: `${id} (not available)` });
    }
    return [...configured.values()];
  }, [catalog.deployment_profiles, draft?.policy.deployment_profiles]);

  if (loading && !draft) {
    return <p role="status">Loading CloudLab auto-scaling settings…</p>;
  }
  if (!draft || !snapshot) {
    return (
      <div>
        <ErrorNotice error={error ?? 'Auto-scaling settings are unavailable.'} />
        <button className={buttonClass} type="button" onClick={() => void load(true)}>
          Try again
        </button>
      </div>
    );
  }

  const { policy } = draft;
  const bindingsDraft = draft.bindings;
  const account = connections.find((connection) => connection.id === policy.connection_id);
  const ssh = connections.find((connection) => connection.id === policy.ssh_connection_id);
  const isPausing = snapshot.policy.enabled && !policy.enabled;
  const configurationStarted = Boolean(
    policy.enabled ||
    policy.max_hosts ||
    policy.connection_id ||
    policy.ssh_connection_id ||
    policy.deployment_profiles.length ||
    Object.entries(policy.configuration).some(
      ([key, value]) => key !== 'bindings' && value !== '' && value !== null && value !== undefined,
    ) ||
    bindingsHaveValues(bindingsDraft),
  );
  const requiredProfiles = policy.max_hosts;
  const configuredProfiles = new Set(catalog.deployment_profiles.map((profile) => profile.id));
  const unavailableProfiles = policy.deployment_profiles.filter(
    (profile) => !configuredProfiles.has(profile),
  );
  const availableProfileCount = policy.deployment_profiles.length - unavailableProfiles.length;
  const configurationBlockers: string[] = [];
  const policyBlockers: string[] = [];
  if (policy.renewal_policy.rotation_enabled) {
    policyBlockers.push('Lease rotation is not available for auto-scaled members yet.');
  }
  const renewalError = renewalPolicyError(policy.renewal_policy);
  if (renewalError) {
    policyBlockers.push(renewalError);
  }
  if (unavailableProfiles.length) {
    configurationBlockers.push(
      `Remove unavailable deployment profiles: ${unavailableProfiles.join(', ')}.`,
    );
  }
  if (availableProfileCount < requiredProfiles) {
    configurationBlockers.push(
      `Select at least ${requiredProfiles} available deployment profiles for ${policy.max_hosts} Hosts.`,
    );
  }
  if (configurationStarted && account?.status !== 'verified') {
    configurationBlockers.push('Select a verified CloudLab account connection.');
  }
  if (configurationStarted && ssh?.status !== 'verified') {
    configurationBlockers.push('Select a verified SSH key connection.');
  }
  const saveBlockers = [...policyBlockers, ...(isPausing ? [] : configurationBlockers)];
  const enableBlockers = [...policyBlockers, ...configurationBlockers];
  if (policy.max_hosts < 1) enableBlockers.push('Set Maximum Hosts to at least 1.');
  if (activeHost) {
    enableBlockers.push(
      'Placement is pinned to one Host. Select Automatic placement before enabling auto-scaling.',
    );
  }
  if (!configurationStarted && account?.status !== 'verified') {
    enableBlockers.push('Select a verified CloudLab account connection.');
  }
  if (!configurationStarted && ssh?.status !== 'verified') {
    enableBlockers.push('Select a verified SSH key connection.');
  }
  const counts = {
    ready: snapshot.members.filter((member) => READY_STATES.has(member.state)).length,
    provisioning: snapshot.members.filter((member) => PROVISIONING_STATES.has(member.state)).length,
    retiring: snapshot.members.filter((member) => RETIRING_STATES.has(member.state)).length,
  };
  const slots = snapshot.runtime.slot_status.reduce(
    (total, status) => total + (status.applied_slots ?? 0),
    0,
  );
  const blockedReasons = snapshot.runtime.slot_status
    .filter((status) => status.blocker_code)
    .map((status) => ({
      code: status.blocker_code!,
      message: `Slot update for ${status.host ?? status.allocation_id} is blocked (${status.blocker_code}).`,
    }));
  if (snapshot.runtime.blocker_message) {
    blockedReasons.unshift({
      code: snapshot.runtime.blocker_code ?? 'autoscaling_blocked',
      message: snapshot.runtime.blocker_message,
    });
  }
  const dirty = signature(draft) !== baseline.current;
  const profileSet = new Set(policy.deployment_profiles);
  const selectedType = selectedNodeType(bindingsDraft);

  function choosePreferredNode(nodeType: string) {
    try {
      const bindings = parseBindings(bindingsDraft);
      const nextBindings = JSON.stringify({ ...bindings, phystype: nodeType }, null, 2);
      updateDraft((current) => ({ ...current, bindings: nextBindings }));
      setSaved(false);
      setError(null);
    } catch {
      setError('Fix Profile parameters JSON before selecting a preferred node type.');
    }
  }

  async function submit() {
    if (savingRef.current) return;
    savingRef.current = true;
    loadGeneration.current += 1;
    setSaving(true);
    setSaved(false);
    setError(null);
    setConflict(false);
    try {
      if (policy.min_hosts > policy.max_hosts) {
        throw new Error('Minimum Hosts cannot be greater than maximum Hosts.');
      }
      if (saveBlockers.length) throw new Error(saveBlockers[0]);
      if (policy.enabled && enableBlockers.length) throw new Error(enableBlockers[0]);
      const parsed = parseBindings(bindingsDraft);
      const { revision, ...writePolicy } = policy;
      let configuration: CloudLabAutoscalingPolicy['configuration'] = {};
      if (configurationStarted) {
        const cleanedConfiguration = { ...writePolicy.configuration };
        for (const key of [
          'project',
          'group',
          'profile_project',
          'profile_name',
          'ssh_username',
        ] as const) {
          if (cleanedConfiguration[key] === '') delete cleanedConfiguration[key];
        }
        configuration = {
          ...cleanedConfiguration,
          bindings: parsed,
          host_key_source: 'registration',
        };
      }
      const next = await updateCloudLabAutoscaling({
        expected_revision: revision,
        policy: {
          ...writePolicy,
          sandbox_storage_path: writePolicy.sandbox_storage_path?.trim() || null,
          renewal_policy: {
            ...writePolicy.renewal_policy,
            rotation_enabled: false,
          },
          configuration,
        },
      });
      if (!mounted.current) return;
      acceptSnapshot(next, true);
      setSaved(true);
    } catch (nextError) {
      if (nextError instanceof APIError && nextError.statusCode === 409) {
        setConflict(nextError.code === 'revision_conflict');
      }
      setError(getErrorMessage(nextError));
    } finally {
      savingRef.current = false;
      if (mounted.current) setSaving(false);
    }
  }

  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <fieldset className="m-0 min-w-0 space-y-6 border-0 p-0" disabled={saving}>
        <div className="rounded-xl border border-border bg-surface p-4 text-sm leading-6">
          <p>
            Manual servers stay independent from this pool. Turning auto-scaling off only pauses new
            decisions; it does not release experiments or change manually allocated servers.
          </p>
          <p className="mt-2 text-muted">
            CloudLab nodes use first-boot host-key registration. Each concurrent Host needs its own
            deployment profile. Lease rotation is not available for auto-scaled members in this
            version.
          </p>
        </div>

        <section aria-label="Auto-scaling status" className="space-y-4">
          <div className="flex items-center justify-between gap-3">
            <h3 className="text-sm font-semibold">Current status</h3>
            <span className="text-xs text-muted">
              Observed {absoluteTime(snapshot.runtime.observed_at)}
            </span>
          </div>
          <dl className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            <Count label="Ready" value={counts.ready} />
            <Count label="Provisioning" value={counts.provisioning} />
            <Count label="Retiring" value={counts.retiring} />
            <Count label="Applied slots" value={slots} />
          </dl>
          <dl className="space-y-2 text-sm">
            <div className="flex justify-between gap-4">
              <dt className="text-muted">Queue pressure</dt>
              <dd className="text-right">
                {snapshot.runtime.queue_pressure} waiting
                {snapshot.runtime.pressure_since
                  ? ` · since ${absoluteTime(snapshot.runtime.pressure_since)}`
                  : ''}
              </dd>
            </div>
            <div className="flex justify-between gap-4">
              <dt className="text-muted">Last decision</dt>
              <dd className="text-right">
                {formatDecision(snapshot.runtime.last_decision)}
                {snapshot.runtime.last_decision_at
                  ? ` · ${absoluteTime(snapshot.runtime.last_decision_at)}`
                  : ''}
              </dd>
            </div>
            <div className="flex justify-between gap-4">
              <dt className="text-muted">Cooldown</dt>
              <dd className="text-right">
                {snapshot.runtime.cooldown_until
                  ? `Until ${absoluteTime(snapshot.runtime.cooldown_until)}`
                  : 'Inactive'}
              </dd>
            </div>
          </dl>
          <Reasons reasons={blockedReasons} />
          {snapshot.members.length > 0 && (
            <ul
              className="divide-y divide-border rounded-xl border border-border"
              aria-label="Pool members"
            >
              {snapshot.members.map((member) => (
                <li key={member.allocation_id} className="px-3 py-3 text-sm">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span className="font-medium">{member.host ?? member.allocation_id}</span>
                    <span>{member.state}</span>
                  </div>
                  <p className="mt-1 text-xs text-muted">
                    {member.deployment_profile} · {memberSlots(member, snapshot)}
                  </p>
                  {member.latest_operation_message && (
                    <p className="mt-1 text-xs text-muted">{member.latest_operation_message}</p>
                  )}
                </li>
              ))}
            </ul>
          )}
        </section>

        <Section title="Pool size">
          <label className="flex items-start gap-3 text-sm">
            <input
              className="mt-1 h-4 w-4"
              type="checkbox"
              checked={policy.enabled}
              disabled={!policy.enabled && enableBlockers.length > 0}
              onChange={(event) =>
                changePolicy((current) => ({ ...current, enabled: event.target.checked }))
              }
            />
            <span>
              <span className="block font-medium">Enable CloudLab auto-scaling</span>
              <span className="mt-1 block text-xs text-muted">
                The control plane creates pool experiments only after sustained queue pressure.
              </span>
            </span>
          </label>
          {enableBlockers.length > 0 && (
            <ul className="space-y-1 rounded-xl border border-status-warning/20 bg-status-warning/5 p-4 text-sm text-status-warning">
              {enableBlockers.map((blocker) => (
                <li key={blocker}>{blocker}</li>
              ))}
            </ul>
          )}
          <div className="grid grid-cols-2 gap-4">
            <NumericField
              label="Minimum Hosts"
              value={policy.min_hosts}
              min={0}
              max={8}
              onChange={(value) => changePolicy((current) => ({ ...current, min_hosts: value }))}
            />
            <NumericField
              label="Maximum Hosts"
              value={policy.max_hosts}
              min={0}
              max={8}
              onChange={(value) => changePolicy((current) => ({ ...current, max_hosts: value }))}
            />
          </div>
        </Section>

        <Section title="Credentials and deployment profiles">
          <Field label="Verified CloudLab account">
            <select
              className={inputClass}
              required={configurationStarted && !isPausing}
              value={policy.connection_id ?? ''}
              onChange={(event) =>
                changePolicy((current) => ({
                  ...current,
                  connection_id: event.target.value || null,
                }))
              }
            >
              <option value="">Select…</option>
              {accountConnections.map((connection) => (
                <option
                  key={connection.id}
                  value={connection.id}
                  disabled={connection.status !== 'verified'}
                >
                  {connection.name}
                  {connection.status === 'verified' ? '' : ' (not verified)'}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Verified SSH key">
            <select
              className={inputClass}
              required={configurationStarted && !isPausing}
              value={policy.ssh_connection_id ?? ''}
              onChange={(event) =>
                changePolicy((current) => ({
                  ...current,
                  ssh_connection_id: event.target.value || null,
                }))
              }
            >
              <option value="">Select…</option>
              {sshConnections.map((connection) => (
                <option
                  key={connection.id}
                  value={connection.id}
                  disabled={connection.status !== 'verified'}
                >
                  {connection.name}
                  {connection.status === 'verified' ? '' : ' (not verified)'}
                </option>
              ))}
            </select>
          </Field>
          <fieldset className="space-y-2">
            <legend className="text-sm font-medium">Deployment profiles</legend>
            <p className="text-xs text-muted">
              {policy.deployment_profiles.length} selected; {requiredProfiles} required at this
              maximum.
            </p>
            <div className="grid gap-2 sm:grid-cols-2">
              {profileChoices.map((profile) => (
                <label
                  key={profile.id}
                  className="flex min-h-11 items-center gap-3 rounded-xl border border-border px-3 py-2 text-sm"
                >
                  <input
                    type="checkbox"
                    disabled={!profileSet.has(profile.id) && policy.deployment_profiles.length >= 8}
                    checked={profileSet.has(profile.id)}
                    onChange={(event) =>
                      changePolicy((current) => ({
                        ...current,
                        deployment_profiles: event.target.checked
                          ? [...current.deployment_profiles, profile.id]
                          : current.deployment_profiles.filter((id) => id !== profile.id),
                      }))
                    }
                  />
                  <span>{profile.label}</span>
                </label>
              ))}
            </div>
          </fieldset>
          {snapshot.profile_reservations.length > 0 && (
            <p className="text-xs text-muted">
              Reserved now:{' '}
              {snapshot.profile_reservations
                .map((reservation) => reservation.deployment_profile)
                .join(', ')}
            </p>
          )}
        </Section>

        <Section title="CloudLab experiment">
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="CloudLab project">
              <input
                className={inputClass}
                required={configurationStarted && !isPausing}
                maxLength={128}
                value={policy.configuration.project ?? ''}
                onChange={(event) =>
                  changeConfiguration((current) => ({ ...current, project: event.target.value }))
                }
              />
            </Field>
            <Field label="Project group (optional)">
              <input
                className={inputClass}
                maxLength={128}
                value={policy.configuration.group ?? ''}
                onChange={(event) =>
                  changeConfiguration((current) => ({ ...current, group: event.target.value }))
                }
              />
            </Field>
            <Field label="Template project">
              <input
                className={inputClass}
                required={configurationStarted && !isPausing}
                maxLength={128}
                value={policy.configuration.profile_project ?? ''}
                onChange={(event) =>
                  changeConfiguration((current) => ({
                    ...current,
                    profile_project: event.target.value,
                  }))
                }
              />
            </Field>
            <Field label="Template name">
              <input
                className={inputClass}
                required={configurationStarted && !isPausing}
                maxLength={128}
                value={policy.configuration.profile_name ?? ''}
                onChange={(event) =>
                  changeConfiguration((current) => ({
                    ...current,
                    profile_name: event.target.value,
                  }))
                }
              />
            </Field>
            <NumericField
              label="Initial duration (hours)"
              value={policy.configuration.duration ?? 4}
              min={1}
              max={168}
              onChange={(value) =>
                changeConfiguration((current) => ({ ...current, duration: value }))
              }
            />
            <Field label="CloudLab SSH user">
              <input
                className={inputClass}
                required={configurationStarted && !isPausing}
                maxLength={128}
                autoComplete="off"
                value={policy.configuration.ssh_username ?? ''}
                onChange={(event) =>
                  changeConfiguration((current) => ({
                    ...current,
                    ssh_username: event.target.value,
                  }))
                }
              />
            </Field>
            <Field label="Sandbox storage path (optional)">
              <input
                className={inputClass}
                maxLength={4096}
                autoComplete="off"
                value={policy.sandbox_storage_path ?? ''}
                onChange={(event) =>
                  changePolicy((current) => ({
                    ...current,
                    sandbox_storage_path: event.target.value || null,
                  }))
                }
              />
            </Field>
          </div>
          <section
            className="space-y-3 rounded-xl border border-border bg-surface p-3"
            aria-label="Preferred CloudLab node types"
          >
            <div className="flex flex-wrap items-start justify-between gap-2">
              <div>
                <h4 className="text-sm font-medium">Preferred node types</h4>
                <p className="mt-1 text-xs text-muted">
                  Modern general-purpose nodes with at least 16 physical cores and 128 GiB RAM; only
                  types with more than 16 currently free are shown.
                </p>
              </div>
              {availability && (
                <span className="text-xs text-muted">
                  Observed {absoluteTime(availability.observed_at)}
                </span>
              )}
            </div>
            {availabilityLoading && !availability && (
              <p role="status" className="text-sm text-muted">
                Loading preferred-node availability…
              </p>
            )}
            {availabilityError && (
              <p role="status" className="text-sm text-status-warning">
                Preferred-node availability is temporarily unavailable. Existing settings are
                unaffected.
              </p>
            )}
            {availability && availability.nodes.length === 0 && (
              <p className="text-sm text-muted">
                No node types currently pass the preferred-node filters.
              </p>
            )}
            {availability && availability.nodes.length > 0 && (
              <ul className="divide-y divide-border" aria-label="Preferred node availability">
                {availability.nodes.map((node) => (
                  <li
                    key={`${node.site}:${node.node_type}`}
                    className="flex flex-wrap items-center justify-between gap-3 py-3 first:pt-0 last:pb-0"
                  >
                    <div>
                      <p className="text-sm font-medium">
                        {node.node_type} · {node.available} free of {node.total}
                      </p>
                      <p className="text-xs text-muted">
                        {node.site} · {node.physical_cores} cores / {node.logical_threads} threads ·{' '}
                        {node.memory_gib} GiB RAM · {node.introduced_year}
                      </p>
                    </div>
                    <button
                      className={buttonClass}
                      type="button"
                      aria-label={`Use ${node.node_type}`}
                      aria-pressed={selectedType === node.node_type}
                      onClick={() => choosePreferredNode(node.node_type)}
                    >
                      {selectedType === node.node_type ? 'Selected' : 'Use'}
                    </button>
                  </li>
                ))}
              </ul>
            )}
            <p className="text-xs text-muted">
              Live counts from{' '}
              <a
                className="underline"
                href="https://www.cloudlab.us/portal-hardware.php"
                target="_blank"
                rel="noreferrer"
              >
                CloudLab public hardware inventory
              </a>
              .
            </p>
          </section>
          <Field label="Profile parameters (JSON object)">
            <textarea
              className={inputClass}
              rows={4}
              spellCheck={false}
              value={draft.bindings}
              onChange={(event) => {
                updateDraft((current) => ({ ...current, bindings: event.target.value }));
                setSaved(false);
                setError(null);
              }}
            />
          </Field>
          <div className="rounded-xl border border-border bg-surface p-3 text-sm">
            <span className="font-medium">SSH host-key checking</span>
            <span className="mt-1 block text-muted">
              Registered by each node at first boot (required)
            </span>
          </div>
        </Section>

        <Section title="Lease renewal">
          <label className="flex items-start gap-3 text-sm">
            <input
              className="mt-1 h-4 w-4"
              type="checkbox"
              checked={policy.renewal_policy.enabled}
              onChange={(event) =>
                changeRenewalPolicy((renewal) => ({
                  ...renewal,
                  enabled: event.target.checked,
                }))
              }
            />
            <span>
              <span className="block font-medium">Request renewal automatically</span>
              <span className="mt-1 block text-xs text-muted">
                Extend controller-owned CloudLab experiments before their leases expire.
              </span>
            </span>
          </label>
          <Field label="Renewal reason">
            <textarea
              className={inputClass}
              required={policy.renewal_policy.enabled}
              maxLength={2000}
              rows={2}
              value={policy.renewal_policy.reason}
              onChange={(event) =>
                changeRenewalPolicy((renewal) => ({
                  ...renewal,
                  reason: event.target.value,
                }))
              }
            />
          </Field>
          <div className="grid gap-4 sm:grid-cols-2">
            <NumericField
              label="Request before expiry (minutes)"
              value={policy.renewal_policy.renew_before_seconds / 60}
              min={1}
              max={10080}
              step={1}
              onChange={(value) => changeRenewalMinutes('renew_before_seconds', value)}
            />
            <NumericField
              label="Extend ahead of now (minutes)"
              value={policy.renewal_policy.extend_to_horizon_seconds / 60}
              min={2}
              max={43200}
              step={1}
              onChange={(value) => changeRenewalMinutes('extend_to_horizon_seconds', value)}
            />
            <NumericField
              label="Stop new sandboxes before expiry (minutes)"
              value={policy.renewal_policy.stop_new_groups_before_seconds / 60}
              min={0}
              max={10080}
              step={1}
              onChange={(value) => changeRenewalMinutes('stop_new_groups_before_seconds', value)}
            />
            <NumericField
              label="Stop all new turns before expiry (minutes)"
              value={policy.renewal_policy.stop_claims_before_seconds / 60}
              min={0}
              max={10080}
              step={1}
              onChange={(value) => changeRenewalMinutes('stop_claims_before_seconds', value)}
            />
          </div>
          <p className="text-xs text-muted">
            The extension horizon must be greater than the renewal lead, which must be greater than
            the stop-new-sandboxes lead. The stop-new-turns lead may be equal to or smaller than the
            stop-new-sandboxes lead.
          </p>
          {renewalError && (
            <p role="alert" className="text-sm text-status-warning">
              {renewalError}
            </p>
          )}
        </Section>

        <Section title="Scale timing">
          <div className="grid gap-4 sm:grid-cols-3">
            <NumericField
              label="Scale-up sustain (seconds)"
              value={policy.scale_up_sustain_seconds}
              min={0}
              max={86400}
              onChange={(value) =>
                changePolicy((current) => ({ ...current, scale_up_sustain_seconds: value }))
              }
            />
            <NumericField
              label="Decision cooldown (seconds)"
              value={policy.cooldown_seconds}
              min={0}
              max={86400}
              onChange={(value) =>
                changePolicy((current) => ({ ...current, cooldown_seconds: value }))
              }
            />
            <NumericField
              label="Scale-down idle (seconds)"
              value={policy.scale_down_idle_seconds}
              min={60}
              max={604800}
              onChange={(value) =>
                changePolicy((current) => ({ ...current, scale_down_idle_seconds: value }))
              }
            />
          </div>
          <label className="flex items-start gap-3 text-sm">
            <input
              className="mt-1 h-4 w-4"
              type="checkbox"
              checked={policy.scale_down_enabled}
              onChange={(event) =>
                changePolicy((current) => ({
                  ...current,
                  scale_down_enabled: event.target.checked,
                }))
              }
            />
            <span>
              <span className="block font-medium">Allow guarded scale-down</span>
              <span className="mt-1 block text-xs text-muted">
                Only controller-owned, idle members that pass the normal retirement checks are
                eligible. Manual servers are never selected.
              </span>
            </span>
          </label>
        </Section>

        <Section title="Automatic slot sizing">
          <p className="text-sm text-muted">
            Formula v{policy.slot_policy.formula_version}: slots = min(floor(
            {policy.slot_policy.cpu_reservation_fraction} × logical cores ÷{' '}
            {policy.slot_policy.cpu_per_slot}), floor((RAM GiB −{' '}
            {policy.slot_policy.memory_reserve_gib}) ÷ {policy.slot_policy.memory_per_slot_gib}),{' '}
            {policy.slot_policy.max_slots}), with a minimum of {policy.slot_policy.min_slots}.
          </p>
          <p className="text-xs text-muted">
            The controller calculates the result from each fresh Host capacity report; the member
            list above shows desired and applied values.
          </p>
        </Section>

        <ErrorNotice error={error} />
        {conflict && (
          <div className="rounded-xl border border-status-warning/20 bg-status-warning/5 p-4 text-sm">
            <p>
              Your draft is preserved. Refresh from the server to discard it and load the latest
              revision.
            </p>
            <button className={`${buttonClass} mt-3`} type="button" onClick={() => void load(true)}>
              Refresh from server
            </button>
          </div>
        )}
        {saved && (
          <p role="status" className="text-sm text-status-success">
            Auto-scaling settings saved.
          </p>
        )}
        <div className="flex flex-wrap items-center gap-3">
          <button
            className={primaryClass}
            type="submit"
            disabled={
              saving ||
              !dirty ||
              saveBlockers.length > 0 ||
              (policy.enabled && enableBlockers.length > 0)
            }
          >
            {saving ? 'Saving…' : 'Save auto-scaling'}
          </button>
          <span className="text-xs text-muted">Revision {policy.revision}</span>
        </div>
      </fieldset>
    </form>
  );
}
