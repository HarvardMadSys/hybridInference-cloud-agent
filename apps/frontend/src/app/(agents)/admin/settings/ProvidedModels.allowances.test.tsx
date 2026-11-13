// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { chooseOption, optionNames } from '@/components/ui/__tests__/select';
import * as api from '@/lib/api/adminOperatorModels';

import ProvidedModelsSettings from './ProvidedModels';
import { utcDay } from './providedModelsForm';
import {
  daily,
  PROVIDED_ID,
  providedConnection,
  providedList,
  refusal,
} from './providedModelsTestFixtures';

// Allowances, the accounts that differ from them, and the requests counted.

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

const ADA: api.OperatorModelUser = {
  account_id: 'acct_ada',
  email: 'ada@example.com',
  limits: { 'house-fast': 5000, 'house-deep': null },
  used_today: { 'house-fast': 1200 },
};
const BOB: api.OperatorModelUser = {
  account_id: 'acct_bob',
  email: '',
  limits: {},
  used_today: { 'house-deep': 3 },
};

async function card() {
  return screen.findByRole('article', { name: 'House models' });
}

/** One model's row in the default allowances list. */
function limitRow(house: HTMLElement, model: string) {
  const list = within(house).getByRole('region', { name: 'Default allowances' });
  return within(list).getByRole('listitem', { name: model });
}

async function openAccounts() {
  const house = await card();
  fireEvent.click(
    within(house).getByRole('button', { name: 'Show accounts and their allowances' }),
  );
  return within(house).findByRole('region', { name: 'Accounts' });
}

async function openUsage() {
  const house = await card();
  fireEvent.click(within(house).getByRole('button', { name: 'Show requests by day' }));
  return within(house).findByRole('region', { name: 'Requests' });
}

function rowTexts(table: HTMLElement): string[][] {
  return within(table)
    .getAllByRole('row')
    .slice(1)
    .map((row) =>
      within(row)
        .getAllByRole('cell')
        .map((cell) => cell.textContent ?? ''),
    );
}

beforeEach(() => {
  vi.resetAllMocks();
  localStorage.clear();
  vi.mocked(api.listOperatorModelConnections).mockResolvedValue(providedList());
  vi.mocked(api.listOperatorModelUsers).mockResolvedValue({ users: [], next_cursor: null });
});
afterEach(cleanup);

describe('default allowances', () => {
  it('shows what is saved for each model, and which still have nothing', async () => {
    vi.mocked(api.listOperatorModelConnections).mockResolvedValue(
      providedList([
        providedConnection({
          models: ['house-fast', 'house-open', 'house-named', 'house-new'],
          limits: {
            'house-fast': { user_daily_requests: 1500, global_concurrency: 8 },
            'house-open': daily(null),
            'house-named': daily(0),
          },
        }),
      ]),
    );
    render(<ProvidedModelsSettings />);
    const house = await card();
    expect(limitRow(house, 'house-fast')).toHaveTextContent(
      '1,500 requests per account per day. At most 8 in flight across accounts.',
    );
    expect(limitRow(house, 'house-open')).toHaveTextContent(
      'Unlimited requests per account per day.',
    );
    expect(limitRow(house, 'house-named')).toHaveTextContent(
      'Offered only to accounts given their own allowance.',
    );
    expect(limitRow(house, 'house-new')).toHaveTextContent('No default allowance yet.');
    // An unset model has nothing to save until an allowance is chosen.
    const unset = limitRow(house, 'house-new');
    expect(within(unset).getByLabelText('Daily allowance')).toHaveTextContent('Choose…');
    expect(
      within(unset).getByRole('button', { name: 'Save the allowance for house-new' }),
    ).toBeDisabled();
    expect(optionNames(within(unset).getByLabelText('Daily allowance'))).toEqual([
      'A number of requests',
      'Unlimited',
      'Only named accounts (0)',
    ]);
  });

  it('saves a number of requests a day, with an optional deployment-wide cap', async () => {
    const saved = providedConnection({
      limits: { 'house-fast': { user_daily_requests: 1500, global_concurrency: 8 } },
    });
    vi.mocked(api.setOperatorModelLimit).mockResolvedValue(saved);
    render(<ProvidedModelsSettings />);
    const house = await card();
    const row = limitRow(house, 'house-fast');
    chooseOption(within(row).getByLabelText('Daily allowance'), 'A number of requests');
    fireEvent.change(within(row).getByLabelText('Requests per day'), {
      target: { value: '1500' },
    });
    fireEvent.change(within(row).getByLabelText(/^Requests in flight across accounts/), {
      target: { value: '8' },
    });
    fireEvent.click(within(row).getByRole('button', { name: 'Save the allowance for house-fast' }));
    await waitFor(() =>
      expect(api.setOperatorModelLimit).toHaveBeenCalledExactlyOnceWith(PROVIDED_ID, {
        model: 'house-fast',
        user_daily_requests: 1500,
        global_concurrency: 8,
      }),
    );
    expect(await within(house).findByText(/1,500 requests per account per day/)).toBeVisible();
    // Saved and unchanged: nothing to save again. The other model still blocks enabling.
    expect(
      within(limitRow(house, 'house-fast')).getByRole('button', {
        name: 'Save the allowance for house-fast',
      }),
    ).toBeDisabled();
    expect(within(house).getByRole('button', { name: 'Enable' })).toHaveAccessibleDescription(
      /Still without one: house-deep\./,
    );
  });

  it('saves unlimited as null and "only named accounts" as zero, then lets the connection be enabled', async () => {
    vi.mocked(api.setOperatorModelLimit)
      .mockResolvedValueOnce(providedConnection({ limits: { 'house-fast': daily(null) } }))
      .mockResolvedValueOnce(
        providedConnection({ limits: { 'house-fast': daily(null), 'house-deep': daily(0) } }),
      );
    render(<ProvidedModelsSettings />);
    const house = await card();
    expect(within(house).getByRole('button', { name: 'Enable' })).toBeDisabled();

    chooseOption(
      within(limitRow(house, 'house-fast')).getByLabelText('Daily allowance'),
      'Unlimited',
    );
    expect(within(limitRow(house, 'house-fast')).queryByLabelText('Requests per day')).toBeNull();
    fireEvent.click(
      within(house).getByRole('button', { name: 'Save the allowance for house-fast' }),
    );
    await within(house).findByText('Unlimited requests per account per day.');

    chooseOption(
      within(limitRow(house, 'house-deep')).getByLabelText('Daily allowance'),
      'Only named accounts (0)',
    );
    fireEvent.click(
      within(house).getByRole('button', { name: 'Save the allowance for house-deep' }),
    );
    await within(house).findByText('Offered only to accounts given their own allowance.');

    expect(vi.mocked(api.setOperatorModelLimit).mock.calls).toEqual([
      [PROVIDED_ID, { model: 'house-fast', user_daily_requests: null, global_concurrency: null }],
      [PROVIDED_ID, { model: 'house-deep', user_daily_requests: 0, global_concurrency: null }],
    ]);
    // Every model now has a default: the gate is gone.
    expect(within(house).getByRole('button', { name: 'Enable' })).toBeEnabled();
    expect(within(house).queryByText(/cannot be enabled/)).not.toBeInTheDocument();
  });

  it('will not send a number that is not one', async () => {
    render(<ProvidedModelsSettings />);
    const row = limitRow(await card(), 'house-fast');
    chooseOption(within(row).getByLabelText('Daily allowance'), 'A number of requests');
    const save = within(row).getByRole('button', { name: 'Save the allowance for house-fast' });
    for (const value of ['', '0', '1.5', 'lots', '10000001']) {
      fireEvent.change(within(row).getByLabelText('Requests per day'), { target: { value } });
      expect(save).toBeDisabled();
    }
    expect(row).toHaveTextContent('Enter whole numbers: 1 to 10,000,000 requests per day');
    fireEvent.change(within(row).getByLabelText('Requests per day'), { target: { value: '10' } });
    expect(save).toBeEnabled();
    fireEvent.change(within(row).getByLabelText(/^Requests in flight across accounts/), {
      target: { value: '2000' },
    });
    expect(save).toBeDisabled();
    expect(api.setOperatorModelLimit).not.toHaveBeenCalled();
  });

  it('shows a pending state while saving, and what the server said when it refuses', async () => {
    let refuse!: (cause: unknown) => void;
    vi.mocked(api.setOperatorModelLimit).mockReturnValue(
      new Promise((_resolve, reject) => {
        refuse = reject;
      }),
    );
    render(<ProvidedModelsSettings />);
    const row = limitRow(await card(), 'house-fast');
    chooseOption(within(row).getByLabelText('Daily allowance'), 'Unlimited');
    fireEvent.click(within(row).getByRole('button', { name: 'Save the allowance for house-fast' }));
    const save = within(row).getByRole('button', { name: 'Save the allowance for house-fast' });
    expect(save).toHaveTextContent('Working…');
    expect(save).toHaveAttribute('aria-busy', 'true');
    refuse(refusal('operator_model_connection_not_found', 'Gone.', 404));
    expect(await within(row).findByRole('alert')).toHaveTextContent(
      'This connection no longer exists.',
    );
    expect(save).toHaveTextContent('Save');
  });
});

describe('accounts with their own allowance', () => {
  it('asks for nothing until it is opened', async () => {
    render(<ProvidedModelsSettings />);
    await card();
    expect(api.listOperatorModelUsers).not.toHaveBeenCalled();
    expect(api.getOperatorModelUsage).not.toHaveBeenCalled();
  });

  it('lists each account and model with its own allowance and today’s use', async () => {
    vi.mocked(api.listOperatorModelUsers).mockResolvedValue({
      users: [ADA, BOB],
      next_cursor: null,
    });
    render(<ProvidedModelsSettings />);
    const accounts = await openAccounts();
    const table = await within(accounts).findByRole('table', { name: 'Accounts' });
    expect(api.listOperatorModelUsers).toHaveBeenCalledExactlyOnceWith(PROVIDED_ID, {
      cursor: null,
    });
    expect(
      within(table)
        .getAllByRole('columnheader')
        .map((header) => header.textContent),
    ).toEqual(['Account', 'Model', 'Own daily allowance', 'Used today', 'Actions']);
    expect(rowTexts(table)).toEqual([
      ['ada@example.comacct_ada', 'house-deep', 'Unlimited', '0', 'Clear'],
      ['ada@example.comacct_ada', 'house-fast', '5,000', '1,200', 'Clear'],
      // Use today on the default allowance: nothing of its own to clear.
      ['acct_bob', 'house-deep', 'Follows the default', '3', ''],
    ]);
  });

  it('says so when no account differs from the defaults', async () => {
    render(<ProvidedModelsSettings />);
    const accounts = await openAccounts();
    expect(
      await within(accounts).findByText(
        'No account has its own allowance or has used a model today.',
      ),
    ).toBeInTheDocument();
  });

  it('loads further pages by cursor, keeping the ones already shown', async () => {
    vi.mocked(api.listOperatorModelUsers)
      .mockResolvedValueOnce({ users: [ADA], next_cursor: 'acct_ada' })
      .mockResolvedValueOnce({ users: [BOB], next_cursor: null });
    render(<ProvidedModelsSettings />);
    const accounts = await openAccounts();
    fireEvent.click(await within(accounts).findByRole('button', { name: 'Load more accounts' }));
    await within(accounts).findByText('acct_bob');
    expect(api.listOperatorModelUsers).toHaveBeenLastCalledWith(PROVIDED_ID, {
      cursor: 'acct_ada',
    });
    expect(within(accounts).getAllByText('ada@example.com')).toHaveLength(2);
    expect(within(accounts).queryByRole('button', { name: 'Load more accounts' })).toBeNull();
  });

  it('sets an allowance for an account named by email, per model', async () => {
    vi.mocked(api.setOperatorModelUserLimit).mockResolvedValue({
      account_id: 'acct_cy',
      email: 'cy@example.com',
      limits: { 'house-deep': 250 },
      used_today: {},
    });
    render(<ProvidedModelsSettings />);
    const accounts = await openAccounts();
    const form = within(accounts).getByRole('form', { name: 'Set an allowance for an account' });
    const submit = within(form).getByRole('button', { name: 'Set allowance' });
    expect(submit).toBeDisabled();
    fireEvent.change(within(form).getByLabelText('Account'), {
      target: { value: ' cy@example.com ' },
    });
    expect(optionNames(within(form).getByLabelText('Model'))).toEqual(['house-fast', 'house-deep']);
    chooseOption(within(form).getByLabelText('Model'), 'house-deep');
    fireEvent.change(within(form).getByLabelText('Requests per day'), { target: { value: '250' } });
    fireEvent.click(submit);
    await waitFor(() =>
      expect(api.setOperatorModelUserLimit).toHaveBeenCalledExactlyOnceWith(PROVIDED_ID, {
        account: 'cy@example.com',
        model: 'house-deep',
        daily_requests: 250,
      }),
    );
    const table = await within(accounts).findByRole('table', { name: 'Accounts' });
    expect(rowTexts(table)).toEqual([['cy@example.comacct_cy', 'house-deep', '250', '0', 'Clear']]);
    expect(within(form).getByLabelText('Account')).toHaveValue('');
  });

  it('can make one account unlimited, or withhold a model from it', async () => {
    vi.mocked(api.listOperatorModelUsers).mockResolvedValue({ users: [ADA], next_cursor: null });
    vi.mocked(api.setOperatorModelUserLimit)
      .mockResolvedValueOnce({ ...ADA, limits: { ...ADA.limits, 'house-fast': null } })
      .mockResolvedValueOnce({ ...ADA, limits: { 'house-fast': null, 'house-deep': 0 } });
    render(<ProvidedModelsSettings />);
    const accounts = await openAccounts();
    const form = within(accounts).getByRole('form', { name: 'Set an allowance for an account' });
    const set = (model: string, allowance: string) => {
      fireEvent.change(within(form).getByLabelText('Account'), { target: { value: 'acct_ada' } });
      chooseOption(within(form).getByLabelText('Model'), model);
      chooseOption(within(form).getByLabelText('Daily allowance'), allowance);
      fireEvent.click(within(form).getByRole('button', { name: 'Set allowance' }));
    };

    set('house-fast', 'Unlimited');
    await waitFor(() => expect(api.setOperatorModelUserLimit).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(within(form).getByLabelText('Account')).toHaveValue(''));
    set('house-deep', 'Withhold the model (0)');
    await waitFor(() => expect(api.setOperatorModelUserLimit).toHaveBeenCalledTimes(2));

    expect(vi.mocked(api.setOperatorModelUserLimit).mock.calls.map((call) => call[1])).toEqual([
      { account: 'acct_ada', model: 'house-fast', daily_requests: null },
      { account: 'acct_ada', model: 'house-deep', daily_requests: 0 },
    ]);
    const table = within(accounts).getByRole('table', { name: 'Accounts' });
    // The account's one row is replaced, never listed twice.
    await waitFor(() =>
      expect(rowTexts(table)).toEqual([
        ['ada@example.comacct_ada', 'house-deep', 'Withheld (0)', '0', 'Clear'],
        ['ada@example.comacct_ada', 'house-fast', 'Unlimited', '1,200', 'Clear'],
      ]),
    );
  });

  it('says when no account has that email or id, and keeps what was typed', async () => {
    vi.mocked(api.setOperatorModelUserLimit).mockRejectedValue(
      refusal('account_not_found', 'No such account.', 404),
    );
    render(<ProvidedModelsSettings />);
    const accounts = await openAccounts();
    const form = within(accounts).getByRole('form', { name: 'Set an allowance for an account' });
    fireEvent.change(within(form).getByLabelText('Account'), { target: { value: 'nobody@x.io' } });
    fireEvent.change(within(form).getByLabelText('Requests per day'), { target: { value: '5' } });
    fireEvent.click(within(form).getByRole('button', { name: 'Set allowance' }));
    expect(await within(form).findByRole('alert')).toHaveTextContent(
      'No account has that email address or id.',
    );
    expect(within(form).getByLabelText('Account')).toHaveValue('nobody@x.io');
  });

  it('clears an account’s own allowance, putting it back on the default', async () => {
    vi.mocked(api.listOperatorModelUsers).mockResolvedValue({
      users: [ADA, { ...BOB, limits: { 'house-fast': 10 }, used_today: {} }],
      next_cursor: null,
    });
    vi.mocked(api.clearOperatorModelUserLimit).mockResolvedValue();
    render(<ProvidedModelsSettings />);
    const accounts = await openAccounts();
    const table = await within(accounts).findByRole('table', { name: 'Accounts' });

    fireEvent.click(
      within(table).getByRole('button', {
        name: 'Put ada@example.com back on the default for house-fast',
      }),
    );
    await waitFor(() =>
      expect(api.clearOperatorModelUserLimit).toHaveBeenCalledWith(
        PROVIDED_ID,
        'acct_ada',
        'house-fast',
      ),
    );
    // Ada used the model today, so her line stays, now on the default.
    await waitFor(() =>
      expect(rowTexts(table)).toContainEqual([
        'ada@example.comacct_ada',
        'house-fast',
        'Follows the default',
        '1,200',
        '',
      ]),
    );

    // Bob had only that allowance and no use today: nothing is left to list.
    fireEvent.click(
      within(table).getByRole('button', {
        name: 'Put acct_bob back on the default for house-fast',
      }),
    );
    await waitFor(() => expect(within(table).queryByText('acct_bob')).not.toBeInTheDocument());
    expect(api.clearOperatorModelUserLimit).toHaveBeenLastCalledWith(
      PROVIDED_ID,
      'acct_bob',
      'house-fast',
    );
  });

  it('drops a line whose allowance was already cleared elsewhere, without an error', async () => {
    vi.mocked(api.listOperatorModelUsers).mockResolvedValue({
      users: [{ ...BOB, limits: { 'house-fast': 10 }, used_today: {} }],
      next_cursor: null,
    });
    vi.mocked(api.clearOperatorModelUserLimit).mockRejectedValue(
      refusal('operator_model_user_limit_not_found', 'No allowance of its own.', 404),
    );
    render(<ProvidedModelsSettings />);
    const accounts = await openAccounts();
    fireEvent.click(
      await within(accounts).findByRole('button', {
        name: 'Put acct_bob back on the default for house-fast',
      }),
    );
    await waitFor(() => expect(within(accounts).queryByRole('table')).not.toBeInTheDocument());
    expect(within(accounts).queryByRole('alert')).not.toBeInTheDocument();
  });

  it('keeps the allowance, and says why, when clearing fails or the list cannot be read', async () => {
    vi.mocked(api.listOperatorModelUsers).mockResolvedValue({ users: [ADA], next_cursor: null });
    vi.mocked(api.clearOperatorModelUserLimit).mockRejectedValue(
      refusal('SERVER_ERROR', 'HTTP 500', 500),
    );
    render(<ProvidedModelsSettings />);
    const accounts = await openAccounts();
    const table = await within(accounts).findByRole('table', { name: 'Accounts' });
    fireEvent.click(
      within(table).getByRole('button', {
        name: 'Put ada@example.com back on the default for house-deep',
      }),
    );
    expect(await within(accounts).findByText(/Server error/)).toHaveAttribute('role', 'alert');
    expect(rowTexts(table)).toHaveLength(2);
  });
});

describe('requests counted for a connection', () => {
  it('says these are requests the relay counted, not tokens or cost', async () => {
    render(<ProvidedModelsSettings />);
    const usage = await openUsage();
    expect(usage).toHaveTextContent(
      'Requests this deployment’s relay accepted, per UTC day, account and model. These are request counts, not tokens and not cost',
    );
    // Nothing is read until a range is asked for.
    expect(api.getOperatorModelUsage).not.toHaveBeenCalled();
  });

  it('reads a date range and lists requests per day, account and model', async () => {
    vi.mocked(api.getOperatorModelUsage).mockResolvedValue({
      rows: [
        {
          day: '2026-11-12',
          account_id: 'acct_ada',
          email: 'ada@example.com',
          model: 'house-fast',
          requests: 1200,
        },
        { day: '2026-11-11', account_id: 'acct_bob', email: '', model: 'house-deep', requests: 3 },
      ],
      truncated: false,
    });
    render(<ProvidedModelsSettings />);
    const usage = await openUsage();
    // The last seven UTC days, to start with.
    expect(within(usage).getByLabelText('From (UTC day)')).toHaveValue(utcDay(6));
    expect(within(usage).getByLabelText('To (UTC day)')).toHaveValue(utcDay());
    fireEvent.change(within(usage).getByLabelText('From (UTC day)'), {
      target: { value: '2026-11-01' },
    });
    fireEvent.change(within(usage).getByLabelText('To (UTC day)'), {
      target: { value: '2026-11-12' },
    });
    fireEvent.click(within(usage).getByRole('button', { name: 'Show requests' }));
    const table = await within(usage).findByRole('table', { name: 'Requests' });
    expect(api.getOperatorModelUsage).toHaveBeenCalledExactlyOnceWith(PROVIDED_ID, {
      from: '2026-11-01',
      to: '2026-11-12',
    });
    expect(
      within(table)
        .getAllByRole('columnheader')
        .map((header) => header.textContent),
    ).toEqual(['Day (UTC)', 'Account', 'Model', 'Requests']);
    expect(rowTexts(table)).toEqual([
      ['2026-11-12', 'ada@example.com', 'house-fast', '1,200'],
      ['2026-11-11', 'acct_bob', 'house-deep', '3'],
    ]);
    expect(within(usage).queryByText(/More rows matched/)).not.toBeInTheDocument();
  });

  it('says when more rows matched than are shown, and when none did', async () => {
    vi.mocked(api.getOperatorModelUsage)
      .mockResolvedValueOnce({
        rows: [{ day: '2026-11-12', account_id: 'a', email: '', model: 'house-fast', requests: 1 }],
        truncated: true,
      })
      .mockResolvedValueOnce({ rows: [], truncated: false });
    render(<ProvidedModelsSettings />);
    const usage = await openUsage();
    fireEvent.click(within(usage).getByRole('button', { name: 'Show requests' }));
    expect(
      await within(usage).findByText(
        'More rows matched than are shown. Choose a shorter range to see the rest.',
      ),
    ).toBeInTheDocument();
    fireEvent.click(within(usage).getByRole('button', { name: 'Show requests' }));
    expect(
      await within(usage).findByText('No requests were counted in these days.'),
    ).toBeInTheDocument();
    expect(within(usage).queryByRole('table')).not.toBeInTheDocument();
  });

  it('will not ask for a range that ends before it starts', async () => {
    render(<ProvidedModelsSettings />);
    const usage = await openUsage();
    fireEvent.change(within(usage).getByLabelText('From (UTC day)'), {
      target: { value: '2026-11-12' },
    });
    fireEvent.change(within(usage).getByLabelText('To (UTC day)'), {
      target: { value: '2026-11-01' },
    });
    expect(within(usage).getByRole('button', { name: 'Show requests' })).toBeDisabled();
    expect(
      within(usage).getByText('Choose a first day that is not after the last day.'),
    ).toBeInTheDocument();
  });

  it('says what went wrong when the counts cannot be read', async () => {
    vi.mocked(api.getOperatorModelUsage).mockRejectedValue(
      refusal('invalid_range', 'The range may span at most 92 days.', 422),
    );
    render(<ProvidedModelsSettings />);
    const usage = await openUsage();
    fireEvent.click(within(usage).getByRole('button', { name: 'Show requests' }));
    expect(await within(usage).findByRole('alert')).toHaveTextContent(
      'The range may span at most 92 days.',
    );
  });
});
