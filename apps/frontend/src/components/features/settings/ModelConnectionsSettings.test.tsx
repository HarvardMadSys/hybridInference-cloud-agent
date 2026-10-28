// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { LocaleProvider } from '@/components/providers/LocaleProvider';
import { LOCALE_STORAGE_KEY } from '@/lib/i18n/locales';
import * as api from '@/lib/api/modelConnections';
import { ModelConnectionsSettings } from './ModelConnectionsSettings';

// The calls are mocked; the protocol vocabulary is the real one.
vi.mock('@/lib/api/modelConnections', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/modelConnections')>()),
  listModelConnections: vi.fn(),
  createModelConnection: vi.fn(),
  updateModelConnection: vi.fn(),
  deleteModelConnection: vi.fn(),
  checkModelConnection: vi.fn(),
  discoverConnectionModels: vi.fn(),
}));

const connection: api.ModelConnection = {
  id: 'mconn_01234567890123456789012345678901',
  name: 'My provider',
  base_url: 'https://api.example.com/custom/v1',
  protocols: ['chat_completions', 'responses'],
  models: ['shared-model', 'another-model'],
  enabled: true,
  has_api_key: true,
  runtimes: ['opencode', 'kilo', 'pi', 'codex'],
  last_check: null,
  created_at: '2026-10-27T00:00:00Z',
  updated_at: '2026-10-27T00:00:00Z',
};

/** The labels a themed select's menu offers; the menu closes again with Escape. */
function menuLabels(combobox: HTMLElement): (string | null)[] {
  fireEvent.click(combobox);
  const label = (combobox as HTMLButtonElement).labels?.[0]?.textContent ?? '';
  const menu = screen.getByRole('listbox', { name: label });
  const labels = within(menu)
    .getAllByRole('option')
    .map((option) => option.textContent);
  fireEvent.keyDown(menu, { key: 'Escape' });
  expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
  return labels;
}

/** Choose an option from a themed select's menu with the pointer. */
function choose(combobox: HTMLElement, label: string) {
  fireEvent.click(combobox);
  fireEvent.click(screen.getByRole('option', { name: label }));
  expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
}

function renderSettings() {
  return render(
    <LocaleProvider>
      <ModelConnectionsSettings />
    </LocaleProvider>,
  );
}

beforeEach(() => {
  vi.resetAllMocks();
  localStorage.clear();
  localStorage.setItem(LOCALE_STORAGE_KEY, 'en');
  vi.mocked(api.listModelConnections).mockResolvedValue({
    configured: true,
    enabled: true,
    connections: [connection],
  });
});
afterEach(cleanup);

describe('personal model connections', () => {
  it('saves a manually configured connection without discovery or billable tests', async () => {
    vi.mocked(api.createModelConnection).mockResolvedValue({
      ...connection,
      id: 'new',
      name: 'Second provider',
    });
    renderSettings();
    fireEvent.click(await screen.findByRole('button', { name: 'Add connection' }));
    fireEvent.change(screen.getByLabelText('Connection name'), {
      target: { value: 'Second provider' },
    });
    fireEvent.change(screen.getByLabelText('Base URL'), {
      target: { value: 'https://api.example.com/custom/v1' },
    });
    const key = screen.getByLabelText('API key');
    expect(key).toHaveAttribute('type', 'password');
    fireEvent.change(key, { target: { value: 'private-provider-key' } });
    fireEvent.change(screen.getByLabelText('Model IDs'), {
      target: { value: 'shared-model\nshared-model\nanother-model' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save connection' }));
    await waitFor(() =>
      expect(api.createModelConnection).toHaveBeenCalledWith({
        name: 'Second provider',
        base_url: connection.base_url,
        api_key: 'private-provider-key',
        protocols: ['chat_completions'],
        models: connection.models,
      }),
    );
    expect(await screen.findByRole('article', { name: 'Second provider' })).toBeVisible();
    expect(screen.queryByDisplayValue('private-provider-key')).not.toBeInTheDocument();
    expect(api.checkModelConnection).not.toHaveBeenCalled();
    expect(api.discoverConnectionModels).not.toHaveBeenCalled();
  });

  it('never reads back a saved key and omits a blank key on edit', async () => {
    vi.mocked(api.updateModelConnection).mockResolvedValue({
      ...connection,
      name: 'Renamed',
      updated_at: 'later',
    });
    renderSettings();
    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }));
    expect(screen.getByLabelText('API key')).toHaveValue('');
    expect(screen.getByLabelText('API key')).not.toBeRequired();
    fireEvent.change(screen.getByLabelText('Connection name'), { target: { value: 'Renamed' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save connection' }));
    await waitFor(() =>
      expect(api.updateModelConnection).toHaveBeenCalledWith(connection.id, {
        name: 'Renamed',
        base_url: connection.base_url,
        protocols: connection.protocols,
        models: connection.models,
      }),
    );
    expect(await screen.findByRole('article', { name: 'Renamed' })).toBeVisible();
  });

  it('sends an explicitly entered replacement key and clears the form on success', async () => {
    vi.mocked(api.updateModelConnection).mockResolvedValue({ ...connection, updated_at: 'later' });
    renderSettings();
    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }));
    fireEvent.change(screen.getByLabelText('API key'), { target: { value: 'replacement-key' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save connection' }));
    await waitFor(() =>
      expect(api.updateModelConnection).toHaveBeenCalledWith(
        connection.id,
        expect.objectContaining({ api_key: 'replacement-key' }),
      ),
    );
    await waitFor(() => expect(screen.queryByLabelText('API key')).not.toBeInTheDocument());
  });

  it('requires confirmation for disable and delete and does not claim provider revocation', async () => {
    vi.mocked(api.updateModelConnection).mockResolvedValue({
      ...connection,
      enabled: false,
      updated_at: 'later',
    });
    vi.mocked(api.deleteModelConnection).mockResolvedValue(undefined);
    renderSettings();
    fireEvent.click(await screen.findByRole('button', { name: 'Disable' }));
    expect(api.updateModelConnection).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));
    await waitFor(() =>
      expect(api.updateModelConnection).toHaveBeenCalledWith(connection.id, { enabled: false }),
    );
    await screen.findByRole('button', { name: 'Enable' });
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
    expect(screen.getByText(/does not revoke the key at your provider/)).toBeVisible();
    expect(api.deleteModelConnection).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));
    await waitFor(() => expect(api.deleteModelConnection).toHaveBeenCalledWith(connection.id));
    expect(await screen.findByText('No personal connections yet.')).toBeVisible();
  });

  it('tests only the selected model and protocol and labels stale results', async () => {
    vi.mocked(api.checkModelConnection).mockResolvedValue({
      status: 'supported',
      category: 'supported',
      message: 'The test completed.',
      protocol: 'responses',
      model: 'another-model',
      streaming: true,
      tool_call: true,
      final_answer: true,
      http_status: 200,
      checked_at: 'now',
      recorded: false,
    });
    renderSettings();
    const card = await screen.findByRole('article', { name: connection.name });
    fireEvent.click(within(card).getByText('Test model', { selector: 'summary' }));
    const model = screen.getByRole('combobox', { name: 'Model to test' });
    const protocol = screen.getByRole('combobox', { name: 'API to test' });
    expect(model).toHaveTextContent('shared-model');
    expect(protocol).toHaveTextContent('Chat Completions');
    choose(model, 'another-model');
    expect(model).toHaveTextContent('another-model');
    // The keyboard reaches the same menu: open on the chosen option, step, choose.
    fireEvent.keyDown(protocol, { key: 'ArrowDown' });
    const menu = screen.getByRole('listbox', { name: 'API to test' });
    expect(within(menu).getByRole('option', { selected: true })).toHaveTextContent(
      'Chat Completions',
    );
    fireEvent.keyDown(menu, { key: 'ArrowDown' });
    fireEvent.keyDown(menu, { key: 'Enter' });
    expect(protocol).toHaveTextContent('Responses');
    expect(protocol).toHaveFocus();
    fireEvent.click(screen.getByRole('button', { name: 'Test model' }));
    await waitFor(() =>
      expect(api.checkModelConnection).toHaveBeenCalledWith(
        connection.id,
        'another-model',
        'responses',
      ),
    );
    expect(await screen.findByText(/connection changed during this test/)).toBeVisible();
    expect(screen.getByText(/another-model · Responses\. Other models/)).toBeVisible();
  });

  it('adds only explicitly selected discovered IDs and does not run a test', async () => {
    vi.mocked(api.discoverConnectionModels).mockResolvedValue([
      'shared-model',
      'third-model',
      'fourth-model',
    ]);
    vi.mocked(api.updateModelConnection).mockResolvedValue({
      ...connection,
      models: [...connection.models, 'third-model'],
      updated_at: 'later',
    });
    renderSettings();
    await screen.findByRole('article', { name: connection.name });
    fireEvent.click(screen.getByText('Discover model IDs', { selector: 'summary' }));
    fireEvent.click(screen.getByRole('button', { name: 'Discover model IDs' }));
    fireEvent.click(await screen.findByRole('checkbox', { name: 'third-model' }));
    fireEvent.click(screen.getByRole('button', { name: 'Add selected IDs' }));
    await waitFor(() =>
      expect(api.updateModelConnection).toHaveBeenCalledWith(connection.id, {
        models: [...connection.models, 'third-model'],
      }),
    );
    expect(api.checkModelConnection).not.toHaveBeenCalled();
  });

  it('keeps the form for correction but never renders the raw failure or key', async () => {
    vi.mocked(api.updateModelConnection).mockRejectedValue(
      new Error('upstream echoed replacement-key'),
    );
    renderSettings();
    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }));
    fireEvent.change(screen.getByLabelText('API key'), { target: { value: 'replacement-key' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save connection' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('The request failed.');
    expect(screen.queryByText(/upstream echoed/)).not.toBeInTheDocument();
    expect(screen.getByLabelText('API key')).toHaveAttribute('type', 'password');
  });

  it('allows cleanup while the deployment gate is off but prevents new use', async () => {
    vi.mocked(api.listModelConnections).mockResolvedValue({
      configured: true,
      enabled: false,
      connections: [{ ...connection, enabled: false }],
    });
    renderSettings();
    expect(await screen.findByText(/not available on this deployment/)).toBeVisible();
    expect(screen.queryByRole('button', { name: 'Add connection' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Edit' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Enable' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Delete' })).toBeEnabled();
  });
});

describe('an Anthropic Messages connection', () => {
  const messages: api.ModelConnection = {
    ...connection,
    id: 'mconn_abcdefabcdefabcdefabcdefabcdefab',
    name: 'Claude provider',
    base_url: 'https://gateway.example/anthropic/v1',
    protocols: ['anthropic_messages'],
    models: ['claude-sonnet-4-5'],
    runtimes: ['claude-code'],
  };

  it('offers Anthropic Messages for Claude Code and saves it on its own', async () => {
    vi.mocked(api.createModelConnection).mockResolvedValue(messages);
    renderSettings();
    fireEvent.click(await screen.findByRole('button', { name: 'Add connection' }));
    const anthropic = screen.getByRole('checkbox', { name: 'Anthropic Messages · Claude Code' });
    expect(anthropic).not.toBeChecked();
    expect(screen.getByText(/Claude Code adds \/v1\/messages/)).toBeVisible();
    expect(screen.queryByText(/cannot use these connections/)).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Connection name'), {
      target: { value: 'Claude provider' },
    });
    fireEvent.change(screen.getByLabelText('Base URL'), { target: { value: messages.base_url } });
    fireEvent.change(screen.getByLabelText('API key'), { target: { value: 'sk-ant-private' } });
    fireEvent.change(screen.getByLabelText('Model IDs'), {
      target: { value: 'claude-sonnet-4-5' },
    });
    fireEvent.click(anthropic);
    fireEvent.click(
      screen.getByRole('checkbox', { name: 'Chat Completions · OpenCode, Kilo, pi' }),
    );
    fireEvent.click(screen.getByRole('button', { name: 'Save connection' }));
    await waitFor(() =>
      expect(api.createModelConnection).toHaveBeenCalledWith({
        name: 'Claude provider',
        base_url: messages.base_url,
        api_key: 'sk-ant-private',
        protocols: ['anthropic_messages'],
        models: ['claude-sonnet-4-5'],
      }),
    );
  });

  it('tests the selected model over Anthropic Messages and discovers its IDs', async () => {
    vi.mocked(api.listModelConnections).mockResolvedValue({
      configured: true,
      enabled: true,
      connections: [messages],
    });
    vi.mocked(api.checkModelConnection).mockResolvedValue({
      status: 'supported',
      category: 'supported',
      message: 'Streaming and a tool call round trip both worked.',
      protocol: 'anthropic_messages',
      model: 'claude-sonnet-4-5',
      streaming: true,
      tool_call: true,
      final_answer: true,
      http_status: 200,
      checked_at: 'now',
      recorded: true,
    });
    vi.mocked(api.discoverConnectionModels).mockResolvedValue(['claude-opus-4-1']);
    renderSettings();
    const card = await screen.findByRole('article', { name: 'Claude provider' });
    fireEvent.click(within(card).getByText('Test model', { selector: 'summary' }));
    const protocol = screen.getByRole('combobox', { name: 'API to test' });
    expect(menuLabels(protocol)).toEqual(['Anthropic Messages']);
    fireEvent.click(screen.getByRole('button', { name: 'Test model' }));
    await waitFor(() =>
      expect(api.checkModelConnection).toHaveBeenCalledWith(
        messages.id,
        'claude-sonnet-4-5',
        'anthropic_messages',
      ),
    );
    expect(await screen.findByText(/claude-sonnet-4-5 · Anthropic Messages\./)).toBeVisible();
    fireEvent.click(screen.getByText('Discover model IDs', { selector: 'summary' }));
    fireEvent.click(screen.getByRole('button', { name: 'Discover model IDs' }));
    expect(await screen.findByRole('checkbox', { name: 'claude-opus-4-1' })).toBeVisible();
    expect(api.discoverConnectionModels).toHaveBeenCalledWith(messages.id);
  });
});

describe('a Chat Completions connection used through translation', () => {
  const chat: api.ModelConnection = {
    ...connection,
    id: 'mconn_c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0',
    name: 'Chat provider',
    protocols: ['chat_completions'],
    models: ['chat-model'],
    runtimes: ['claude-code', 'codex', 'kilo', 'opencode', 'pi'],
    translated_runtimes: ['claude-code', 'codex'],
  };

  it('names the translated agents and tests the translated path on request', async () => {
    vi.mocked(api.listModelConnections).mockResolvedValue({
      configured: true,
      enabled: true,
      connections: [chat],
    });
    vi.mocked(api.checkModelConnection).mockResolvedValue({
      status: 'supported',
      category: 'supported',
      message: 'Streaming and a tool call round trip both worked.',
      protocol: 'responses',
      upstream_protocol: 'chat_completions',
      mode: 'translated',
      model: 'chat-model',
      streaming: true,
      tool_call: true,
      final_answer: true,
      http_status: 200,
      checked_at: 'now',
      recorded: true,
    });
    renderSettings();
    const card = await screen.findByRole('article', { name: 'Chat provider' });
    expect(card).toHaveTextContent('codex (via Chat Completions)');
    expect(card).toHaveTextContent('claude-code (via Chat Completions)');
    expect(card).not.toHaveTextContent('pi (via');
    // Nothing is tested until asked: opening the page spends no quota.
    expect(api.checkModelConnection).not.toHaveBeenCalled();
    fireEvent.click(within(card).getByText('Test model', { selector: 'summary' }));
    const protocol = within(card).getByRole('combobox', { name: 'API to test' });
    expect(menuLabels(protocol)).toEqual([
      'Chat Completions',
      'Responses via Chat Completions',
      'Anthropic Messages via Chat Completions',
    ]);
    choose(protocol, 'Responses via Chat Completions');
    // The menu shows the translated label but submits the exact protocol value.
    expect(protocol).toHaveTextContent('Responses via Chat Completions');
    fireEvent.click(within(card).getByRole('button', { name: 'Test model' }));
    // Both menus are disabled while the test runs.
    expect(protocol).toBeDisabled();
    expect(within(card).getByRole('combobox', { name: 'Model to test' })).toBeDisabled();
    await waitFor(() =>
      expect(api.checkModelConnection).toHaveBeenCalledWith(chat.id, 'chat-model', 'responses'),
    );
    expect(
      await within(card).findByText(/chat-model · Responses via Chat Completions\./),
    ).toBeVisible();
  });

  it('offers no translated test where the connection speaks the agent API itself', async () => {
    vi.mocked(api.listModelConnections).mockResolvedValue({
      configured: true,
      enabled: true,
      connections: [
        { ...chat, protocols: ['chat_completions', 'responses', 'anthropic_messages'] },
      ],
    });
    renderSettings();
    const card = await screen.findByRole('article', { name: 'Chat provider' });
    fireEvent.click(within(card).getByText('Test model', { selector: 'summary' }));
    const protocol = within(card).getByRole('combobox', { name: 'API to test' });
    expect(menuLabels(protocol)).toEqual(['Chat Completions', 'Responses', 'Anthropic Messages']);
  });
});
