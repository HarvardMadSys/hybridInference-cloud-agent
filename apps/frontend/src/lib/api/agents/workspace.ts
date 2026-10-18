import { fetchWithAuth, jsonOrThrow } from '../client';
import { config } from '@/config/env';

const API_BASE = config.apiBase;

export type AgentJobFileStatus = 'added' | 'modified' | 'deleted';

export interface AgentJobFileEntryApi {
  name: string;
  path: string;
  kind: 'file' | 'directory' | 'symlink';
  status?: AgentJobFileStatus | null;
  size?: number | null;
  binary?: boolean;
  truncated?: boolean;
  omitted_reason?: string | null;
}

export interface AgentJobDirectoryApi {
  path: string;
  kind: 'directory';
  entries: AgentJobFileEntryApi[] | null;
  /**
   * Some entries are not listed: the directory is over the host's entry limit,
   * or holds a name that is not UTF-8.
   */
  truncated?: boolean;
  writable?: boolean;
  source?: 'workspace';
}

export interface AgentJobFileApi {
  path: string;
  kind: 'file';
  content: string | null;
  /** Complete bounded bytes for binary downloads and passive raster previews. */
  content_base64?: string | null;
  media_type?: 'image/png' | 'image/jpeg' | 'image/gif' | 'image/webp' | null;
  size: number | null;
  binary: boolean;
  truncated: boolean;
  status?: AgentJobFileStatus | null;
  omitted_reason?: string | null;
  writable?: boolean;
  source?: 'workspace';
}

export interface AgentJobSymlinkApi {
  path: string;
  kind: 'symlink';
  status?: AgentJobFileStatus | null;
  omitted_reason?: string | null;
  writable?: boolean;
  source?: 'workspace';
}

export type AgentJobFilesApi = AgentJobDirectoryApi | AgentJobFileApi | AgentJobSymlinkApi;

/** Browse the retained container's live workspace. */
export async function getAgentJobFiles(jobId: string, path = ''): Promise<AgentJobFilesApi> {
  const query = path ? `?path=${encodeURIComponent(path)}` : '';
  const resp = await fetchWithAuth(
    API_BASE,
    `/v1/agent/jobs/${encodeURIComponent(jobId)}/files${query}`,
  );
  return jsonOrThrow<AgentJobFilesApi>(resp);
}

/** Save one UTF-8 file in the live workspace. */
export async function writeAgentJobFile(
  jobId: string,
  path: string,
  content: string,
): Promise<AgentJobFileApi> {
  const resp = await fetchWithAuth(
    API_BASE,
    `/v1/agent/jobs/${encodeURIComponent(jobId)}/files?path=${encodeURIComponent(path)}`,
    {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content }),
    },
  );
  return jsonOrThrow<AgentJobFileApi>(resp);
}

export interface AgentGitWorkspaceApi {
  available: boolean;
  branch: string;
  changes: Array<{ code: string; path: string }>;
  patch: string;
  commits: Array<{
    sha: string;
    short_sha: string;
    subject: string;
    author: string;
    authored_at: string;
  }>;
}

/** Read Git status/diff/commits from the live workspace. */
export async function getAgentJobGit(jobId: string): Promise<AgentGitWorkspaceApi> {
  const resp = await fetchWithAuth(API_BASE, `/v1/agent/jobs/${encodeURIComponent(jobId)}/git`);
  return jsonOrThrow<AgentGitWorkspaceApi>(resp);
}

/**
 * The name a download asked to be saved under: the UTF-8 form when the server
 * sent one, else the plain ASCII fallback.
 */
export function attachmentFilename(header: string | null): string | null {
  if (!header) return null;
  const encoded = /filename\*\s*=\s*UTF-8''([^;]+)/i.exec(header);
  if (encoded) {
    try {
      return decodeURIComponent(encoded[1].trim());
    } catch {
      // A malformed escape falls back to the plain name below.
    }
  }
  const plain = /filename\s*=\s*"([^"]+)"/i.exec(header);
  return plain ? plain[1] : null;
}

/**
 * The whole workspace as one gzipped tar, built by the host that holds it.
 *
 * Fetched rather than opened as a link, so a refusal — larger than the host's
 * download limit, or no workspace yet — is reported on the page instead of
 * replacing it with the error body.
 */
export async function downloadAgentWorkspace(
  jobId: string,
): Promise<{ blob: Blob; filename: string }> {
  const resp = await fetchWithAuth(
    API_BASE,
    `/v1/agent/jobs/${encodeURIComponent(jobId)}/workspace/archive`,
  );
  if (!resp.ok) await jsonOrThrow<never>(resp);
  return {
    blob: await resp.blob(),
    filename: attachmentFilename(resp.headers.get('Content-Disposition')) ?? 'workspace.tar.gz',
  };
}
