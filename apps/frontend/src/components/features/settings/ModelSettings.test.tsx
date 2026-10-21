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

async function modelField(value: string) {
  const trigger = await screen.findByRole('button', { name: 'Default model' });
  await waitFor(() => expect(trigger).toHaveValue(value));
  return trigger;
}

async function pick(model: string) {
  fireEvent.click(screen.getByRole('button', { name: 'Default model' }));
  fireEvent.click(screen.getByRole('option', { name: model }));
}

describe('default model settings', () => {
  it('shows the effective model and offers only concrete models before pinning', async () => {
    renderSettings();
    const field = await modelField('qwen-next');
    expect(screen.getByText('New tasks start on qwen-next.')).toBeInTheDocument();
    fireEvent.click(field);
    expect(screen.getAllByRole('option').map((option) => option.textContent)).toEqual(MODELS);
    expect(screen.queryByText(/follow my last/i)).not.toBeInTheDocument();
    vi.mocked(saveModelPreference).mockResolvedValue(
      preference({ default_model: 'glm-5.1', effective_model: 'glm-5.1' }),
    );
    fireEvent.click(screen.getByRole('option', { name: 'glm-5.1' }));
    await waitFor(() => expect(saveModelPreference).toHaveBeenCalledWith('glm-5.1'));
    await modelField('glm-5.1');
    expect(screen.getByText('New tasks start on glm-5.1.')).toBeInTheDocument();
  });

  it('can pin the displayed effective model without choosing a different one first', async () => {
    renderSettings();
    await modelField('qwen-next');
    vi.mocked(saveModelPreference).mockResolvedValue(preference({ default_model: 'qwen-next' }));
    await pick('qwen-next');
    await waitFor(() => expect(saveModelPreference).toHaveBeenCalledExactlyOnceWith('qwen-next'));
    expect(await screen.findByRole('button', { name: 'Reset to default' })).toBeVisible();
  });

  it('clears a pin using the separate reset action and displays the resolved model', async () => {
    vi.mocked(getModelPreference).mockResolvedValue(
      preference({ default_model: 'glm-5.1', effective_model: 'glm-5.1' }),
    );
    vi.mocked(saveModelPreference).mockResolvedValue(preference());
    renderSettings();
    await modelField('glm-5.1');
    fireEvent.click(screen.getByRole('button', { name: 'Reset to default' }));
    await waitFor(() => expect(saveModelPreference).toHaveBeenCalledWith(null));
    await modelField('qwen-next');
    expect(screen.getByText('New tasks start on qwen-next.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Reset to default' })).not.toBeInTheDocument();
  });

  it('displays the deployment default for an owner who has no history', async () => {
    vi.mocked(getModelPreference).mockResolvedValue(
      preference({ last_model: null, effective_model: 'glm-5.1', deployment_default: 'glm-5.1' }),
    );
    renderSettings();
    await modelField('glm-5.1');
    expect(screen.getByText('New tasks start on glm-5.1.')).toBeInTheDocument();
    expect(saveModelPreference).not.toHaveBeenCalled();
  });

  it('leaves the field empty when neither history nor a default applies', async () => {
    vi.mocked(getModelPreference).mockResolvedValue(
      preference({ last_model: null, effective_model: null, deployment_default: null }),
    );
    renderSettings();
    expect(
      await screen.findByText(
        'Nothing is chosen for new tasks yet. Pick a model here or in the composer.',
      ),
    ).toBeVisible();
    const field = await modelField('');
    expect(field).toHaveAccessibleDescription('Choose a model');
    expect(field).toBeEnabled();
  });

  it('shows a retired pin and its actual fallback without offering the retired model', async () => {
    vi.mocked(getModelPreference).mockResolvedValue(
      preference({ default_model: 'retired-model', effective_model: 'qwen-next' }),
    );
    renderSettings();
    const field = await modelField('retired-model');
    expect(field).toHaveAccessibleDescription('retired-model (no longer available)');
    expect(screen.getByText('New tasks start on qwen-next.')).toBeInTheDocument();
    fireEvent.click(field);
    expect(screen.queryByRole('option', { name: /retired-model/ })).not.toBeInTheDocument();
    expect(screen.getAllByRole('option')).toHaveLength(MODELS.length);
  });

  it('reports a failed save without pretending the choice changed', async () => {
    renderSettings();
    const field = await modelField('qwen-next');
    vi.mocked(saveModelPreference).mockRejectedValue(new Error('refused'));
    await pick('glm-5.1');
    expect(
      await screen.findByText('Could not save. Your previous choice is unchanged.'),
    ).toBeInTheDocument();
    expect(field).toHaveValue('qwen-next');
    expect(field).toBeEnabled();
  });

  it('keeps both controls disabled while a preference save is pending', async () => {
    vi.mocked(getModelPreference).mockResolvedValue(preference({ default_model: 'qwen-next' }));
    let finish!: (value: ModelPreference) => void;
    vi.mocked(saveModelPreference).mockReturnValue(
      new Promise((resolve) => {
        finish = resolve;
      }),
    );
    renderSettings();
    const field = await modelField('qwen-next');
    await pick('glm-5.1');
    expect(field).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Reset to default' })).toBeDisabled();
    expect(screen.getByText('Saving…')).toBeVisible();
    finish(preference({ default_model: 'glm-5.1', effective_model: 'glm-5.1' }));
    await modelField('glm-5.1');
    expect(field).toBeEnabled();
  });

  it('offers no model when the catalog is empty and says so', async () => {
    vi.mocked(getModelPreference).mockResolvedValue(
      preference({ last_model: null, effective_model: null, models: [] }),
    );
    renderSettings();
    expect(
      await screen.findByText('No model is available yet. A new task waits until one is offered.'),
    ).toBeInTheDocument();
    expect(await modelField('')).toBeDisabled();
    expect(screen.queryByRole('option')).not.toBeInTheDocument();
  });

  it('reads the catalog from the preference alone, not the composer config', async () => {
    renderSettings();
    fireEvent.click(await modelField('qwen-next'));
    expect(getModelPreference).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('option', { name: 'glm-5.1' })).toBeInTheDocument();
  });

  it('displays the resolved fallback when the last model is retired', async () => {
    vi.mocked(getModelPreference).mockResolvedValue(
      preference({
        last_model: 'retired-model',
        effective_model: 'glm-5.1',
        deployment_default: 'glm-5.1',
      }),
    );
    renderSettings();
    await modelField('glm-5.1');
    expect(screen.getByText('New tasks start on glm-5.1.')).toBeInTheDocument();
    expect(screen.queryByText(/retired-model/)).not.toBeInTheDocument();
  });

  it('asks for a choice when the last model is retired and nothing else applies', async () => {
    vi.mocked(getModelPreference).mockResolvedValue(
      preference({ last_model: 'retired-model', effective_model: null }),
    );
    renderSettings();
    expect(
      await screen.findByText(
        'Nothing is chosen for new tasks yet. Pick a model here or in the composer.',
      ),
    ).toBeInTheDocument();
    expect(await modelField('')).toBeEnabled();
  });

  it('does not call a pin retired when the catalog could not be read', async () => {
    vi.mocked(getModelPreference).mockResolvedValue(
      preference({ default_model: 'glm-5.1', effective_model: null, models: [] }),
    );
    renderSettings();
    const field = await modelField('glm-5.1');
    expect(field).toHaveAccessibleDescription('glm-5.1');
    expect(field).toBeDisabled();
    expect(screen.queryByText(/no longer available/)).not.toBeInTheDocument();
    expect(
      screen.getByText('No model is available yet. A new task waits until one is offered.'),
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Reset to default' })).toBeEnabled();
  });

  it('shows one retryable error when the read fails', async () => {
    vi.mocked(getModelPreference).mockRejectedValue(new Error('offline'));
    renderSettings();
    expect(await screen.findByText('Could not load your default model.')).toBeInTheDocument();
    vi.mocked(getModelPreference).mockResolvedValue(preference());
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    await modelField('qwen-next');
    expect(getModelPreference).toHaveBeenCalledTimes(2);
  });
});
