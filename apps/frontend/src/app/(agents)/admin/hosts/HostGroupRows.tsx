import type { ReactNode } from 'react';

import type { AgentRunnerHost } from '@/lib/api/admin';
import { MAX_HOST_SLOTS, MIN_HOST_SLOTS } from '@/lib/api/admin';

import hostStyles from './host-controls.module.css';
import {
  type CapacityCell,
  type HostGroupCapacity,
  type SandboxSummary,
  concurrencyFacts,
  cpuCell,
  executionSlotsCell,
  memoryCell,
  reasonLabel,
  storageCell,
} from './capacity';
import { hostStatusLabel } from './format';

const STATUS_STYLES: Record<string, string> = {
  online:
    'bg-emerald-50 dark:bg-emerald-500/10 text-emerald-700 dark:text-emerald-400 ring-emerald-600/20 dark:ring-emerald-500/25',
  draining:
    'bg-amber-50 dark:bg-amber-500/10 text-amber-700 dark:text-amber-400 ring-amber-600/20 dark:ring-amber-500/25',
  enrolling:
    'bg-blue-50 dark:bg-blue-500/10 text-blue-700 dark:text-blue-400 ring-blue-600/20 dark:ring-blue-500/25',
  disabled:
    'bg-gray-100 dark:bg-white/5 text-gray-600 dark:text-gray-400 ring-gray-500/20 dark:ring-white/15',
};

function StatusBadge({ host }: { host: AgentRunnerHost }) {
  const style = STATUS_STYLES[host.status] ?? STATUS_STYLES.disabled;
  return (
    <span
      className={`inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium ring-1 ring-inset ${style}`}
    >
      {hostStatusLabel(host)}
    </span>
  );
}

/**
 * A 0-100 reading as a bar under its number.
 *
 * The chart's own axis, at the size of a table cell. A fleet is read down this
 * column, and "8%" and "82%" are the same shape of text; the bar is what makes
 * the difference visible without reading. It is drawn only for a value that was
 * measured — the same rule the number follows — and `aria-hidden`, because the
 * number beside it is the accessible reading and an unlabelled bar is not.
 */
function Meter({ value, muted }: { value: number; muted: boolean }) {
  const width = Math.min(100, Math.max(0, value));
  return (
    <span
      aria-hidden
      className="mt-1.5 block h-1 w-16 overflow-hidden rounded-full bg-gray-200/80 dark:bg-white/10"
    >
      <span
        className={`block h-full rounded-full ${
          muted ? 'bg-gray-400 dark:bg-gray-500' : 'bg-crimson/60 dark:bg-crimson-light/60'
        }`}
        style={{ width: `${width}%` }}
      />
    </span>
  );
}

function CapabilityCell({
  cell,
  muted = false,
  meter = null,
}: {
  cell: CapacityCell;
  muted?: boolean;
  /** The cell's own 0-100 reading, or null when nothing was measured. */
  meter?: number | null;
}) {
  return (
    <div
      // A stale observation keeps its number and loses its confidence; the
      // muted value reinforces the explicit warning under Status.
      className={muted ? 'text-gray-500 dark:text-gray-400' : 'text-gray-800 dark:text-gray-200'}
      title={cell.title ?? undefined}
    >
      <span className="block whitespace-nowrap tabular-nums">{cell.text}</span>
      {meter !== null && <Meter value={meter} muted={muted} />}
      {cell.note !== null && (
        <span className="mt-0.5 block whitespace-nowrap text-[11px] text-gray-500 dark:text-gray-400">
          {cell.note}
        </span>
      )}
    </div>
  );
}

function RowToggle({
  open,
  name,
  panelId,
  onClick,
}: {
  open: boolean;
  name: string;
  panelId: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      aria-expanded={open}
      aria-label={`${open ? 'Hide details for' : 'Show details for'} ${name}`}
      aria-controls={panelId}
      onClick={onClick}
      className="ui-button ui-button-secondary min-h-9 gap-1.5 px-3 py-1.5 text-xs [@media(pointer:coarse)]:min-h-11"
    >
      <svg
        viewBox="0 0 12 12"
        aria-hidden
        className={`h-3 w-3 transition-transform ${open ? 'rotate-90' : ''}`}
      >
        <path
          d="M4 2l4 4-4 4"
          fill="none"
          stroke="currentColor"
          strokeWidth={1.6}
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
      {/* The band holds the server's usage history and its runtime settings,
          so the toggle names neither one. */}
      <span aria-hidden="true">Details</span>
    </button>
  );
}

// What a moving removal still waits for. Sandboxes that cannot move hold it
// until the administrator agrees to leave them, by removing again.
function movingOutProgress(host: AgentRunnerHost): string {
  const progress = `Moving out: ${host.sandboxes_left ?? 0} sandbox(es), ${host.slots_used} running turn(s) left`;
  const unmovable = host.sandboxes_unmovable ?? 0;
  if (unmovable === 0) return progress;
  return host.removal_leaves_unmoved
    ? `${progress}; ${unmovable} cannot move and stay`
    : `${progress}; ${unmovable} cannot move, remove again to leave them`;
}

function SandboxesCell({ sandboxes }: { sandboxes: SandboxSummary }) {
  const title = [sandboxes.explanation, ...sandboxes.reasons.map(reasonLabel)]
    .filter((part): part is string => Boolean(part))
    .join(' ');
  return (
    <td data-label="Sandboxes" className="px-3 py-4" title={title || undefined}>
      <span
        className={`block whitespace-nowrap ${
          sandboxes.staleOnly
            ? 'text-amber-700 dark:text-amber-300'
            : 'text-gray-800 dark:text-gray-200'
        }`}
      >
        {sandboxes.text}
      </span>
      {sandboxes.partial && (
        <span className="mt-0.5 block text-[11px] text-amber-700 dark:text-amber-300">
          {sandboxes.reasons.length > 0
            ? sandboxes.reasons.map(reasonLabel).join(' · ')
            : 'Partial coverage'}
        </span>
      )}
    </td>
  );
}

function RowActions({
  host,
  busy,
  onResume,
  onRemove,
}: {
  host: AgentRunnerHost;
  busy: boolean;
  onResume: () => void;
  onRemove: () => void;
}) {
  return (
    <td className={`${hostStyles.actions} px-6 py-4`}>
      <div className="flex flex-wrap items-center justify-end gap-2">
        {/* Draining is not offered as an action of its
            own: Remove drains a busy host by itself. Only
            a host already held back without a deployment
            owning it — a pending removal, or a drain made
            through the API — gets the way back. */}
        {host.status === 'draining' && !host.drain_fence_active && (
          <button
            type="button"
            disabled={busy}
            onClick={onResume}
            className="ui-button ui-button-secondary min-h-9 px-3 py-1.5 text-xs [@media(pointer:coarse)]:min-h-11"
          >
            {host.removal_pending ? 'Cancel removal' : 'Resume new work'}
          </button>
        )}
        <button
          type="button"
          disabled={busy}
          onClick={onRemove}
          className="ui-button min-h-9 border border-status-error/20 px-3 py-1.5 text-xs text-status-error hover:bg-status-error/10 [@media(pointer:coarse)]:min-h-11"
        >
          {host.removal_pending ? 'Remove now' : 'Remove from pool'}
        </button>
      </div>
    </td>
  );
}

/**
 * One server's row in the hosts table, and the band under it when it is open.
 *
 * The row draws what the page already knows about the server; every action it
 * offers is the page's, passed in. The band's content is the page's too: the
 * capacity history and the settings it holds belong to the open panel's state,
 * which the page keeps.
 */
export function HostGroupRows({
  name,
  host,
  facts,
  open,
  panelId,
  autoscalingManaged,
  busy,
  onToggle,
  onSlots,
  onResume,
  onRemove,
  panel,
}: {
  name: string;
  host: AgentRunnerHost;
  facts: HostGroupCapacity;
  open: boolean;
  panelId: string;
  /** CloudLab auto-scaling sets this server's slots; the row offers no control. */
  autoscalingManaged: boolean;
  busy: boolean;
  onToggle: () => void;
  /** Ask for a new limit of turns at once, already checked against the bounds. */
  onSlots: (slots: number) => void;
  onResume: () => void;
  onRemove: () => void;
  /** What the open band shows; nothing is drawn under the row without it. */
  panel: ReactNode;
}) {
  const concurrency = concurrencyFacts(host);
  return (
    <>
      <tr
        className={hostStyles.hostRow}
        onClick={(event) => {
          // Buttons inside the row keep their own meaning; a
          // row click is a convenience for the toggle, not a
          // second way to press Remove.
          if ((event.target as HTMLElement).closest('button, a, select, input')) {
            return;
          }
          onToggle();
        }}
      >
        <th
          scope="row"
          aria-label={name}
          className="px-6 py-4 font-medium text-gray-900 dark:text-gray-50"
        >
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span>{name}</span>
            {/* The row header keeps the server's own name:
                  the toggle speaks for itself inside it, so a
                  reader asking "which machine is this row?"
                  is not answered with a button's label. */}
            <RowToggle open={open} name={name} panelId={panelId} onClick={onToggle} />
          </div>
        </th>
        <td data-label="Status" className="px-3 py-4">
          <StatusBadge host={host} />
          {host.removal_pending && (
            <span className="mt-1 block whitespace-nowrap text-[11px] text-gray-500 dark:text-gray-400">
              {host.removal_moves_out
                ? movingOutProgress(host)
                : `${host.slots_used} running turn(s) left`}
            </span>
          )}
          {facts.stale && (
            <span className="mt-1 block whitespace-nowrap text-[11px] font-medium text-amber-700 dark:text-amber-300">
              Capacity stale
            </span>
          )}
        </td>
        <td data-label="Concurrent turns" className="px-3 py-4">
          <CapabilityCell cell={executionSlotsCell(host)} muted={facts.stale} />
          {/* The control sits under the number it changes.
                It asks for a limit, not a fleet size: the host
                starts a process per turn as it claims one, so
                the input is bounded only by the protocol's
                floor and a typo guard, and no machine capacity
                is implied by either.

                The field is uncontrolled and re-keyed on the
                saved value, so a poll landing mid-edit does not
                overwrite what the operator is typing, and a
                saved change does replace it.

                Saving is not applying, and the cell's own note
                says which happened — "applying 8" until the
                host confirms, "not applied" for an offline
                host, "redeploy to change" for a runner that
                predates remote limits — with the longer reason
                on hover. Nothing else is written under the
                field: the number is the host's number, and an
                operator who wants a different one types it. */}
          {autoscalingManaged ? (
            <p className="mt-2 max-w-[15rem] text-[11px] text-gray-500 dark:text-gray-400">
              Slots are managed from fresh capacity by CloudLab auto-scaling.
            </p>
          ) : (
            <form
              className="mt-2 flex min-w-0 max-w-full flex-wrap items-center gap-2"
              onSubmit={(event) => {
                event.preventDefault();
                const value = Number(new FormData(event.currentTarget).get('slots'));
                if (!Number.isInteger(value) || value < MIN_HOST_SLOTS || value > MAX_HOST_SLOTS) {
                  return;
                }
                onSlots(value);
              }}
            >
              <input
                key={`${host.host}:${concurrency.requested ?? 'default'}`}
                name="slots"
                type="number"
                min={MIN_HOST_SLOTS}
                max={MAX_HOST_SLOTS}
                step={1}
                defaultValue={concurrency.requested ?? host.slots_total ?? MIN_HOST_SLOTS}
                aria-label={`Turns at once for ${name}`}
                disabled={busy}
                className="ui-input min-h-9 w-20 min-w-0 max-w-full px-2.5 py-1.5 tabular-nums [@media(pointer:coarse)]:min-h-11"
              />
              <button
                type="submit"
                disabled={busy}
                className="ui-button ui-button-secondary min-h-9 px-3 py-1.5 text-xs [@media(pointer:coarse)]:min-h-11"
              >
                Apply
              </button>
            </form>
          )}
          {concurrency.launchError !== null && (
            <span
              role="alert"
              className="mt-1 block max-w-[15rem] text-[11px] text-red-600 dark:text-red-400"
            >
              {concurrency.launchError}
            </span>
          )}
        </td>
        <td data-label="CPU" className="px-3 py-4">
          <CapabilityCell
            cell={cpuCell(facts.capacity)}
            muted={facts.stale}
            meter={facts.capacity?.cpu_percent ?? null}
          />
        </td>
        <td data-label="Memory" className="px-3 py-4">
          <CapabilityCell
            cell={memoryCell(facts.capacity)}
            muted={facts.stale}
            meter={facts.capacity?.memory_used_percent ?? null}
          />
        </td>
        <td data-label="Storage" className="px-3 py-4">
          <CapabilityCell
            cell={storageCell(facts.capacity)}
            muted={facts.stale}
            meter={facts.capacity?.storage_used_percent ?? null}
          />
          {/* A recommendation, not a refusal: this host takes
                untrusted work and gives each new sandbox an
                unbounded writable layer on the disk above. */}
          {host.untrusted_without_disk_quota === true && (
            <span
              role="status"
              title="This server runs untrusted sandboxes with no container disk quota, so one sandbox can fill the disk the others write to. Set Container disk above 0 (needs Docker overlay2 on XFS with project quotas)."
              className="mt-1 block max-w-[15rem] text-[11px] font-medium text-amber-700 dark:text-amber-300"
            >
              No container disk quota
            </span>
          )}
        </td>
        <SandboxesCell sandboxes={facts.sandboxes} />
        <RowActions host={host} busy={busy} onResume={onResume} onRemove={onRemove} />
      </tr>
      {panel && (
        // No second card inside this one: the band is the
        // detail surface, and the panel draws on it directly
        // at the table's own first-column padding.
        <tr className="bg-gray-50/70 dark:bg-white/[0.03]">
          <td colSpan={8} className="px-6 pb-6 pt-4">
            {panel}
          </td>
        </tr>
      )}
    </>
  );
}
