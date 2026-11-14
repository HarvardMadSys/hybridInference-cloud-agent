// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { chooseOption, optionNames } from '@/components/ui/__tests__/select';
import * as api from '@/lib/api/adminOperatorModels';

import ProvidedModelsSettings from './ProvidedModels';
import {
  daily,
  PROVIDED_ID,
  providedConnection,
  providedList,
  refusal,
  refusedModel,
} from './providedModelsTestFixtures';

// The calls are mocked; the limits, the error helpers and the vocabulary are the real ones.
vi.mock('@/lib/api/adminOperatorModels', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/adminOperatorModels')>()),
  listOperatorModelConnections: vi.fn(),
  createOperatorModelConnection: vi.fn(),
  updateOperatorModelConnection: vi.fn(),
  deleteOperatorModelConnection: vi.fn(),
  setOperatorModelLimit: vi.fn(),
  listOperatorModelUsers: vi.fn(),
  setOperatorModelUserLimit: vi.fn(),
  clearOperatorModelUserLimit: vi.fn(),
  getOperatorModelUsage: vi.fn(),
}));

const BOTH_SET = { 'house-fast': daily(1500), 'house-deep': daily(0) };

function section() {
  return screen.getByRole('region', { name: 'Models this deployment provides' });
}

async function card(name = 'House models') {
  return screen.findByRole('article', { name });
}

function type(label: string | RegExp, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
}

/** Fill the new-connection form; the last model ID stays typed and still counts. */
function fillNew(models: string[] = ['house-fast', 'house-deep']) {
  type('Name', 'House models');
  type('Base URL', 'https://api.example.com/v1');
  type('API key', 'sk-deployment-key');
  const input = screen.getByLabelText('Model IDs');
  models.forEach((model, index) => {
    fireEvent.change(input, { target: { value: model } });
    if (index < models.length - 1) fireEvent.keyDown(input, { key: 'Enter' });
  });
}

/** A request the test settles by hand, to look at the page while it is pending. */
function pending<T>() {
  let resolve!: (value: T) => void;
  let reject!: (cause: unknown) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

beforeEach(() => {
  vi.resetAllMocks();
  localStorage.clear();
  vi.mocked(api.listOperatorModelConnections).mockResolvedValue(providedList());
});
afterEach(cleanup);

describe('the list of provided connections', () => {
  it('shows each connection with what it is and whether it is on', async () => {
    vi.mocked(api.listOperatorModelConnections).mockResolvedValue(
      providedList([
        providedConnection({
          enabled: true,
          is_default: true,
          default_model: 'house-fast',
          end_user_header: 'X-End-User',
          user_concurrency: 6,
          limits: BOTH_SET,
        }),
        providedConnection({ id: 'oconn_ffffffffffffffffffffffffffffffff', name: 'Spare' }),
      ]),
    );
    render(<ProvidedModelsSettings />);
    const house = await card();
    expect(house).toHaveTextContent('https://api.example.com/v1');
    expect(
      within(house)
        .getAllByRole('listitem')
        .slice(0, 5)
        .map((item) => item.textContent),
    ).toEqual([
      'Enabled',
      'Default for new users',
      'Default model: house-fast',
      'Up to 6 requests in flight per account',
      'End-user header: X-End-User',
    ]);
    // Each model's verification reads as on an owner's connection.
    expect(within(house).getAllByText('Works with Claude Code, Codex')).toHaveLength(2);
    expect(within(await card('Spare')).getByText('Disabled')).toBeInTheDocument();
    // The key is write-only: nothing on the page shows or hints at one.
    expect(section()).not.toHaveTextContent(/sk-/);
  });

  it('says so when there are none yet', async () => {
    vi.mocked(api.listOperatorModelConnections).mockResolvedValue(providedList([]));
    render(<ProvidedModelsSettings />);
    expect(await screen.findByText('No provided connections yet.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Add a provided connection' })).toBeEnabled();
  });

  it('warns that no server can run provided models yet', async () => {
    vi.mocked(api.listOperatorModelConnections).mockResolvedValue(
      providedList(undefined, { relay_hosts: 0 }),
    );
    render(<ProvidedModelsSettings />);
    await card();
    expect(within(section()).getByText(/No server can run provided models yet/)).toHaveAttribute(
      'role',
      'alert',
    );
  });

  it('has no such warning while a server reaches the relay', async () => {
    render(<ProvidedModelsSettings />);
    await card();
    expect(screen.queryByText(/No server can run provided models yet/)).not.toBeInTheDocument();
    expect(screen.queryByText(/switched off on this deployment/)).not.toBeInTheDocument();
  });

  it('says when model connections are switched off, and offers nothing the server would refuse', async () => {
    vi.mocked(api.listOperatorModelConnections).mockResolvedValue(
      providedList([providedConnection({ limits: BOTH_SET })], { feature_enabled: false }),
    );
    render(<ProvidedModelsSettings />);
    expect(
      await screen.findByText(
        /Model connections are switched off on this deployment\. Nothing here is offered to anyone/,
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Add a provided connection' })).toBeDisabled();
    // Every model has its allowance, and it still cannot be enabled.
    const enable = within(await card()).getByRole('button', { name: 'Enable' });
    expect(enable).toBeDisabled();
    expect(enable).toHaveAccessibleDescription(
      'It cannot be enabled while model connections are switched off on this deployment.',
    );
  });

  it('reads the server’s refusal for the feature being off as that, not as lost admin access', async () => {
    vi.mocked(api.listOperatorModelConnections).mockResolvedValue(
      providedList([providedConnection({ limits: BOTH_SET })]),
    );
    // Switched off after this page was read: the server answers 403 for an enable.
    vi.mocked(api.updateOperatorModelConnection).mockRejectedValue(
      refusal('model_connections_disabled', 'Model connections are switched off.', 403),
    );
    const onAccessDenied = vi.fn();
    render(<ProvidedModelsSettings onAccessDenied={onAccessDenied} />);
    const house = await card();
    fireEvent.click(within(house).getByRole('button', { name: 'Enable' }));
    expect(await within(house).findByRole('alert')).toHaveTextContent(
      'Model connections are switched off on this deployment. Switch them on before adding, verifying or enabling a provided connection.',
    );
    expect(onAccessDenied).not.toHaveBeenCalled();
  });

  it('warns since when the provider has refused the stored key', async () => {
    const since = '2026-11-11T08:30:00Z';
    vi.mocked(api.listOperatorModelConnections).mockResolvedValue(
      providedList([providedConnection({ key_refused_at: since })]),
    );
    render(<ProvidedModelsSettings />);
    const house = await card();
    expect(
      within(house).getByText(
        `The provider has refused the stored API key since ${new Date(since).toLocaleString()}. Requests on this connection fail until a working key is saved.`,
      ),
    ).toHaveAttribute('role', 'alert');
  });

  it('offers a retry when the list cannot be read', async () => {
    vi.mocked(api.listOperatorModelConnections)
      .mockRejectedValueOnce(refusal('SERVER_ERROR', 'HTTP 500', 500))
      .mockResolvedValue(providedList());
    render(<ProvidedModelsSettings />);
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Server error. Please try again in a moment');
    expect(screen.queryByRole('button', { name: 'Add a provided connection' })).toBeNull();
    fireEvent.click(within(alert).getByRole('button', { name: 'Retry' }));
    expect(await card()).toBeInTheDocument();
    expect(screen.queryByText(/Server error/)).not.toBeInTheDocument();
  });

  it('hands an ended admin session to the page', async () => {
    const denied = refusal('forbidden', 'Admin access required.', 403);
    vi.mocked(api.listOperatorModelConnections).mockRejectedValue(denied);
    const onAccessDenied = vi.fn();
    render(<ProvidedModelsSettings onAccessDenied={onAccessDenied} />);
    await waitFor(() => expect(onAccessDenied).toHaveBeenCalledWith(denied));
  });
});

describe('adding a provided connection', () => {
  beforeEach(() => {
    vi.mocked(api.listOperatorModelConnections).mockResolvedValue(providedList([]));
  });

  it('verifies and saves it disabled, with a visible pending state meanwhile', async () => {
    const save = pending<api.OperatorModelConnection>();
    vi.mocked(api.createOperatorModelConnection).mockReturnValue(save.promise);
    render(<ProvidedModelsSettings />);
    fireEvent.click(await screen.findByRole('button', { name: 'Add a provided connection' }));
    const form = screen.getByRole('form', { name: 'New provided connection' });
    expect(within(form).getByLabelText('API key')).toHaveAttribute('type', 'password');
    expect(within(form).getByRole('button', { name: 'Verify and save' })).toBeDisabled();
    // Said before anything is sent: it is saved disabled, and the provider may bill the check.
    expect(form).toHaveTextContent(/The connection is saved disabled/);
    expect(form).toHaveTextContent(/may bill/);
    // There is no provider list to load for a provided connection.
    expect(within(form).queryByRole('button', { name: /Load models/ })).toBeNull();

    fillNew();
    expect(optionNames(within(form).getByLabelText('Default model'))).toEqual([
      'None',
      'house-fast',
      'house-deep',
    ]);
    chooseOption(within(form).getByLabelText('Default model'), 'house-deep');
    type(/^End-user header/, 'X-End-User');
    type('Requests in flight per account', '6');
    fireEvent.click(within(form).getByRole('button', { name: 'Verify and save' }));

    // The save can take minutes: the form says so and cannot be sent twice or cancelled.
    expect(
      await within(form).findByText(/Verifying every model with the provider…/),
    ).toBeInTheDocument();
    expect(form).toHaveTextContent('this can take up to four minutes');
    const busy = within(form).getByRole('button', { name: 'Verifying…' });
    expect(busy).toBeDisabled();
    expect(busy).toHaveAttribute('aria-busy', 'true');
    expect(within(form).getByRole('button', { name: 'Cancel' })).toBeDisabled();
    expect(within(form).getByLabelText('Name')).toBeDisabled();
    expect(api.createOperatorModelConnection).toHaveBeenCalledExactlyOnceWith({
      name: 'House models',
      base_url: 'https://api.example.com/v1',
      api_key: 'sk-deployment-key',
      models: ['house-fast', 'house-deep'],
      default_model: 'house-deep',
      end_user_header: 'X-End-User',
      user_concurrency: 6,
    });

    save.resolve(providedConnection({ default_model: 'house-deep', user_concurrency: 6 }));
    const saved = await card();
    expect(within(saved).getByText('Disabled')).toBeInTheDocument();
    expect(screen.queryByRole('form')).not.toBeInTheDocument();
    expect(api.updateOperatorModelConnection).not.toHaveBeenCalled();
    expect(section()).not.toHaveTextContent('sk-deployment-key');
  });

  it('makes it the default for new users with an edit, once it exists', async () => {
    vi.mocked(api.createOperatorModelConnection).mockResolvedValue(providedConnection());
    vi.mocked(api.updateOperatorModelConnection).mockResolvedValue(
      providedConnection({ is_default: true }),
    );
    render(<ProvidedModelsSettings />);
    fireEvent.click(await screen.findByRole('button', { name: 'Add a provided connection' }));
    fillNew();
    fireEvent.click(screen.getByRole('checkbox', { name: /Default for new users/ }));
    fireEvent.click(screen.getByRole('button', { name: 'Verify and save' }));

    expect(await within(await card()).findByText('Default for new users')).toBeInTheDocument();
    expect(vi.mocked(api.createOperatorModelConnection).mock.calls[0][0]).not.toHaveProperty(
      'is_default',
    );
    expect(api.updateOperatorModelConnection).toHaveBeenCalledExactlyOnceWith(PROVIDED_ID, {
      is_default: true,
    });
  });

  it('keeps a saved connection, and says so, when making it the default fails', async () => {
    vi.mocked(api.createOperatorModelConnection).mockResolvedValue(providedConnection());
    vi.mocked(api.updateOperatorModelConnection).mockRejectedValue(
      refusal('operator_model_connection_not_found', 'Gone.', 404),
    );
    render(<ProvidedModelsSettings />);
    fireEvent.click(await screen.findByRole('button', { name: 'Add a provided connection' }));
    fillNew();
    fireEvent.click(screen.getByRole('checkbox', { name: /Default for new users/ }));
    fireEvent.click(screen.getByRole('button', { name: 'Verify and save' }));

    expect(
      await screen.findByText(
        /The connection was saved, but it could not be made the default for new users: This connection no longer exists/,
      ),
    ).toHaveAttribute('role', 'alert');
    expect(await card()).toBeInTheDocument();
    expect(screen.queryByRole('form')).not.toBeInTheDocument();
  });

  it('refuses a form it can see is wrong, without calling the server', async () => {
    render(<ProvidedModelsSettings />);
    fireEvent.click(await screen.findByRole('button', { name: 'Add a provided connection' }));
    fillNew(['house-fast']);
    type('Base URL', 'http://api.example.com/v1');
    type(/^End-user header/, 'X End: User');
    type('Requests in flight per account', '0');
    fireEvent.click(screen.getByRole('button', { name: 'Verify and save' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Check the highlighted fields, then try again.',
    );
    expect(screen.getByLabelText('Base URL')).toHaveAccessibleDescription(
      /Enter an HTTPS Base URL/,
    );
    expect(screen.getByLabelText(/^End-user header/)).toBeInvalid();
    expect(screen.getByLabelText('Requests in flight per account')).toHaveAccessibleDescription(
      /Enter a whole number from 1 to 64/,
    );
    expect(api.createOperatorModelConnection).not.toHaveBeenCalled();
  });

  it('shows each model’s result when the provider refuses one, and saves nothing', async () => {
    vi.mocked(api.createOperatorModelConnection).mockRejectedValue(
      refusal('model_connection_verification_failed', 'A model could not be verified.', 422, {
        models: [
          refusedModel('house-fast', 'auth'),
          refusedModel('house-deep', 'endpoint_missing'),
        ],
      }),
    );
    render(<ProvidedModelsSettings />);
    fireEvent.click(await screen.findByRole('button', { name: 'Add a provided connection' }));
    fillNew();
    fireEvent.click(screen.getByRole('button', { name: 'Verify and save' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Not saved: a model could not be verified for any agent.',
    );
    const chips = screen.getByRole('list', { name: 'Models to save' });
    expect(chips).toHaveTextContent('Not usable: The provider refused the API key (401/403).');
    expect(chips).toHaveTextContent(/Not usable: The provider has no such endpoint or model/);
    // Every input is kept for another try, the typed model ID included.
    expect(screen.getByLabelText('Name')).toHaveValue('House models');
    expect(screen.getByLabelText('API key')).toHaveValue('sk-deployment-key');
    expect(screen.queryByRole('article')).not.toBeInTheDocument();
    // Editing anything clears results that described the form as it was.
    type('Name', 'House models 2');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(chips).not.toHaveTextContent('Not usable');
  });

  it('marks the fields the server named, and says a taken name or a timeout plainly', async () => {
    vi.mocked(api.createOperatorModelConnection)
      .mockRejectedValueOnce(
        refusal('invalid_model_connection', 'Check the fields.', 422, { fields: ['base_url'] }),
      )
      .mockRejectedValueOnce(refusal('operator_model_connection_name_taken', 'Taken.', 409))
      .mockRejectedValueOnce(refusal('TIMEOUT_ERROR', 'HTTP 504', 504))
      .mockRejectedValueOnce(refusal('model_connection_check_limit', 'Slow down.', 429));
    render(<ProvidedModelsSettings />);
    fireEvent.click(await screen.findByRole('button', { name: 'Add a provided connection' }));
    fillNew();
    const submit = () => fireEvent.click(screen.getByRole('button', { name: 'Verify and save' }));

    submit();
    expect(await screen.findByRole('alert')).toHaveTextContent('Check the highlighted fields');
    expect(screen.getByLabelText('Base URL')).toBeInvalid();
    submit();
    expect(
      await screen.findByText('Another provided connection already has this name.'),
    ).toBeInTheDocument();
    submit();
    expect(
      await screen.findByText('Verification took too long. Nothing was saved; try again later.'),
    ).toBeInTheDocument();
    submit();
    expect(await screen.findByText(/Too many verifications in the last hour/)).toBeInTheDocument();
    expect(api.createOperatorModelConnection).toHaveBeenCalledTimes(4);
  });
});

describe('editing a provided connection', () => {
  const saved = providedConnection({
    default_model: 'house-fast',
    end_user_header: 'X-End-User',
    limits: BOTH_SET,
  });
  beforeEach(() => {
    vi.mocked(api.listOperatorModelConnections).mockResolvedValue(providedList([saved]));
  });

  async function openEditor() {
    render(<ProvidedModelsSettings />);
    fireEvent.click(within(await card()).getByRole('button', { name: 'Edit' }));
    return screen.getByRole('form', { name: 'Edit provided connection' });
  }

  it('starts from the saved values, never the key, and sends only what changed', async () => {
    vi.mocked(api.updateOperatorModelConnection).mockResolvedValue({ ...saved, name: 'Renamed' });
    const form = await openEditor();
    expect(within(form).getByLabelText('Name')).toHaveValue('House models');
    expect(within(form).getByLabelText('API key')).toHaveValue('');
    expect(within(form).getByLabelText('API key')).toHaveAttribute(
      'placeholder',
      'Leave blank to keep the saved key',
    );
    // A rename calls no provider: it saves at once.
    type('Name', 'Renamed');
    fireEvent.click(within(form).getByRole('button', { name: 'Save' }));
    await waitFor(() =>
      expect(api.updateOperatorModelConnection).toHaveBeenCalledExactlyOnceWith(PROVIDED_ID, {
        name: 'Renamed',
      }),
    );
    expect(await card('Renamed')).toBeInTheDocument();
    expect(screen.queryByRole('form')).not.toBeInTheDocument();
  });

  it('verifies again for a new key, and clears the default model and header with empty strings', async () => {
    vi.mocked(api.updateOperatorModelConnection).mockResolvedValue({
      ...saved,
      default_model: null,
      end_user_header: null,
      is_default: true,
    });
    const form = await openEditor();
    type('API key', 'sk-rotated');
    chooseOption(within(form).getByLabelText('Default model'), 'None');
    type(/^End-user header/, '');
    fireEvent.click(within(form).getByRole('checkbox', { name: /Default for new users/ }));
    fireEvent.click(within(form).getByRole('button', { name: 'Verify and save' }));
    await waitFor(() =>
      expect(api.updateOperatorModelConnection).toHaveBeenCalledExactlyOnceWith(PROVIDED_ID, {
        api_key: 'sk-rotated',
        default_model: '',
        is_default: true,
        end_user_header: '',
      }),
    );
    expect(await within(await card()).findByText('Default for new users')).toBeInTheDocument();
  });

  it('drops a default model that was removed from the list', async () => {
    vi.mocked(api.updateOperatorModelConnection).mockResolvedValue({
      ...saved,
      models: ['house-deep'],
      default_model: null,
    });
    const form = await openEditor();
    fireEvent.click(within(form).getByRole('button', { name: 'Remove house-fast' }));
    fireEvent.click(within(form).getByRole('button', { name: 'Save' }));
    await waitFor(() =>
      expect(api.updateOperatorModelConnection).toHaveBeenCalledExactlyOnceWith(PROVIDED_ID, {
        models: ['house-deep'],
        default_model: '',
      }),
    );
  });

  it('sends nothing for an editor closed unchanged, and holds the cards while it is open', async () => {
    const form = await openEditor();
    const house = await card();
    for (const name of ['Edit', 'Enable', 'Delete']) {
      expect(within(house).getByRole('button', { name })).toBeDisabled();
    }
    fireEvent.click(within(form).getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(screen.queryByRole('form')).not.toBeInTheDocument());
    expect(api.updateOperatorModelConnection).not.toHaveBeenCalled();
    expect(within(house).getByRole('button', { name: 'Edit' })).toBeEnabled();
  });

  it('moves the default mark when another connection becomes the default', async () => {
    const other = providedConnection({
      id: 'oconn_ffffffffffffffffffffffffffffffff',
      name: 'Spare',
      is_default: true,
    });
    vi.mocked(api.listOperatorModelConnections).mockResolvedValue(providedList([saved, other]));
    vi.mocked(api.updateOperatorModelConnection).mockResolvedValue({ ...saved, is_default: true });
    const form = await openEditor();
    fireEvent.click(within(form).getByRole('checkbox', { name: /Default for new users/ }));
    fireEvent.click(within(form).getByRole('button', { name: 'Save' }));
    expect(await within(await card()).findByText('Default for new users')).toBeInTheDocument();
    expect(within(await card('Spare')).queryByText('Default for new users')).toBeNull();
  });
});

describe('enabling, disabling and deleting', () => {
  it('cannot be enabled until every model has a default allowance, and says which lack one', async () => {
    vi.mocked(api.listOperatorModelConnections).mockResolvedValue(
      providedList([providedConnection({ limits: { 'house-fast': daily(1500) } })]),
    );
    render(<ProvidedModelsSettings />);
    const house = await card();
    const enable = within(house).getByRole('button', { name: 'Enable' });
    expect(enable).toBeDisabled();
    expect(enable).toHaveAccessibleDescription(
      'It cannot be enabled until every model has a default allowance. Still without one: house-deep.',
    );
    fireEvent.click(enable);
    expect(api.updateOperatorModelConnection).not.toHaveBeenCalled();
  });

  it('is enabled at once when every model has one', async () => {
    const ready = providedConnection({ limits: BOTH_SET });
    vi.mocked(api.listOperatorModelConnections).mockResolvedValue(providedList([ready]));
    const change = pending<api.OperatorModelConnection>();
    vi.mocked(api.updateOperatorModelConnection).mockReturnValue(change.promise);
    render(<ProvidedModelsSettings />);
    const house = await card();
    expect(within(house).queryByText(/cannot be enabled/)).not.toBeInTheDocument();
    fireEvent.click(within(house).getByRole('button', { name: 'Enable' }));
    expect(await within(house).findByText('Working…')).toBeInTheDocument();
    expect(within(house).getByRole('button', { name: 'Enable' })).toBeDisabled();
    expect(api.updateOperatorModelConnection).toHaveBeenCalledExactlyOnceWith(PROVIDED_ID, {
      enabled: true,
    });
    change.resolve({ ...ready, enabled: true });
    expect(await within(house).findByText('Enabled')).toBeInTheDocument();
    expect(within(house).getByRole('button', { name: 'Disable' })).toBeEnabled();
    expect(within(house).queryByText('Working…')).not.toBeInTheDocument();
  });

  it('says what the server said when it still refuses to enable', async () => {
    vi.mocked(api.listOperatorModelConnections).mockResolvedValue(
      providedList([providedConnection({ limits: BOTH_SET })]),
    );
    vi.mocked(api.updateOperatorModelConnection).mockRejectedValue(
      refusal('operator_model_limits_missing', 'Limits are missing.', 409),
    );
    render(<ProvidedModelsSettings />);
    const house = await card();
    fireEvent.click(within(house).getByRole('button', { name: 'Enable' }));
    expect(await within(house).findByRole('alert')).toHaveTextContent(
      'Not enabled: every model needs a default allowance first.',
    );
    expect(within(house).getByText('Disabled')).toBeInTheDocument();
  });

  it('asks before disabling, because it stops work for every account', async () => {
    const on = providedConnection({ enabled: true, limits: BOTH_SET });
    vi.mocked(api.listOperatorModelConnections).mockResolvedValue(providedList([on]));
    vi.mocked(api.updateOperatorModelConnection).mockResolvedValue({ ...on, enabled: false });
    render(<ProvidedModelsSettings />);
    const house = await card();
    fireEvent.click(within(house).getByRole('button', { name: 'Disable' }));
    const confirm = within(house).getByRole('group', { name: 'Confirm' });
    expect(confirm).toHaveTextContent(
      'Disable House models? Agent processes running on it stop for every account',
    );
    expect(api.updateOperatorModelConnection).not.toHaveBeenCalled();
    fireEvent.click(within(confirm).getByRole('button', { name: 'Cancel' }));
    expect(within(house).queryByRole('group', { name: 'Confirm' })).not.toBeInTheDocument();

    fireEvent.click(within(house).getByRole('button', { name: 'Disable' }));
    fireEvent.click(
      within(within(house).getByRole('group', { name: 'Confirm' })).getByRole('button', {
        name: 'Confirm',
      }),
    );
    expect(await within(house).findByText('Disabled')).toBeInTheDocument();
    expect(api.updateOperatorModelConnection).toHaveBeenCalledExactlyOnceWith(PROVIDED_ID, {
      enabled: false,
    });
    expect(within(house).queryByRole('group', { name: 'Confirm' })).not.toBeInTheDocument();
  });

  it('deletes only after confirmation', async () => {
    vi.mocked(api.deleteOperatorModelConnection).mockResolvedValue();
    render(<ProvidedModelsSettings />);
    const house = await card();
    fireEvent.click(within(house).getByRole('button', { name: 'Delete' }));
    const confirm = within(house).getByRole('group', { name: 'Confirm' });
    expect(confirm).toHaveTextContent('Delete House models?');
    expect(confirm).toHaveTextContent('This does not revoke the key at the provider.');
    expect(api.deleteOperatorModelConnection).not.toHaveBeenCalled();
    fireEvent.click(within(confirm).getByRole('button', { name: 'Confirm' }));
    await waitFor(() => expect(screen.queryByRole('article')).not.toBeInTheDocument());
    expect(api.deleteOperatorModelConnection).toHaveBeenCalledExactlyOnceWith(PROVIDED_ID);
    expect(screen.getByText('No provided connections yet.')).toBeInTheDocument();
  });

  it('keeps the connection, and says why, when the delete is refused', async () => {
    vi.mocked(api.deleteOperatorModelConnection).mockRejectedValue(
      refusal('operator_model_connection_busy', 'Turns are still running on it.', 409),
    );
    render(<ProvidedModelsSettings />);
    const house = await card();
    fireEvent.click(within(house).getByRole('button', { name: 'Delete' }));
    fireEvent.click(
      within(within(house).getByRole('group', { name: 'Confirm' })).getByRole('button', {
        name: 'Confirm',
      }),
    );
    expect(await within(house).findByRole('alert')).toHaveTextContent(
      'Turns are still running on it.',
    );
    expect(screen.getByRole('article', { name: 'House models' })).toBeInTheDocument();
  });
});
