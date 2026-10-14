// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { downloadAgentWorkspace } from '@/lib/api/agents';
import { translate } from '@/lib/i18n/translate';
import { TEMPORARY_WORKSPACE } from '@/lib/temporaryWorkspace';

import { projectLabel, projectTitle } from './conversations';
import { OutcomeCard } from './job-detail/RunOutcome';
import { WorkspaceDownloadButton } from './TemporaryWorkspace';
import type { AgentJob } from './types';

vi.mock('@/components/providers', async () => {
  const { translate } = await import('@/lib/i18n/translate');
  const t = (key: Parameters<typeof translate>[1], vars?: Parameters<typeof translate>[2]) =>
    translate('en', key, vars);
  return { useT: () => t };
});

vi.mock('@/lib/api/agents', () => ({ downloadAgentWorkspace: vi.fn() }));

function job(overrides: Partial<AgentJob> = {}): AgentJob {
  return {
    id: 'ajob_1',
    title: 'Plot the survey results',
    prompt: 'Plot the survey results',
    state: 'done',
    repo: TEMPORARY_WORKSPACE,
    baseSha: '77bbec2',
    branch: '',
    runtime: 'claude-code',
    model: 'qwen-test',
    spentUsd: 0,
    timeoutLabel: '',
    sandbox: '',
    attempts: [{ no: 1, status: 'finished' }],
    events: [],
    diffFiles: [],
    gates: [],
    usage: { tokensIn: '', tokensOut: '', cachePct: 0, turns: null },
    ...overrides,
  };
}

// The browser's own download: a link to an object URL, clicked once.
function captureDownloads() {
  const saved: { href: string; download: string; attached: boolean }[] = [];
  const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (
    this: HTMLAnchorElement,
  ) {
    saved.push({ href: this.href, download: this.download, attached: this.isConnected });
  });
  return { saved, click };
}

describe('WorkspaceDownloadButton', () => {
  const created = vi.fn(() => 'blob:workspace');
  const revoked = vi.fn();

  beforeEach(() => {
    vi.mocked(downloadAgentWorkspace).mockReset();
    created.mockClear();
    revoked.mockClear();
    Object.assign(URL, { createObjectURL: created, revokeObjectURL: revoked });
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it('saves the whole workspace under the name the server gave it', async () => {
    let finish!: (value: { blob: Blob; filename: string }) => void;
    vi.mocked(downloadAgentWorkspace).mockReturnValue(
      new Promise((resolve) => {
        finish = resolve;
      }),
    );
    const { saved } = captureDownloads();
    const timers = vi.spyOn(window, 'setTimeout');
    render(<WorkspaceDownloadButton jobId="ajob_1" />);

    fireEvent.click(screen.getByRole('button', { name: 'Download workspace' }));

    const pending = screen.getByRole('button', { name: 'Preparing download…' });
    expect(pending).toBeDisabled();
    expect(pending).toHaveAttribute('aria-busy', 'true');
    expect(downloadAgentWorkspace).toHaveBeenCalledExactlyOnceWith('ajob_1');

    const blob = new Blob(['archive']);
    await act(async () => finish({ blob, filename: 'Plot the survey results.tar.gz' }));

    expect(created).toHaveBeenCalledWith(blob);
    expect(saved).toEqual([
      { href: 'blob:workspace', download: 'Plot the survey results.tar.gz', attached: true },
    ]);
    // The link was only ever the means; it does not stay in the page.
    expect(document.querySelector('a[download]')).toBeNull();
    expect(screen.getByRole('button', { name: 'Download workspace' })).toBeEnabled();
    // Released later, once the browser has certainly taken the bytes.
    expect(revoked).not.toHaveBeenCalled();
    const release = timers.mock.calls.find(([, delay]) => delay === 60_000)?.[0];
    expect(release).toBeTypeOf('function');
    (release as () => void)();
    expect(revoked).toHaveBeenCalledWith('blob:workspace');
  });

  it('says why a download was refused, beside the button', async () => {
    vi.mocked(downloadAgentWorkspace).mockRejectedValue(
      new Error('The workspace is larger than this server’s 1024 MiB download limit.'),
    );
    const { click } = captureDownloads();
    render(<WorkspaceDownloadButton jobId="ajob_1" />);

    fireEvent.click(screen.getByRole('button', { name: 'Download workspace' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Could not download the workspace. The workspace is larger than this server’s 1024 MiB download limit.',
    );
    expect(screen.getByRole('button', { name: 'Download workspace' })).toBeEnabled();
    expect(click).not.toHaveBeenCalled();
  });
});

describe('a temporary workspace where a repository would be', () => {
  afterEach(() => cleanup());

  it('offers the download where a pull request would be, even with no changes listed', async () => {
    render(<OutcomeCard job={job()} onOpenDiff={() => {}} />);

    expect(screen.getByRole('button', { name: 'Download workspace' })).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Open draft PR' })).not.toBeInTheDocument();
  });

  it('keeps the download for a run that failed, whose files are still there', async () => {
    render(<OutcomeCard job={job({ state: 'failed' })} onOpenDiff={() => {}} />);

    await waitFor(() => expect(screen.getByText('Run failed')).toBeInTheDocument());
    expect(screen.getByRole('button', { name: 'Download workspace' })).toBeInTheDocument();
  });

  it('shows a repository conversation’s outcome as before', () => {
    const { container } = render(
      <OutcomeCard job={job({ repo: 'owner/repository' })} onOpenDiff={() => {}} />,
    );

    expect(container).toBeEmptyDOMElement();
  });

  it('is named, not shown as a key, in the sidebar and its tooltips', () => {
    const t = (key: Parameters<typeof translate>[1]) => translate('en', key);

    expect(projectLabel(TEMPORARY_WORKSPACE, t)).toBe('Start from scratch');
    expect(projectTitle(TEMPORARY_WORKSPACE, t)).toBe('Start from scratch');
    expect(projectLabel('owner/repository', t)).toBe('repository');
    expect(projectTitle('owner/repository', t)).toBe('owner/repository');
  });
});
