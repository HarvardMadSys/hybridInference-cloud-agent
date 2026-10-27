// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { LocaleProvider } from '@/components/providers/LocaleProvider';
import { LOCALE_STORAGE_KEY } from '@/lib/i18n/locales';
import * as api from '@/lib/api/modelConnections';
import { ModelConnectionsSettings } from './ModelConnectionsSettings';

vi.mock('@/lib/api/modelConnections', () => ({
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
    fireEvent.change(screen.getByRole('combobox', { name: 'Model to test' }), {
      target: { value: 'another-model' },
    });
    fireEvent.change(screen.getByRole('combobox', { name: 'API to test' }), {
      target: { value: 'responses' },
    });
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
