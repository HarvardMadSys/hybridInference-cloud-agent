// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { getAgentAdminSettings } from '@/lib/api/adminSettings';

import { AdminSetupNotice } from './AdminSetupNotice';

vi.mock('@/lib/api/adminSettings', () => ({ getAgentAdminSettings: vi.fn() }));
afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

describe('administrator setup notification', () => {
  it('links administrators to incomplete configuration', async () => {
    vi.mocked(getAgentAdminSettings).mockResolvedValue({
      revision: 1,
      definitions: [],
      values: {},
      secret_configured: {},
      bootstrap: [],
      setup: {
        required: true,
        bootstrap_missing: [],
        groups: [
          {
            key: 'sign_in',
            label: 'Gateway sign-in',
            required: true,
            configured: false,
            needs_setup: true,
            missing: [],
          },
        ],
      },
    });
    render(<AdminSetupNotice />);
    expect(await screen.findByRole('status')).toHaveTextContent('Gateway sign-in');
    expect(screen.getByRole('link', { name: 'Complete missing settings' })).toHaveAttribute(
      'href',
      '/admin/settings',
    );
  });

  it('does not infer missing configuration from a failed or unauthorized read', async () => {
    vi.mocked(getAgentAdminSettings).mockRejectedValue(new Error('Access denied'));
    const { container } = render(<AdminSetupNotice />);
    await vi.waitFor(() => expect(getAgentAdminSettings).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });
});
