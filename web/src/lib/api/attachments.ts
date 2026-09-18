// Per-task uploaded files.
//
// Uploads are staged before the job exists, then named by id in the create
// request. The alternative — sending the bytes with the job — would make
// `POST /v1/agent/jobs` multipart and change the shape its `Idempotency-Key`
// fingerprints. Staging also means the transfer happens while the user is
// still typing rather than after they press Run.
//
// The control plane garbage-collects a staged upload that no job ever claimed.
//
// Bytes travel base64 inside JSON rather than as multipart: every other call
// this client makes is JSON, and the control plane has no binary surface at
// all. At a 5 MB ceiling the encoding costs a third more bytes and nothing in
// complexity.

import { config } from '@/config/env';
import { fetchWithAuth, jsonOrThrow } from './client';

export interface AttachmentApi {
  id: number;
  /** Sandbox-relative path under /uploads, directory structure preserved. */
  path: string;
  size_bytes: number;
  content_type: string;
}

const PATH = '/v1/agent/uploads';

/**
 * Base64 without blowing the stack.
 *
 * `String.fromCharCode(...bytes)` is the usual one-liner and it throws on a
 * multi-megabyte file: the spread becomes one argument per byte, and engines
 * cap argument counts well below five million. Chunking keeps each call small.
 */
function toBase64(bytes: Uint8Array): string {
  const CHUNK = 0x8000;
  let binary = '';
  for (let at = 0; at < bytes.length; at += CHUNK) {
    binary += String.fromCharCode(...bytes.subarray(at, at + CHUNK));
  }
  return btoa(binary);
}

/**
 * Stage one file.
 *
 * `relativePath` carries a directory upload's own structure: the browser gives
 * it as `webkitRelativePath`, and it is what the file is called under
 * `/uploads`. A plain file selection has none, so the bare name is used.
 */
export async function uploadAttachment(file: File, relativePath?: string): Promise<AttachmentApi> {
  const bytes = new Uint8Array(await file.arrayBuffer());
  const response = await fetchWithAuth(config.apiBase, PATH, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      path: relativePath || file.name,
      content_base64: toBase64(bytes),
      content_type: file.type || 'application/octet-stream',
    }),
  });
  return jsonOrThrow<AttachmentApi>(response);
}

/** Drop a staged upload the user removed before running. */
export async function deleteAttachment(id: number): Promise<void> {
  const response = await fetchWithAuth(config.apiBase, `${PATH}/${id}`, { method: 'DELETE' });
  if (!response.ok) await jsonOrThrow(response);
}
