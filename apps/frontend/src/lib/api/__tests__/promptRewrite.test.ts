import { afterEach, describe, expect, it, vi } from 'vitest';
import * as client from '../client';
import { rewritePrompt } from '../promptRewrite';

afterEach(() => vi.restoreAllMocks());

describe('prompt rewrite API', () => {
  it('sends the draft, answers and cancellation signal through the authenticated client', async () => {
    const result = { kind: 'prompt', prompt: 'Build an accessible CSV export.' };
    const fetch = vi.spyOn(client, 'fetchWithAuth').mockResolvedValue(Response.json(result));
    const controller = new AbortController();
    const body = {
      prompt: 'Add export',
      model: 'selected-model',
      clarify: false,
      answers: [{ question: 'Which format?', answer: 'CSV' }],
    };

    await expect(rewritePrompt(body, controller.signal)).resolves.toEqual(result);
    expect(fetch).toHaveBeenCalledWith(expect.any(String), '/v1/agent/prompts/rewrite', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: controller.signal,
    });
  });

  it('preserves a refusal for the composer to report', async () => {
    vi.spyOn(client, 'fetchWithAuth').mockResolvedValue(
      Response.json({ detail: 'This model is unavailable.' }, { status: 403 }),
    );
    await expect(
      rewritePrompt({ prompt: 'Add export', model: 'unavailable', clarify: true }),
    ).rejects.toThrow('This model is unavailable.');
  });
});
