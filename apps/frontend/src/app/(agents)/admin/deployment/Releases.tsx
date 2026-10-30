'use client';

import { useMemo } from 'react';

import { type DeploymentAgent, startRollout } from '@/lib/api/adminManagement';

import { shortVersion } from './deploymentCopy';
import {
  BUTTON_CLASS,
  Empty,
  FieldRow,
  GroupCard,
  Pill,
  type Run,
  useConfirmation,
} from './deploymentUi';

/** Releases every machine has verified, and which machines already run each one. */
export function Releases({ agents, run }: { agents: DeploymentAgent[]; run: Run }) {
  const [confirm, dialog] = useConfirmation();
  const shared = useMemo(() => {
    if (agents.length === 0) return [];
    const [first, ...rest] = agents;
    return Object.entries(first.observed.releases ?? {}).filter(([sha]) =>
      rest.every((agent) => sha in (agent.observed.releases ?? {})),
    );
  }, [agents]);
  return (
    <GroupCard
      title="Desired release"
      icon="review"
      description="Releases every machine has downloaded and verified. Deploying one prepares every machine, updates the database, then switches the servers and finally the control plane."
    >
      {shared.length === 0 ? (
        <Empty>No release is ready on every machine yet.</Empty>
      ) : (
        shared.map(([sha, entry]) => {
          const version = shortVersion(entry.source_commit);
          const running = agents.filter(
            (agent) => agent.observed.deployed_commit === entry.source_commit,
          ).length;
          return (
            <FieldRow
              key={sha}
              label={<span className="font-mono">{version}</span>}
              help={
                running === agents.length
                  ? 'Running on every machine.'
                  : running
                    ? `Running on ${running} of ${agents.length} machines.`
                    : 'Ready on every machine, not running yet.'
              }
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                {running === agents.length ? <Pill tone="success">Current</Pill> : <span />}
                <button
                  type="button"
                  className={BUTTON_CLASS}
                  aria-label={`Deploy ${version}`}
                  onClick={() =>
                    confirm({
                      title: `Deploy ${version} to every machine?`,
                      confirmLabel: 'Deploy',
                      body: (
                        <p>
                          Every machine is prepared first, then the database is updated, then the
                          servers and finally the control plane switch over.
                        </p>
                      ),
                      action: () =>
                        void run(() =>
                          startRollout(sha).then(() => `Deploying ${version}. Progress is below.`),
                        ),
                    })
                  }
                >
                  Deploy
                </button>
              </div>
            </FieldRow>
          );
        })
      )}
      {dialog}
    </GroupCard>
  );
}
