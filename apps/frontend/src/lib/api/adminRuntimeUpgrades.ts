// Admin client for updating an agent CLI from the Settings page.
//
// `GET` lists each agent's version for new chats beside npm's latest release,
// with the progress of an update in flight; `POST` starts one. Rolling back is
// `setAgentRuntimeVersion` in `./admin`, which also replaces the update and
// keeps the chosen version on every server. See
// `cloud_agent/runtime_upgrades.py` for how an update completes.

import { config } from '@/config/env';

import { fetchWithAuth, jsonOrThrow } from './client';

const RUNTIME_UPGRADES = '/admin/agent/runtime-upgrades';

/** One update, as every online server reports it. */
export interface RuntimeUpgradeProgress {
  version: string;
  /** `downloading` until every online server holds and has checked it. */
  state: 'downloading' | 'updated' | 'failed';
  /** 0–100, the mean of the online servers' progress; 100 only once updated. */
  progress: number;
  error: string | null;
  /** What new chats used before; `null` is each server's default. */
  previous_version: string | null;
  requested_at: string | null;
  activated_at: string | null;
  /**
   * `update` for one started with the Update button. `selection` for a
   * version an admin chose (Roll back included) that a server could not
   * download; a selection only appears once it failed.
   */
  kind: 'update' | 'selection';
}

/** One agent the page can update. */
export interface RuntimeUpgradeItem {
  runtime: string;
  /** The version new chats use, or `null` when servers disagree. */
  current_version: string | null;
  default_version: string | null;
  latest_version: string | null;
  latest_error: string | null;
  update_available: boolean;
  upgrade: RuntimeUpgradeProgress | null;
}

export interface RuntimeUpgrades {
  runtimes: RuntimeUpgradeItem[];
}

export async function getRuntimeUpgrades(signal?: AbortSignal): Promise<RuntimeUpgrades> {
  return jsonOrThrow<RuntimeUpgrades>(
    await fetchWithAuth(config.apiBase, RUNTIME_UPGRADES, { signal }),
  );
}

/** Update one agent; asking again for the same version retries it. */
export async function requestRuntimeUpgrade(
  runtime: string,
  version: string,
): Promise<RuntimeUpgrades> {
  const resp = await fetchWithAuth(config.apiBase, RUNTIME_UPGRADES, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ runtime, version }),
  });
  return jsonOrThrow<RuntimeUpgrades>(resp);
}
