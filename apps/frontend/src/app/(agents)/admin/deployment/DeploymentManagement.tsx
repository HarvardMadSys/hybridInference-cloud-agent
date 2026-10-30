'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import {
  type AdminCredential,
  type DeploymentOverview,
  type ManagementCatalog,
  type ManagementStatus,
  getDeployments,
  getManagementCatalog,
  getManagementStatus,
  listCredentials,
} from '@/lib/api/adminManagement';
import { getErrorMessage } from '@/lib/utils/errors';

import { MachineSteps, Operations } from './Activity';
import { AdminCredentials } from './AdminCredentials';
import { ConfigurationMap, type DeploymentTab } from './ConfigurationMap';
import { DatabaseOperations, MaintenancePanel } from './DatabaseOperations';
import { ErrorNotice, OneTimeSecret, Panel, StatusNotice } from './deploymentUi';
import { EnrollMachine, Machines } from './Machines';
import { Overview, workflowTab } from './Overview';
import { Releases } from './Releases';
import { type Revealed, SecurityKeys } from './SecurityKeys';

const TABS: { id: DeploymentTab; label: string }[] = [
  { id: 'overview', label: 'Overview' },
  { id: 'machines', label: 'Machines' },
  { id: 'releases', label: 'Releases' },
  { id: 'keys', label: 'Keys and credentials' },
  { id: 'database', label: 'Database' },
  { id: 'activity', label: 'Activity' },
];

function tabFromLocation(): DeploymentTab {
  const url = new URL(window.location.href);
  // Earlier links pointed at sections with a hash.
  const requested = url.searchParams.get('tab') ?? url.hash.slice(1);
  return TABS.find((tab) => tab.id === requested)?.id ?? 'overview';
}

function useTab(): [DeploymentTab, (tab: DeploymentTab) => void] {
  const [tab, setTab] = useState<DeploymentTab>('overview');
  useEffect(() => setTab(tabFromLocation()), []);
  const select = useCallback((next: DeploymentTab) => {
    setTab(next);
    const url = new URL(window.location.href);
    if (next === 'overview') url.searchParams.delete('tab');
    else url.searchParams.set('tab', next);
    url.hash = '';
    window.history.replaceState(window.history.state, '', `${url.pathname}${url.search}`);
  }, []);
  return [tab, select];
}

function Tabs({ tab, onSelect }: { tab: DeploymentTab; onSelect: (tab: DeploymentTab) => void }) {
  const buttons = useRef<(HTMLButtonElement | null)[]>([]);
  return (
    <div
      role="tablist"
      aria-label="Deployment sections"
      className="mt-6 flex w-full gap-1 overflow-x-auto rounded-xl border border-border bg-surface-muted p-1 sm:flex-wrap sm:overflow-x-visible"
    >
      {TABS.map(({ id, label }, index) => (
        <button
          key={id}
          ref={(element) => {
            buttons.current[index] = element;
          }}
          type="button"
          role="tab"
          id={`deployment-tab-${id}`}
          aria-controls={`deployment-panel-${id}`}
          aria-selected={tab === id}
          tabIndex={tab === id ? 0 : -1}
          onClick={() => onSelect(id)}
          onKeyDown={(event) => {
            const moves: Partial<Record<string, number>> = {
              ArrowRight: (index + 1) % TABS.length,
              ArrowLeft: (index - 1 + TABS.length) % TABS.length,
              Home: 0,
              End: TABS.length - 1,
            };
            const next = moves[event.key];
            if (next === undefined) return;
            event.preventDefault();
            onSelect(TABS[next].id);
            buttons.current[next]?.focus();
          }}
          className={`min-h-11 shrink-0 rounded-lg px-4 py-2 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-crimson dark:focus-visible:ring-crimson-light ${
            tab === id
              ? 'bg-surface text-foreground shadow-subtle'
              : 'text-muted hover:bg-surface-hover hover:text-foreground'
          }`}
        >
          {label}
        </button>
      ))}
    </div>
  );
}

function TabPanel({
  id,
  tab,
  children,
}: {
  id: DeploymentTab;
  tab: DeploymentTab;
  children: React.ReactNode;
}) {
  // Inactive panels stay mounted so switching tabs keeps what was typed.
  return (
    <div
      role="tabpanel"
      id={`deployment-panel-${id}`}
      aria-labelledby={`deployment-tab-${id}`}
      hidden={tab !== id}
      tabIndex={0}
      className="focus-visible:outline-none"
    >
      {children}
    </div>
  );
}

export function DeploymentManagement() {
  const [overview, setOverview] = useState<DeploymentOverview | null>(null);
  const [status, setStatus] = useState<ManagementStatus | undefined>(undefined);
  const [credentials, setCredentials] = useState<AdminCredential[]>([]);
  const [catalog, setCatalog] = useState<ManagementCatalog | null>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [revealed, setRevealed] = useState<Revealed | null>(null);
  const [tab, selectTab] = useTab();

  useEffect(() => {
    // The catalog never changes while the page is open, and an earlier
    // control plane has none: the page then simply goes without the map.
    getManagementCatalog().then(setCatalog, () => setCatalog(null));
  }, []);

  const refresh = useCallback(async () => {
    try {
      const [deployments, management, creds] = await Promise.all([
        getDeployments(),
        getManagementStatus(),
        listCredentials(),
      ]);
      setOverview(deployments);
      setStatus(management);
      setCredentials(creds.credentials);
      setError('');
    } catch {
      setError(
        'The control plane is not answering right now; it may be restarting. This page tries again every 10 seconds.',
      );
    }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 10000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const run = useCallback(
    async (work: () => Promise<string | void>) => {
      try {
        const done = await work();
        setNotice(done ?? 'Started. Progress appears here as the machines report back.');
        setError('');
        await refresh();
      } catch (caught) {
        setNotice('');
        setError(`That did not go through: ${getErrorMessage(caught)}`);
      }
    },
    [refresh],
  );

  const agents = useMemo(() => (overview?.agents ?? []).filter((a) => !a.revoked), [overview]);
  const controlPlane = agents.filter((a) => a.role === 'control-plane');
  const hosts = agents.filter((a) => a.role === 'host');
  const workflows = overview?.workflows ?? [];
  const area = (name: DeploymentTab) =>
    workflows.filter((workflow) => workflowTab(workflow.kind) === name);

  return (
    <div>
      {error && <ErrorNotice onRetry={() => void refresh()}>{error}</ErrorNotice>}
      {notice && <StatusNotice>{notice}</StatusNotice>}
      {revealed && <OneTimeSecret {...revealed} onDismiss={() => setRevealed(null)} />}
      <MaintenancePanel maintenance={overview?.maintenance} />
      <Tabs tab={tab} onSelect={selectTab} />
      <TabPanel id="overview" tab={tab}>
        <Panel
          title="Overview"
          description="Whether saved settings are in use, what is running, and anything under way. Everything here is as the processes and machines last reported it."
        >
          <Overview status={status} agents={agents} workflows={workflows} onOpen={selectTab} />
        </Panel>
        <ConfigurationMap catalog={catalog} onOpen={selectTab} />
      </TabPanel>
      <TabPanel id="machines" tab={tab}>
        <Panel
          title="Machines"
          description="Machines with a deployment agent, what they report, and their configuration."
        >
          <EnrollMachine reveal={setRevealed} run={run} />
          <Machines agents={agents} contract={overview?.contract} catalog={catalog} run={run} />
        </Panel>
      </TabPanel>
      <TabPanel id="releases" tab={tab}>
        <Panel title="Releases" description="The release every machine should run.">
          <Releases agents={agents} run={run} />
          <Operations title="Rollouts" workflows={area('releases')} run={run} />
        </Panel>
      </TabPanel>
      <TabPanel id="keys" tab={tab}>
        <Panel
          title="Keys and credentials"
          description="Rotate the keys that protect saved secrets and connections, renew server credentials, and manage credentials for automation."
        >
          <SecurityKeys
            controlPlane={controlPlane}
            hosts={hosts}
            workflows={workflows}
            run={run}
            reveal={setRevealed}
          />
          <Operations title="Rotations" workflows={area('keys')} run={run} />
          <AdminCredentials credentials={credentials} run={run} reveal={setRevealed} />
        </Panel>
      </TabPanel>
      <TabPanel id="database" tab={tab}>
        <Panel
          title="Database"
          description="Change how the application reaches its database, rotate its password, or move the data to another database."
        >
          <DatabaseOperations controlPlane={controlPlane} workflows={workflows} run={run} />
          <Operations title="Database changes" workflows={area('database')} run={run} />
        </Panel>
      </TabPanel>
      <TabPanel id="activity" tab={tab}>
        <Panel title="Activity" description="Recent steps sent to machines.">
          <Operations title="Other operations" workflows={area('activity')} run={run} />
          <MachineSteps intents={overview?.intents ?? []} agents={agents} run={run} />
        </Panel>
      </TabPanel>
    </div>
  );
}
