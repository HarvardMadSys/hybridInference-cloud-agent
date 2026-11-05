'use client';

import type { ReactNode } from 'react';
import { useT } from '@/components/providers';
import type { TranslationKey } from '@/lib/i18n/translate';
import type {
  EnvironmentConfig,
  EnvironmentSnapshotSummary,
  EnvironmentStep,
} from '@/lib/api/environments';

// One vocabulary for every state an environment surface shows. Tests, setup
// steps and actions report overlapping lowercase states from the server; the
// reader should see the same words and colours for the same state wherever it
// appears, and never the raw API value.

type Tone = 'success' | 'error' | 'progress' | 'neutral' | 'warning';

const STATES: Record<string, { key: TranslationKey; tone: Tone }> = {
  queued: { key: 'environment.state.queued', tone: 'progress' },
  starting: { key: 'environment.state.starting', tone: 'progress' },
  running: { key: 'environment.state.running', tone: 'progress' },
  paused: { key: 'environment.state.paused', tone: 'neutral' },
  suspended: { key: 'environment.state.suspended', tone: 'neutral' },
  succeeded: { key: 'environment.state.succeeded', tone: 'success' },
  failed: { key: 'environment.state.failed', tone: 'error' },
  skipped: { key: 'environment.state.skipped', tone: 'warning' },
  cancelled: { key: 'environment.state.cancelled', tone: 'neutral' },
  stopped: { key: 'environment.state.stopped', tone: 'neutral' },
  closed: { key: 'environment.state.closed', tone: 'neutral' },
  unknown: { key: 'environment.state.unknown', tone: 'warning' },
};

const PHASES: Record<string, TranslationKey> = {
  setup: 'environment.phase.setup',
  update: 'environment.phase.update',
  start: 'environment.phase.start',
  verify: 'environment.phase.verify',
};

const TONES: Record<Tone, string> = {
  success: 'bg-status-success/10 text-status-success',
  error: 'bg-status-error/10 text-status-error',
  progress: 'bg-status-info/10 text-status-info',
  warning: 'bg-status-warning/10 text-status-warning',
  neutral: 'bg-surface-muted text-muted',
};

export const ACTIVE_STATES = ['queued', 'starting', 'running', 'paused', 'suspended'];

/** A state carries a dot; a plain label such as Default does not. */
export function Badge({
  tone,
  dot = true,
  children,
}: {
  tone: Tone;
  dot?: boolean;
  children: ReactNode;
}) {
  return (
    <span
      className={`inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-full px-2 py-0.5 text-xs font-medium ${TONES[tone]}`}
    >
      {dot ? <span aria-hidden="true" className="h-1.5 w-1.5 rounded-full bg-current" /> : null}
      {children}
    </span>
  );
}

/** Marks a team's shared environment, naming the team. */
export function TeamBadge({ name }: { name: string }) {
  const t = useT();
  const label = t('environment.teamBadge', { team: name });
  return (
    <Badge tone="neutral" dot={false}>
      <span className="max-w-[14rem] truncate" title={label}>
        {label}
      </span>
    </Badge>
  );
}

/** A server state as a coloured label; an unrecognised state is shown as sent. */
export function StateBadge({ state }: { state: string }) {
  const t = useT();
  // Own entries only, here and for phases: `__proto__` must not find
  // Object.prototype's.
  const known = Object.hasOwn(STATES, state) ? STATES[state] : undefined;
  return <Badge tone={known?.tone ?? 'neutral'}>{known ? t(known.key) : state}</Badge>;
}

export function usePhaseLabel(): (phase: string) => string {
  const t = useT();
  return (phase) => (Object.hasOwn(PHASES, phase) ? t(PHASES[phase]) : phase);
}

/** What the last test says about the saved version a list row shows. */
export function TestBadge({
  environment,
  globalRevision,
}: {
  environment: EnvironmentConfig;
  globalRevision: number;
}) {
  const t = useT();
  const test = environment.latest_test;
  if (!test) return <Badge tone="neutral">{t('environment.test.never')}</Badge>;
  if (ACTIVE_STATES.includes(test.state))
    return <Badge tone="progress">{t('environment.test.running')}</Badge>;
  // A pass for another version, or another set of global variables, says
  // nothing about this one.
  if (test.revision !== environment.revision || test.global_revision !== globalRevision)
    return <Badge tone="warning">{t('environment.test.outdated')}</Badge>;
  if (test.state === 'succeeded')
    return <Badge tone="success">{t('environment.test.passed')}</Badge>;
  if (test.state === 'failed') return <Badge tone="error">{t('environment.test.failed')}</Badge>;
  return <Badge tone="neutral">{t('environment.test.cancelled')}</Badge>;
}

/** Whether new tasks of an environment skip its installation. */
export function SnapshotBadge({ snapshot }: { snapshot?: EnvironmentSnapshotSummary | null }) {
  const t = useT();
  if (!snapshot) return <Badge tone="neutral">{t('environment.snapshot.none')}</Badge>;
  if (snapshot.state === 'ready')
    return (
      <span title={snapshot.ready_at ? new Date(snapshot.ready_at).toLocaleString() : undefined}>
        <Badge tone="success">{t('environment.snapshot.ready')}</Badge>
      </span>
    );
  if (snapshot.state === 'failed')
    return (
      <span title={snapshot.detail ?? undefined}>
        <Badge tone="error">{t('environment.snapshot.failed')}</Badge>
      </span>
    );
  return <Badge tone="progress">{t('environment.snapshot.building')}</Badge>;
}

/** A step satisfied without running: installed from a snapshot, or before a restore. */
function satisfiedLabel(step: EnvironmentStep): TranslationKey | null {
  if (step.state !== 'succeeded' || step.ran !== false || step.phase !== 'setup') return null;
  return step.snapshot_id ? 'environment.fromSnapshot' : 'environment.keptAfterRestore';
}

export function formatDuration(seconds: number): string {
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const whole = Math.round(seconds);
  return `${Math.floor(whole / 60)}m ${whole % 60}s`;
}

/** Install, start and verify steps, with a failed step's output already open. */
export function EnvironmentSteps({ steps }: { steps: EnvironmentStep[] }) {
  const t = useT();
  const phase = usePhaseLabel();
  if (!steps.length) return null;
  return (
    <ol className="divide-y divide-border overflow-hidden rounded-lg border border-border">
      {steps.map((step, index) => (
        <li key={`${step.phase}-${index}`} className="bg-surface">
          <details open={step.state === 'failed'} className="group">
            <summary className="flex cursor-pointer list-none items-center gap-2 px-3 py-2 text-sm hover:bg-surface-hover">
              <span className="font-medium text-foreground">{phase(step.phase)}</span>
              {satisfiedLabel(step) ? (
                <Badge tone="success">{t(satisfiedLabel(step)!)}</Badge>
              ) : (
                <StateBadge state={step.state} />
              )}
              <span className="ml-auto flex shrink-0 items-center gap-2 text-xs tabular-nums text-muted">
                {step.exit_code !== undefined && step.exit_code !== null && step.exit_code !== 0 ? (
                  <span>{t('environment.exitCode', { code: step.exit_code })}</span>
                ) : null}
                {step.duration_seconds !== undefined ? (
                  <span>{formatDuration(step.duration_seconds)}</span>
                ) : null}
                <svg
                  aria-hidden="true"
                  viewBox="0 0 20 20"
                  className="h-3.5 w-3.5 transition-transform group-open:rotate-90"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth={1.6}
                >
                  <path strokeLinecap="round" strokeLinejoin="round" d="m8 6 4 4-4 4" />
                </svg>
              </span>
            </summary>
            <div className="space-y-2 border-t border-border px-3 py-2">
              {step.detail && !satisfiedLabel(step) ? (
                <p className="text-xs text-status-error">{step.detail}</p>
              ) : null}
              {step.output ? (
                <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-all rounded-md bg-surface-muted p-2 font-mono text-xs text-foreground">
                  {step.output}
                </pre>
              ) : (
                <p className="text-xs text-muted">{t('environment.noOutput')}</p>
              )}
            </div>
          </details>
        </li>
      ))}
    </ol>
  );
}
