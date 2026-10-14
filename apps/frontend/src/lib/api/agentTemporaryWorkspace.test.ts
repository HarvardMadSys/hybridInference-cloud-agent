import { afterEach, describe, expect, it, vi } from 'vitest';

import { listAgentConversations, listAgentSessions } from './agents';
import { attachmentFilename, downloadAgentWorkspace } from './agents/workspace';
import * as client from './client';
import { TEMPORARY_WORKSPACE } from '@/lib/temporaryWorkspace';

// The app keys the temporary workspaces' project with a reserved string; the
// API knows them by the absence of a repository. These are the two places the
// one becomes the other, and the one way a workspace's files come out.

afterEach(() => vi.restoreAllMocks());

function respond(body: unknown = {}) {
  return vi
    .spyOn(client, 'fetchWithAuth')
    .mockImplementation(
      async () =>
        new Response(JSON.stringify(body), { headers: { 'Content-Type': 'application/json' } }),
    );
}

function query(fetch: ReturnType<typeof respond>) {
  return Object.fromEntries(new URL(fetch.mock.calls[0][1], 'http://test').searchParams);
}

describe('the temporary workspaces as a project', () => {
  it('asks for conversations without a repository instead of sending the reserved key', async () => {
    const fetch = respond({ conversations: [], next_cursor: null });

    await listAgentConversations({ repo: TEMPORARY_WORKSPACE });

    expect(query(fetch)).toEqual({ limit: '50', temporary: 'true' });
  });

  it('still asks for one repository by name', async () => {
    const fetch = respond({ conversations: [], next_cursor: null });

    await listAgentConversations({ repo: 'owner/name' });

    expect(query(fetch)).toEqual({ limit: '50', repo: 'owner/name' });
  });

  it('filters sessions the same way', async () => {
    const fetch = respond({ sessions: [], next_cursor: null, projects: [], total: 0 });

    await listAgentSessions({ repo: TEMPORARY_WORKSPACE });

    expect(query(fetch)).toMatchObject({ temporary: 'true' });
    expect(query(fetch)).not.toHaveProperty('repo');
  });
});

describe('downloading a workspace', () => {
  // How the control plane names the download: an ASCII fallback, and the
  // conversation's title in UTF-8 when it has one.
  const fallback = 'attachment; filename="workspace-athr_1.tar.gz"';
  const disposition = (encoded?: string) =>
    encoded === undefined ? fallback : `${fallback}; filename*=UTF-8''${encoded}`;

  it.each([
    [
      'the UTF-8 name when there is one',
      disposition('%E8%B0%83%E6%9F%A5%E7%BB%93%E6%9E%9C.tar.gz'),
      '调查结果.tar.gz',
    ],
    ['the plain name otherwise', disposition(), 'workspace-athr_1.tar.gz'],
    [
      'the plain name when the UTF-8 one is malformed',
      disposition('%E8%B0'),
      'workspace-athr_1.tar.gz',
    ],
    ['nothing without a header', null, null],
  ])('saves under %s', (_case, header, expected) => {
    expect(attachmentFilename(header)).toBe(expected);
  });

  it('fetches the archive and names it as the server asked', async () => {
    const fetch = vi.spyOn(client, 'fetchWithAuth').mockResolvedValue(
      new Response(new Uint8Array([0x1f, 0x8b]), {
        headers: {
          'Content-Type': 'application/gzip',
          'Content-Disposition': disposition('notes.tar.gz'),
        },
      }),
    );

    const { blob, filename } = await downloadAgentWorkspace('ajob/1');

    expect(fetch.mock.calls[0][1]).toBe('/v1/agent/jobs/ajob%2F1/workspace/archive');
    expect(filename).toBe('notes.tar.gz');
    expect(blob.size).toBe(2);
  });

  it('reports a refusal with the server’s words rather than saving them as the file', async () => {
    vi.spyOn(client, 'fetchWithAuth').mockResolvedValue(
      new Response(
        JSON.stringify({
          detail: 'The workspace is larger than this server’s 1024 MiB download limit.',
        }),
        { status: 413, headers: { 'Content-Type': 'application/json' } },
      ),
    );

    await expect(downloadAgentWorkspace('ajob_1')).rejects.toThrow(
      'The workspace is larger than this server’s 1024 MiB download limit.',
    );
  });
});
