// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { StrictMode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import AgentsConnectedPage from './page';

const navigation = vi.hoisted(() => ({ search: '', replace: vi.fn() }));

vi.mock('next/navigation', () => ({
  useRouter: () => ({ replace: navigation.replace }),
  useSearchParams: () => new URLSearchParams(navigation.search),
}));

vi.mock('@/lib/api/agents', () => ({
  connectGitHub: vi.fn(),
  connectGitLab: vi.fn(),
}));

import { connectGitHub, connectGitLab } from '@/lib/api/agents';

const mockedConnectGitHub = connectGitHub as unknown as ReturnType<typeof vi.fn>;
const mockedConnectGitLab = connectGitLab as unknown as ReturnType<typeof vi.fn>;

describe('AgentsConnectedPage', () => {
  beforeEach(() => {
    sessionStorage.clear();
    navigation.search = '';
    navigation.replace.mockReset();
    mockedConnectGitHub.mockReset();
    mockedConnectGitLab.mockReset();
  });

  afterEach(() => {
    cleanup();
  });

  it('completes GitLab OAuth with the returned state', async () => {
    navigation.search = 'provider=gitlab&code=gitlab-code&state=signed-state';
    mockedConnectGitLab.mockResolvedValue({ provider: 'gitlab', connected: true });

    render(<AgentsConnectedPage />);

    await waitFor(() =>
      expect(mockedConnectGitLab).toHaveBeenCalledWith('gitlab-code', 'signed-state'),
    );
    expect(navigation.replace).toHaveBeenCalledWith('/settings/integrations?connected=gitlab');
  });

  it('defaults legacy callbacks without a provider to GitHub', async () => {
    navigation.search = 'code=github-code&state=signed-state';
    mockedConnectGitHub.mockResolvedValue({ connections: [{}], repos: [], install_url: null });

    render(<AgentsConnectedPage />);

    await waitFor(() =>
      expect(mockedConnectGitHub).toHaveBeenCalledWith('github-code', 'signed-state'),
    );
    expect(navigation.replace).toHaveBeenCalledWith('/settings/integrations?connected=github');
  });

  it('submits a single-use OAuth state only once in React Strict Mode', async () => {
    navigation.search = 'provider=github&code=github-code&state=signed-state';
    mockedConnectGitHub.mockResolvedValue({ connections: [{}], repos: [], install_url: null });

    render(
      <StrictMode>
        <AgentsConnectedPage />
      </StrictMode>,
    );

    await waitFor(() => expect(navigation.replace).toHaveBeenCalled());
    expect(mockedConnectGitHub).toHaveBeenCalledTimes(1);
  });

  it('offers App installation after OAuth finds no accessible installation', async () => {
    navigation.search = 'provider=github&code=github-code&state=signed-state';
    mockedConnectGitHub.mockResolvedValue({
      connections: [],
      repos: [],
      install_url: 'https://github.com/apps/freeinference/installations/new?state=fresh-state',
    });

    render(<AgentsConnectedPage />);

    const install = await screen.findByRole('link', { name: 'Install GitHub App' });
    expect(install).toHaveAttribute(
      'href',
      'https://github.com/apps/freeinference/installations/new?state=fresh-state',
    );
    expect(navigation.replace).not.toHaveBeenCalled();
  });

  it('goes back to the task the owner left for GitHub, once', async () => {
    sessionStorage.setItem(
      'agents.github.return',
      JSON.stringify({ value: '/?repo=owner%2Frepository', savedAt: Date.now() }),
    );
    navigation.search = 'provider=github&code=github-code&state=signed-state';
    mockedConnectGitHub.mockResolvedValue({ connections: [{}], repos: [], install_url: null });

    render(<AgentsConnectedPage />);

    await waitFor(() =>
      expect(navigation.replace).toHaveBeenCalledWith('/?repo=owner%2Frepository'),
    );
    expect(sessionStorage.getItem('agents.github.return')).toBeNull();
  });

  it('never follows a return path that leaves the app', async () => {
    sessionStorage.setItem(
      'agents.github.return',
      JSON.stringify({ value: '//evil.example/steal', savedAt: Date.now() }),
    );
    navigation.search = 'provider=github&code=github-code&state=signed-state';
    mockedConnectGitHub.mockResolvedValue({ connections: [{}], repos: [], install_url: null });

    render(<AgentsConnectedPage />);

    await waitFor(() =>
      expect(navigation.replace).toHaveBeenCalledWith('/settings/integrations?connected=github'),
    );
  });

  it('keeps the way back while the App still has to be installed', async () => {
    sessionStorage.setItem(
      'agents.github.return',
      JSON.stringify({ value: '/', savedAt: Date.now() }),
    );
    navigation.search = 'provider=github&code=github-code&state=signed-state';
    mockedConnectGitHub.mockResolvedValue({
      connections: [],
      repos: [],
      install_url: 'https://github.com/apps/freeinference/installations/new?state=fresh-state',
    });

    render(<AgentsConnectedPage />);

    expect(await screen.findByRole('link', { name: 'Install GitHub App' })).toBeInTheDocument();
    // Installing comes back through this page, which then returns to the task.
    expect(sessionStorage.getItem('agents.github.return')).not.toBeNull();
  });

  it('asks to install on the owner’s own account when only an organization has the App', async () => {
    sessionStorage.setItem(
      'agents.github.return',
      JSON.stringify({ value: '/?repo=acme%2Fservice', savedAt: Date.now() }),
    );
    navigation.search = 'provider=github&code=github-code&state=signed-state';
    mockedConnectGitHub.mockResolvedValue({
      connections: [{ installation_id: 77, account_login: 'acme' }],
      repos: ['acme/service'],
      install_url: 'https://github.com/apps/freeinference/installations/new?state=own',
    });

    render(<AgentsConnectedPage />);

    expect(await screen.findByText('Use your own repositories too')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Install on my account' })).toHaveAttribute(
      'href',
      'https://github.com/apps/freeinference/installations/new?state=own',
    );
    expect(navigation.replace).not.toHaveBeenCalled();

    // Not now is a real answer: back to where the owner came from, connected.
    fireEvent.click(screen.getByRole('button', { name: 'Not now' }));
    expect(navigation.replace).toHaveBeenCalledWith('/?repo=acme%2Fservice');
  });

  it('lets the owner skip installing, back to Integrations when they came from there', async () => {
    navigation.search = 'provider=github&code=github-code&state=signed-state';
    mockedConnectGitHub.mockResolvedValue({
      connections: [],
      repos: [],
      install_url: 'https://github.com/apps/freeinference/installations/new?state=fresh-state',
    });

    render(<AgentsConnectedPage />);

    fireEvent.click(await screen.findByRole('button', { name: 'Not now' }));
    expect(navigation.replace).toHaveBeenCalledWith('/settings/integrations?connected=github');
  });

  // Back from GitHub's install page, or a refresh, opens the same callback
  // again. Its code worked once; sending it again was refused and showed an
  // error for a connection that had succeeded.
  it('does not send a callback this tab already completed', async () => {
    navigation.search = 'provider=github&code=github-code&state=signed-state';
    mockedConnectGitHub.mockResolvedValue({
      connections: [{ installation_id: 77, account_login: 'acme' }],
      repos: ['acme/service'],
      install_url: 'https://github.com/apps/freeinference/installations/new?state=own',
    });

    render(<AgentsConnectedPage />);
    expect(await screen.findByText('Use your own repositories too')).toBeInTheDocument();
    cleanup();

    render(<AgentsConnectedPage />);

    await waitFor(() =>
      expect(navigation.replace).toHaveBeenCalledWith('/settings/integrations?connected=github'),
    );
    expect(mockedConnectGitHub).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(/Could not connect/)).not.toBeInTheDocument();
  });

  it('sends a callback again when the first attempt failed', async () => {
    navigation.search = 'provider=github&code=github-code&state=signed-state';
    mockedConnectGitHub
      .mockRejectedValueOnce(new Error('GitHub is unavailable'))
      .mockResolvedValue({ connections: [{}], repos: [], install_url: null });

    render(<AgentsConnectedPage />);
    expect(await screen.findByText('GitHub is unavailable')).toBeInTheDocument();
    cleanup();

    render(<AgentsConnectedPage />);

    await waitFor(() =>
      expect(navigation.replace).toHaveBeenCalledWith('/settings/integrations?connected=github'),
    );
    expect(mockedConnectGitHub).toHaveBeenCalledTimes(2);
  });

  it('words a provider error itself instead of repeating the link', async () => {
    // Anyone can write this URL, description included.
    navigation.search = new URLSearchParams({
      provider: 'github',
      error: 'access_denied',
      error_description: 'Your GitHub account is locked. Call +1 555 0100 to unlock it.',
    }).toString();

    render(<AgentsConnectedPage />);

    expect(
      await screen.findByText('Access was cancelled or denied (access_denied).'),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Call \+1 555 0100/)).not.toBeInTheDocument();
    expect(mockedConnectGitHub).not.toHaveBeenCalled();
  });

  it('quotes an unknown provider error as unverified detail', async () => {
    navigation.search = new URLSearchParams({
      provider: 'gitlab',
      error_description: 'Your session expired; sign in at evil.example',
    }).toString();

    render(<AgentsConnectedPage />);

    expect(
      await screen.findByText(
        'GitLab did not complete the authorization. Unverified detail from the redirect: ' +
          '“Your session expired; sign in at evil.example”',
      ),
    ).toBeInTheDocument();
    expect(mockedConnectGitLab).not.toHaveBeenCalled();
  });

  it('does not exchange a code without connection state', async () => {
    navigation.search = 'provider=gitlab&code=gitlab-code';

    render(<AgentsConnectedPage />);

    expect(
      await screen.findByText('GitLab did not return the connection state.'),
    ).toBeInTheDocument();
    expect(mockedConnectGitLab).not.toHaveBeenCalled();
  });
});
