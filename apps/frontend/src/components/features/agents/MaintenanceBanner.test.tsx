// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { MaintenanceBanner } from './MaintenanceBanner';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function stub(body: unknown, ok = true) {
  const fetchMock = vi.fn().mockResolvedValue({ ok, json: async () => body });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

describe('maintenance banner', () => {
  it('says why changes are refused while the service reports maintenance', async () => {
    stub({ maintenance: { active: true } });
    render(<MaintenanceBanner />);
    expect(await screen.findByRole('status')).toBeTruthy();
    expect(screen.getByText(/changes are refused/)).toBeTruthy();
  });

  it('stays away otherwise, and from an earlier control plane', async () => {
    const fetchMock = stub({ branding: {} });
    render(<MaintenanceBanner />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(screen.queryByRole('status')).toBeNull();
  });
});
