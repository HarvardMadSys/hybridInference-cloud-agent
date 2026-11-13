// @vitest-environment jsdom
import { cleanup, render } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { LegacyModelConnectionsRedirect } from './LegacyModelConnectionsRedirect';

const navigation = vi.hoisted(() => ({ replace: vi.fn() }));
vi.mock('next/navigation', () => ({ useRouter: () => ({ replace: navigation.replace }) }));
beforeEach(() => navigation.replace.mockClear());
afterEach(cleanup);

it('opens Models for an old My Settings link to model connections', () => {
  window.history.replaceState(null, '', '/settings/my-settings?from=composer#settings-connections');
  render(<LegacyModelConnectionsRedirect />);
  expect(navigation.replace).toHaveBeenCalledWith(
    '/settings/models?from=composer#settings-connections',
  );
});

it.each(['', '#settings-profile', '#settings-notifications'])(
  'leaves My Settings alone for %j',
  (hash) => {
    window.history.replaceState(null, '', `/settings/my-settings${hash}`);
    render(<LegacyModelConnectionsRedirect />);
    expect(navigation.replace).not.toHaveBeenCalled();
  },
);
