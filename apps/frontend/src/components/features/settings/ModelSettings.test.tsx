// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { LocaleProvider } from '@/components/providers/LocaleProvider';
import { LOCALE_STORAGE_KEY } from '@/lib/i18n/locales';
import { getModelPreference, saveModelPreference } from '@/lib/api/modelPreference';
import type { ModelPreference } from '@/lib/api/modelPreference';
import { ModelSettings } from './ModelSettings';

vi.mock('@/lib/api/modelPreference', () => ({
  getModelPreference: vi.fn(),
  saveModelPreference: vi.fn(),
}));

const MODELS = ['glm-5.1', 'qwen-next'];

function preference(overrides: Partial<ModelPreference> = {}): ModelPreference {
  return {
    default_model: null,
    last_model: 'qwen-next',
    effective_model: 'qwen-next',
    deployment_default: null,
    models: MODELS,
    ...overrides,
  };
}

function renderSettings() {
  return render(
    <LocaleProvider>
      <ModelSettings />
    </LocaleProvider>,
  );
}

beforeEach(() => {
  localStorage.clear();
  localStorage.setItem(LOCALE_STORAGE_KEY, 'en');
  vi.mocked(getModelPreference).mockReset();
  vi.mocked(saveModelPreference).mockReset();
  vi.mocked(getModelPreference).mockResolvedValue(preference());
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('default model settings', () => {
  it('follows the last used model until the owner pins one', async () => {
    renderSettings();

    const select = await screen.findByLabelText('Default model');
    expect(select).toHaveValue('__follow_last__');
    expect(screen.getByRole('option', { name: 'Follow my last model (qwen-next)' })).toBeVisible();
    // The card says what is actually applied, not only what is saved.
    expect(screen.getByText('New tasks start on qwen-next.')).toBeInTheDocument();

    vi.mocked(saveModelPreference).mockResolvedValue(
      preference({ default_model: 'glm-5.1', effective_model: 'glm-5.1' }),
    );
    fireEvent.change(select, { target: { value: 'glm-5.1' } });

    await waitFor(() => expect(saveModelPreference).toHaveBeenCalledWith('glm-5.1'));
    await waitFor(() => expect(select).toHaveValue('glm-5.1'));
    expect(screen.getByText('New tasks start on glm-5.1.')).toBeInTheDocument();
  });

  it('clears the pin by choosing to follow the last used model again', async () => {
    vi.mocked(getModelPreference).mockResolvedValue(
      preference({ default_model: 'glm-5.1', effective_model: 'glm-5.1' }),
    );
    vi.mocked(saveModelPreference).mockResolvedValue(preference());
    renderSettings();

    const select = await screen.findByLabelText('Default model');
    expect(select).toHaveValue('glm-5.1');

    fireEvent.change(select, { target: { value: '__follow_last__' } });

    // null, not the empty string: clearing the pin is a real choice.
    await waitFor(() => expect(saveModelPreference).toHaveBeenCalledWith(null));
    await waitFor(() => expect(select).toHaveValue('__follow_last__'));
    expect(screen.getByText('New tasks start on qwen-next.')).toBeInTheDocument();
  });

  it('names the deployment default for an owner who has no history yet', async () => {
    // Without this, a brand-new user reading "follow my last model" has no way
    // to learn what their first task will actually use.
    vi.mocked(getModelPreference).mockResolvedValue(
      preference({ last_model: null, effective_model: 'glm-5.1', deployment_default: 'glm-5.1' }),
    );
    renderSettings();

    const select = await screen.findByLabelText('Default model');
    expect(select).toHaveValue('__follow_last__');
    expect(
      screen.getByRole('option', {
        name: 'Follow my last model (glm-5.1 until you have one)',
      }),
    ).toBeVisible();
    expect(screen.getByText('New tasks start on glm-5.1.')).toBeInTheDocument();
  });

  it('says "follow my last model" plainly when the deployment set no default', async () => {
    vi.mocked(getModelPreference).mockResolvedValue(
      preference({ last_model: null, effective_model: null, deployment_default: null }),
    );
    renderSettings();

    expect(await screen.findByRole('option', { name: 'Follow my last model' })).toBeVisible();
  });

  it('shows a retired pin as saved but explains what actually applies', async () => {
    vi.mocked(getModelPreference).mockResolvedValue(
      preference({
        default_model: 'retired-model',
        last_model: 'qwen-next',
        effective_model: 'qwen-next',
      }),
    );
    renderSettings();

    const select = await screen.findByLabelText('Default model');
    // The saved choice is shown, not silently swapped for "follow my last
    // model" — otherwise the control would misreport what is stored.
    expect(select).toHaveValue('retired-model');
    expect(
      screen.getByRole('option', { name: 'retired-model (no longer available)' }),
    ).toBeVisible();
    expect(screen.getByText('New tasks start on qwen-next.')).toBeInTheDocument();
  });

  it('reports a failed save without pretending the choice changed', async () => {
    renderSettings();

    const select = await screen.findByLabelText('Default model');
    vi.mocked(saveModelPreference).mockRejectedValue(new Error('refused'));
    fireEvent.change(select, { target: { value: 'glm-5.1' } });

    expect(
      await screen.findByText('Could not save. Your previous choice is unchanged.'),
    ).toBeInTheDocument();
    expect(select).toHaveValue('__follow_last__');
  });

  it('offers no model when the catalog is empty and says so', async () => {
    vi.mocked(getModelPreference).mockResolvedValue(
      preference({ last_model: null, effective_model: null, models: [] }),
    );
    renderSettings();

    expect(await screen.findByLabelText('Default model')).toHaveValue('__follow_last__');
    expect(screen.getByRole('option', { name: 'Follow my last model' })).toBeVisible();
    // No fallback to hide behind: with no model on offer, nothing starts.
    expect(
      screen.getByText('No model is available yet. A new task waits until one is offered.'),
    ).toBeInTheDocument();
  });

  it('reads the catalog from the preference alone, not the composer config', async () => {
    renderSettings();

    await screen.findByLabelText('Default model');
    expect(getModelPreference).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('option', { name: 'glm-5.1' })).toBeInTheDocument();
  });

  it('names where "follow" lands when the last model is retired', async () => {
    vi.mocked(getModelPreference).mockResolvedValue(
      preference({
        last_model: 'retired-model',
        effective_model: 'glm-5.1',
        deployment_default: 'glm-5.1',
      }),
    );
    renderSettings();

    // Not "Follow my last model (retired-model)" above "New tasks start on
    // glm-5.1": the option says the same thing as the sentence under it.
    expect(
      await screen.findByRole('option', {
        name: 'Follow my last model (glm-5.1, since retired-model is no longer available)',
      }),
    ).toBeVisible();
    expect(screen.getByText('New tasks start on glm-5.1.')).toBeInTheDocument();
  });

  it('asks for a choice when the last model is retired and nothing else applies', async () => {
    vi.mocked(getModelPreference).mockResolvedValue(
      preference({ last_model: 'retired-model', effective_model: null }),
    );
    renderSettings();

    expect(
      await screen.findByRole('option', {
        name: 'Follow my last model (retired-model is no longer available)',
      }),
    ).toBeVisible();
    // The catalog was read, so this is not an outage: the owner just has to choose.
    expect(
      screen.getByText(
        'Nothing is chosen for new tasks yet. Pick a model here or in the composer.',
      ),
    ).toBeInTheDocument();
  });

  it('does not call a pin retired when the catalog could not be read', async () => {
    vi.mocked(getModelPreference).mockResolvedValue(
      preference({ default_model: 'glm-5.1', effective_model: null, models: [] }),
    );
    renderSettings();

    const select = await screen.findByLabelText('Default model');
    expect(select).toHaveValue('glm-5.1');
    expect(screen.getByRole('option', { name: 'glm-5.1' })).toBeInTheDocument();
    expect(screen.queryByText(/no longer available/)).not.toBeInTheDocument();
    expect(
      screen.getByText('No model is available yet. A new task waits until one is offered.'),
    ).toBeInTheDocument();
  });

  it('shows one retryable error when either read fails', async () => {
    vi.mocked(getModelPreference).mockRejectedValue(new Error('offline'));
    renderSettings();

    expect(await screen.findByText('Could not load your default model.')).toBeInTheDocument();
    vi.mocked(getModelPreference).mockResolvedValue(preference());
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));

    expect(await screen.findByLabelText('Default model')).toHaveValue('__follow_last__');
    expect(getModelPreference).toHaveBeenCalledTimes(2);
  });
});
