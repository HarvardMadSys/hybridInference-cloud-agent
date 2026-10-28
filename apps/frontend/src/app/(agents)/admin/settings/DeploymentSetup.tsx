'use client';

import Link from 'next/link';

import type { DeploymentSetupDefinition, DeploymentState } from '@/lib/api/adminSettings';

const REPOSITORY = 'https://github.com/HarvardMadSys/hybridInference-cloud-agent/blob/dev';

/**
 * Fixed application-owned destinations. A backend action target is never a URL
 * and is never followed directly; only names in this table can become a link.
 */
const TARGETS = {
  github_app_private_key: { kind: 'setting', key: 'github_app_private_key' },
  pgadmin_url: { kind: 'setting', key: 'pgadmin_url' },
  compute_setup: { kind: 'internal', href: '/admin/hosts#compute-setup' },
  database: { kind: 'internal', href: '/admin/database' },
  deployment_guide: {
    kind: 'documentation',
    href: `${REPOSITORY}/docs/developer/operations.md#running-self-hosted`,
  },
  key_rotation_guide: {
    kind: 'documentation',
    href: `${REPOSITORY}/docs/developer/operations.md#admin-settings`,
  },
} as const;

type TargetName = keyof typeof TARGETS;

export type ResolvedDeploymentTarget =
  | { kind: 'setting'; key: string }
  | { kind: 'internal'; href: string }
  | { kind: 'documentation'; href: string }
  | { kind: 'unknown' };

export function resolveDeploymentTarget(target: string): ResolvedDeploymentTarget {
  return Object.prototype.hasOwnProperty.call(TARGETS, target)
    ? TARGETS[target as TargetName]
    : { kind: 'unknown' };
}

const STATE_LABELS: Record<DeploymentState, string> = {
  configured: 'Configuration present',
  missing: 'Not configured',
  weak: 'Short credential',
  fallback: 'Using session-secret fallback',
  not_used: 'Not used',
  unknown: 'Not checked',
};

const REQUIREMENT_LABELS = {
  required: 'Required',
  optional: 'Optional',
  conditional: 'Conditional',
} as const;

export function deploymentRowId(definition: DeploymentSetupDefinition): string {
  return definition.id || definition.key;
}

/** Stable semantic hash target for a deployment row. */
export function deploymentTargetId(definition: DeploymentSetupDefinition): string {
  return `deployment-${deploymentRowId(definition)}`;
}

function matchLegacyHash(
  definitions: DeploymentSetupDefinition[],
  fragment: string,
): DeploymentSetupDefinition | undefined {
  const candidates = [fragment];
  try {
    candidates.push(decodeURIComponent(fragment));
  } catch {
    // A malformed percent escape cannot name a legacy row.
  }
  return definitions.find((definition) => candidates.includes(definition.key));
}

/**
 * The legacy anchor (`#application-bootstrap-<key>`, raw or encoded) and the
 * semantic anchor (`#deployment-<id>`) both select the Deployment tab and focus
 * one row after the definitions load.
 */
export function deploymentHashTarget(
  hash: string,
  definitions: DeploymentSetupDefinition[],
): string | null {
  if (hash.startsWith('#deployment-')) {
    const fragment = hash.slice('#deployment-'.length);
    if (!fragment) return null;
    let decoded = fragment;
    try {
      decoded = decodeURIComponent(fragment);
    } catch {
      // Keep the raw fragment.
    }
    const definition =
      definitions.find((item) => deploymentRowId(item) === fragment) ??
      definitions.find((item) => deploymentRowId(item) === decoded) ??
      definitions.find((item) => item.key === fragment || item.key === decoded);
    return definition ? deploymentTargetId(definition) : null;
  }
  if (hash.startsWith('#application-bootstrap-')) {
    const definition = matchLegacyHash(definitions, hash.slice('#application-bootstrap-'.length));
    return definition ? deploymentTargetId(definition) : null;
  }
  return null;
}

function StateBadge({ state }: { state: DeploymentState }) {
  return (
    <span className="inline-flex items-center rounded-full border border-border bg-surface-muted/60 px-2 py-0.5 text-[11px] font-medium text-muted">
      {STATE_LABELS[state]}
    </span>
  );
}

function Action({
  label,
  target,
  recoveryToken,
  onRevealSetting,
}: {
  label: string;
  target: string;
  recoveryToken?: string;
  onRevealSetting?: (key: string) => void;
}) {
  const destination = resolveDeploymentTarget(target);
  if (destination.kind === 'setting') {
    return (
      <button
        type="button"
        onClick={() => onRevealSetting?.(destination.key)}
        className="ui-focus-ring min-h-9 rounded text-sm font-medium text-accent underline underline-offset-4 [@media(pointer:coarse)]:min-h-11"
      >
        {label}
      </button>
    );
  }
  if (destination.kind === 'documentation') {
    return (
      <a
        href={destination.href}
        target="_blank"
        rel="noreferrer noopener"
        className="ui-focus-ring inline-flex min-h-9 items-center rounded text-sm font-medium text-accent underline underline-offset-4 [@media(pointer:coarse)]:min-h-11"
      >
        {label}
        <span className="sr-only"> (opens in a new tab)</span>
      </a>
    );
  }
  if (destination.kind === 'internal') {
    // The recovery token is in-memory only. Navigating to another page would
    // need Gateway sign-in, so explain instead of moving the token into a URL.
    if (recoveryToken) {
      return (
        <span className="text-sm leading-6 text-muted">
          {label} — Gateway sign-in is required to open this page. It is not available from recovery
          setup.
        </span>
      );
    }
    return (
      <Link
        href={destination.href}
        className="ui-focus-ring inline-flex min-h-9 items-center rounded text-sm font-medium text-accent underline underline-offset-4 [@media(pointer:coarse)]:min-h-11"
      >
        {label}
      </Link>
    );
  }
  // An unknown target is never a link; it has no application-owned destination.
  return <span className="text-sm leading-6 text-muted">{label}</span>;
}

export function DeploymentSetup({
  definitions,
  recoveryToken,
  onRevealSetting,
}: {
  definitions: DeploymentSetupDefinition[];
  recoveryToken?: string;
  onRevealSetting?: (key: string) => void;
}) {
  return (
    <div>
      <h2 className="text-lg font-semibold tracking-tight text-foreground">Deployment setup</h2>
      <p className="mt-2 text-sm leading-6 text-muted">
        Manage these items through the linked setting, dedicated page, or deployment procedure.
        Values are hidden here. Status describes configuration, not health or reachability.
      </p>
      {definitions.length === 0 ? (
        <p className="mt-5 rounded-xl bg-surface-muted p-4 text-sm text-muted">
          No deployment items are available.
        </p>
      ) : (
        <dl className="mt-5 space-y-4">
          {definitions.map((definition) => {
            const targetId = deploymentTargetId(definition);
            return (
              <div
                key={definition.key}
                id={targetId}
                tabIndex={-1}
                className="min-w-0 rounded-xl border border-border p-4"
              >
                <dt className="flex flex-wrap items-center gap-2 text-sm font-medium text-foreground">
                  <span
                    id={`application-bootstrap-${definition.key}`}
                    tabIndex={-1}
                    className="min-w-0"
                  >
                    {definition.label}
                  </span>
                  <code className="min-w-0 max-w-full break-all rounded bg-surface-muted px-1.5 py-0.5 text-xs font-normal text-muted">
                    {definition.key}
                  </code>
                  {definition.requirement && (
                    <span className="text-[11px] font-normal text-muted">
                      {REQUIREMENT_LABELS[definition.requirement]}
                    </span>
                  )}
                  {definition.state && <StateBadge state={definition.state} />}
                </dt>
                <dd className="mt-1 text-sm leading-6 text-muted">{definition.description}</dd>
                {definition.state === 'fallback' && (
                  <dd className="mt-2 rounded-lg bg-surface-muted p-3 text-sm leading-6 text-muted">
                    The settings root currently falls back to the deployment environment&apos;s
                    AGENT_SESSION_SECRET. To separate it, set AGENT_SETTINGS_ENCRYPTION_KEY to the
                    exact same bytes as the current fallback so existing ciphertext stays readable.
                    A different key is a real rotation: it requires re-encrypting everything this
                    root protects and coordinating every listener, including runner policy archived
                    credentials, before the fallback is removed.
                  </dd>
                )}
                {(definition.actions?.length ?? 0) > 0 && (
                  <dd className="mt-3 flex flex-col items-start gap-2">
                    {definition.actions?.map((action) => (
                      <Action
                        key={`${action.target}:${action.label}`}
                        label={action.label}
                        target={action.target}
                        recoveryToken={recoveryToken}
                        onRevealSetting={onRevealSetting}
                      />
                    ))}
                  </dd>
                )}
              </div>
            );
          })}
        </dl>
      )}
    </div>
  );
}

export default DeploymentSetup;
