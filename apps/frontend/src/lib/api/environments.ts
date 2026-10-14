import { config } from '@/config/env';
import { fetchWithAuth, jsonOrThrow } from './client';

export type EnvironmentPhase = 'setup' | 'runtime' | 'both';
export interface EnvironmentVariable {
  name: string;
  secret: boolean;
  phase: EnvironmentPhase;
  value: string | null;
  is_set?: boolean;
  source?: 'global' | 'environment' | 'override';
}
export interface VariableEdit extends Omit<EnvironmentVariable, 'value'> {
  operation: 'keep' | 'replace' | 'remove';
  value?: string | null;
  original_name?: string;
}
export interface EnvironmentAction {
  id: string;
  name: string;
  command: string;
  cwd: string;
}
export interface EnvironmentStep {
  phase: string;
  state: string;
  output: string;
  exit_code?: number;
  duration_seconds?: number;
}
export interface EnvironmentRun {
  id: string;
  state: string;
  steps: EnvironmentStep[];
  detail?: string;
  base_sha: string;
  cleanup_complete?: boolean;
  host?: string | null;
  image_digest?: string;
}
export interface EnvironmentConfig {
  id: string | null;
  name: string;
  repo: string | null;
  revision: number;
  variables: EnvironmentVariable[];
  setup_script: string;
  start_script: string;
  verify_script: string;
  actions: EnvironmentAction[];
  is_default?: boolean;
  default_revision?: number;
  latest_test?: { id: string; state: string; revision: number; global_revision: number } | null;
}
export interface EnvironmentBinding {
  binding_id: string | null;
  environment_id?: string | null;
  environment_name?: string | null;
  revision?: number;
  global_revision?: number;
  legacy: boolean;
  has_new_version?: boolean;
  runtime_revision?: number;
  runtime_variables?: EnvironmentVariable[] | null;
  variables: EnvironmentVariable[];
  setup_script: string;
  start_script: string;
  actions: EnvironmentAction[];
  steps: EnvironmentStep[];
}
export interface ActionRun {
  id: string;
  state: string;
  terminal_id?: string;
  job_id?: string;
  exit_code?: number | null;
  output: string;
}
async function call<T>(path: string, method = 'GET', body?: unknown, key?: string): Promise<T> {
  const response = await fetchWithAuth(config.apiBase, `/v1/agent/${path}`, {
    method,
    cache: 'no-store',
    headers: { 'Content-Type': 'application/json', ...(key ? { 'Idempotency-Key': key } : {}) },
    ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
  });
  if (response.status === 204) return undefined as T;
  return jsonOrThrow<T>(response);
}
export const listEnvironments = (repo?: string) =>
  call<{ environments: EnvironmentConfig[]; globals: EnvironmentConfig }>(
    `environments${repo ? `?repo=${encodeURIComponent(repo)}` : ''}`,
  );
/** The owner's global variables alone, for a task that has no repository. */
export const getGlobalEnvironment = () => call<EnvironmentConfig>('environment-globals');
export const saveEnvironment = (
  value: Omit<EnvironmentConfig, 'variables'> & { variables: VariableEdit[] },
) => {
  const {
    id,
    name,
    repo,
    revision,
    setup_script,
    start_script,
    verify_script,
    actions,
    variables,
  } = value;
  return call<EnvironmentConfig>(
    id ? `environments/${id}` : 'environments',
    id ? 'PATCH' : 'POST',
    {
      name,
      repo,
      revision,
      setup_script,
      start_script,
      verify_script,
      actions,
      variables: variables.map((v) => ({
        name: v.name,
        secret: v.secret,
        phase: v.phase,
        operation: v.operation,
        ...(v.operation === 'replace' ? { value: v.value ?? '' } : {}),
      })),
    },
  );
};
export const saveGlobalVariables = (revision: number, variables: VariableEdit[]) =>
  call<EnvironmentConfig>('environment-globals', 'PATCH', {
    revision,
    variables: variables.map((v) => ({
      name: v.name,
      secret: v.secret,
      phase: v.phase,
      operation: v.operation,
      ...(v.operation === 'replace' ? { value: v.value ?? '' } : {}),
    })),
  });
export const deleteEnvironment = (id: string, revision: number) =>
  call<void>(`environments/${id}?revision=${revision}`, 'DELETE');
export const setEnvironmentDefault = (
  repo: string,
  environment_id: string | null,
  revision: number,
) =>
  call<unknown>(`environment-default?repo=${encodeURIComponent(repo)}`, 'PUT', {
    environment_id,
    revision,
  });
export const copyEnvironment = (
  id: string,
  revision: number,
  repo: string,
  name: string,
  key: string,
) => call<EnvironmentConfig>(`environments/${id}/copy`, 'POST', { revision, repo, name }, key);
export const getEnvironmentHistory = (id: string, global = false, before?: number) =>
  call<{ versions: EnvironmentConfig[] }>(
    `${global ? 'environment-globals' : `environments/${id}`}/versions${before ? `?before=${before}` : ''}`,
  );
export const restoreEnvironment = (
  id: string,
  revision: number,
  source_revision: number,
  global = false,
) =>
  call<EnvironmentConfig>(
    `${global ? 'environment-globals' : `environments/${id}`}/restore`,
    'POST',
    { revision, source_revision },
  );
export const previewEnvironmentImport = (
  environment_id: string | null,
  revision: number,
  text: string,
) =>
  call<{
    variables: { name: string; operation: string; secret: boolean; secret_allowed: boolean }[];
  }>('environment-import/preview', 'POST', {
    environment_id,
    revision,
    text,
  });
export const importEnvironmentVariables = (
  environment_id: string | null,
  revision: number,
  global_revision: number,
  text: string,
  ordinary_names: string[] = [],
) =>
  call<EnvironmentConfig>('environment-import', 'POST', {
    environment_id,
    revision,
    global_revision,
    text,
    ordinary_names,
  });
export const suggestEnvironmentScript = (repo: string) =>
  call<{ suggestions: { file: string; script: string }[] }>(
    'environment-script-suggestions',
    'POST',
    { repo },
  );
export const testEnvironment = (
  environment_id: string,
  environment_revision: number,
  global_revision: number,
) =>
  call<EnvironmentRun>(
    'environment-runs',
    'POST',
    { environment_id, environment_revision, global_revision },
    crypto.randomUUID(),
  );
export const getEnvironmentRun = (id: string) => call<EnvironmentRun>(`environment-runs/${id}`);
export const cancelEnvironmentRun = (id: string) =>
  call<EnvironmentRun>(`environment-runs/${id}/cancel`, 'POST');
export const getWorkspaceEnvironment = (jobId: string) =>
  call<EnvironmentBinding>(`jobs/${jobId}/thread/environment`);
export const retryWorkspaceEnvironment = (jobId: string) =>
  call<unknown>(`jobs/${jobId}/thread/environment/retry`, 'POST');
export const startEnvironmentAction = (jobId: string, actionId: string, key: string) =>
  call<ActionRun>(
    `jobs/${jobId}/thread/environment/actions/${actionId}/runs`,
    'POST',
    undefined,
    key,
  );
export const getEnvironmentActionRun = (id: string) =>
  call<ActionRun>(`environment-action-runs/${id}`);
export const stopEnvironmentAction = (id: string) =>
  call<ActionRun>(`environment-action-runs/${id}/stop`, 'POST');

export const refreshWorkspaceRuntime = (jobId: string, runtime_revision: number) =>
  call<unknown>(`jobs/${jobId}/thread/environment/runtime`, 'POST', { runtime_revision });
export const skipWorkspaceStart = (jobId: string) =>
  call<unknown>(`jobs/${jobId}/thread/environment/skip-start`, 'POST');
