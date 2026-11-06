'use client';

// The harness a lifecycle event reported: what the CLI said it started with.
//
// Claude Code composes its system prompt inside the CLI and never puts it on
// the wire, so no panel can show one. The System Prompt tab says that plainly
// rather than filling the space with something that merely looks like a prompt.
// What a runtime does report about itself is the configuration it started
// under, and that is what these tabs render — quoted from the stored record,
// never reconstructed from the platform's own intentions.

import {
  DetailRow,
  DetailRows,
  DetailSection,
  EmptyNote,
  TabPanel,
  TruncationNote,
} from './StepDetailParts';
import type { HarnessInventory, TrajectoryStep } from './trajectorySteps';

import type { HarnessTab } from './trajectoryInspectorTabs';

export { HARNESS_TABS, type HarnessTab } from './trajectoryInspectorTabs';

/** A value the runtime reported, or the fact that it reported none. */
function Reported({ value, mono = true }: { value: string | null; mono?: boolean }) {
  if (value === null) {
    return <span className="text-gray-400 dark:text-gray-500">Not recorded</span>;
  }
  return <span className={mono ? 'font-mono' : undefined}>{value}</span>;
}

/** The list of tool names, in the order the runtime reported them. */
function ToolNames({ harness }: { harness: HarnessInventory }) {
  if (harness.tools.length === 0) {
    return <EmptyNote>The runtime named no tools.</EmptyNote>;
  }
  return (
    <ul className="flex flex-wrap gap-1">
      {harness.tools.map((name) => (
        <li
          key={name}
          className="rounded border border-gray-200 px-1.5 py-0.5 font-mono text-[11px] text-gray-700 dark:border-white/10 dark:text-gray-300"
        >
          {name}
        </li>
      ))}
    </ul>
  );
}

export function HarnessDetails({ step, tab }: { step: TrajectoryStep; tab: HarnessTab }) {
  const idPrefix = `step-${step.key}`;
  const harness = step.harness;
  const payload = step.sourceEvents[0]?.payload ?? {};
  if (!harness) {
    return (
      <TabPanel idPrefix={idPrefix} tab={tab}>
        <EmptyNote>This step recorded no harness report.</EmptyNote>
      </TabPanel>
    );
  }

  if (tab === 'tools') {
    return (
      <TabPanel idPrefix={idPrefix} tab="tools">
        <TruncationNote payload={payload} />
        <DetailSection title={`Tools reported (${harness.tools.length})`}>
          <ToolNames harness={harness} />
        </DetailSection>
        {harness.toolsTruncated ? (
          <p
            role="note"
            className="rounded bg-amber-50 px-2 py-1 text-[11px] text-amber-800 dark:bg-amber-500/10 dark:text-amber-300"
          >
            The platform stored the first {harness.tools.length} tool name
            {harness.tools.length === 1 ? '' : 's'}. The runtime reported more, and the rest were
            not kept.
          </p>
        ) : null}
        {harness.mcpServers.length ? (
          <DetailSection title="MCP servers reported">
            <DetailRows>
              {harness.mcpServers.map((server) => (
                <DetailRow key={server.name} label={server.name}>
                  <Reported value={server.status} />
                </DetailRow>
              ))}
            </DetailRows>
          </DetailSection>
        ) : null}
        <p className="text-[11px] text-gray-400 dark:text-gray-500">
          These are the names the CLI listed when it started. It does not report what each tool
          accepts, so no argument or schema is shown here.
        </p>
      </TabPanel>
    );
  }

  return (
    <TabPanel idPrefix={idPrefix} tab="systemPrompt">
      <TruncationNote payload={payload} />
      <p className="text-gray-500 dark:text-gray-400">
        The runtime builds its system prompt inside the CLI and does not report it, so the platform
        holds none to show. What the CLI did report about the session it started is below.
      </p>
      <DetailRows>
        <DetailRow label="Runtime version">
          <Reported value={harness.runtimeVersion} />
        </DetailRow>
        <DetailRow label="Model">
          <Reported value={harness.model} />
        </DetailRow>
        <DetailRow label="Permission mode">
          <Reported value={harness.permissionMode} />
        </DetailRow>
      </DetailRows>
      <p className="text-[11px] text-gray-400 dark:text-gray-500">
        The permission mode is the CLI&apos;s own statement of what it ran under. The platform asks
        for bypassPermissions because the sandbox, not an interactive prompt, is the boundary.
      </p>
    </TabPanel>
  );
}
