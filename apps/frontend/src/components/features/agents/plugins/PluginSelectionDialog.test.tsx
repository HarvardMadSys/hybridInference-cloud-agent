// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { useState } from 'react';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { AgentPluginInputApi } from '@/lib/api/agents';
import { PluginSelectionDialog } from './PluginSelectionDialog';

afterEach(cleanup);

const reviewPlugin = {
  plugin: 'code-review@claude-plugins-official',
  marketplace: 'anthropics/claude-plugins-official',
};

function renderDialog({
  initial = [],
  blockedReason,
  onClose = vi.fn(),
  runtime = 'claude-code',
}: {
  initial?: AgentPluginInputApi[];
  blockedReason?: string;
  onClose?: () => void;
  runtime?: string;
} = {}) {
  const changed = vi.fn();
  function Controlled() {
    const [selected, setSelected] = useState(initial);
    return (
      <PluginSelectionDialog
        runtime={runtime}
        selected={selected}
        blockedReason={blockedReason}
        onClose={onClose}
        onChange={(next) => {
          changed(next);
          setSelected(next);
        }}
      />
    );
  }
  render(<Controlled />);
  return { changed, onClose };
}

function add(plugin = reviewPlugin.plugin, marketplace = reviewPlugin.marketplace) {
  fireEvent.change(screen.getByLabelText('Plugin'), { target: { value: plugin } });
  fireEvent.change(screen.getByLabelText(/Marketplace source/), { target: { value: marketplace } });
  fireEvent.click(screen.getByRole('button', { name: 'Add plugin' }));
}

describe('PluginSelectionDialog', () => {
  it('offers a marketplace source for Codex native plugins', () => {
    const { changed } = renderDialog({ runtime: 'codex' });
    add('review@marketplace', 'owner/marketplace');
    expect(changed).toHaveBeenLastCalledWith([
      { plugin: 'review@marketplace', marketplace: 'owner/marketplace' },
    ]);
  });

  it.each(['review.plugin@marketplace', 'review@market.place'])(
    'rejects dotted Codex native IDs: %s',
    (plugin) => {
      const { changed } = renderDialog({ runtime: 'codex' });
      add(plugin, 'owner/marketplace');
      expect(screen.getByRole('alert')).toHaveTextContent('cannot contain dots');
      expect(changed).not.toHaveBeenCalled();
    },
  );

  it.each(['pi', 'opencode', 'kilo'])(
    'adds npm packages without a marketplace for %s',
    (runtime) => {
      const { changed } = renderDialog({ runtime });
      expect(screen.queryByLabelText(/Marketplace source/)).not.toBeInTheDocument();
      fireEvent.change(screen.getByLabelText(/^npm package/), {
        target: { value: ' npm:@scope/plugin@1.2.3 ' },
      });
      fireEvent.click(screen.getByRole('button', { name: 'Add plugin' }));
      expect(changed).toHaveBeenLastCalledWith([{ plugin: '@scope/plugin@1.2.3' }]);
      fireEvent.change(screen.getByLabelText(/^npm package/), {
        target: { value: '@scope/plugin@2.0.0' },
      });
      fireEvent.click(screen.getByRole('button', { name: 'Add plugin' }));
      expect(screen.getByRole('alert')).toHaveTextContent('already selected');
      expect(changed).toHaveBeenCalledTimes(1);
    },
  );

  it.each([
    'plugin@latest',
    'plugin@^1.2.3',
    'plugin@~1.2.3',
    'https://example.com/plugin.git',
    '--plugin',
    'Plugin',
  ])('rejects non-package or non-exact-version input %s', (plugin) => {
    const { changed } = renderDialog({ runtime: 'pi' });
    fireEvent.change(screen.getByLabelText(/^npm package/), { target: { value: plugin } });
    fireEvent.click(screen.getByRole('button', { name: 'Add plugin' }));
    expect(screen.getByRole('alert')).toHaveTextContent('npm package name');
    expect(changed).not.toHaveBeenCalled();
  });

  it('adds trimmed configuration, prevents duplicate plugins, and allows removal', () => {
    const { changed } = renderDialog();
    expect(screen.getByLabelText('Plugin')).toHaveFocus();
    add(` ${reviewPlugin.plugin} `, ` ${reviewPlugin.marketplace} `);
    expect(changed).toHaveBeenLastCalledWith([reviewPlugin]);
    expect(screen.getByLabelText('Plugin')).toHaveValue('');
    add(reviewPlugin.plugin, 'another/marketplace');
    expect(screen.getByRole('alert')).toHaveTextContent('already selected');
    expect(changed).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: `Remove ${reviewPlugin.plugin}` }));
    expect(changed).toHaveBeenLastCalledWith([]);
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('rejects conflicting sources for plugins using the same marketplace name', () => {
    const { changed } = renderDialog({ initial: [reviewPlugin] });
    add('another@claude-plugins-official', 'another/marketplace');
    expect(screen.getByRole('alert')).toHaveTextContent(
      'same marketplace must use the same source',
    );
    expect(changed).not.toHaveBeenCalled();
  });

  it.each([
    ['missing-marketplace', reviewPlugin.marketplace, 'name@marketplace'],
    [reviewPlugin.plugin, 'https://user:secret@example.com/plugins.git', 'without credentials'],
    [reviewPlugin.plugin, 'https://@example.com/plugins.git', 'without credentials'],
    [reviewPlugin.plugin, 'http://example.com/plugins.git', 'HTTPS'],
    [reviewPlugin.plugin, 'https://example.com:8443/plugins.git', 'HTTPS'],
    [reviewPlugin.plugin, 'https://example.com/plugins.git?token=secret', 'without credentials'],
    [reviewPlugin.plugin, 'https://example.com/plugins.git#secret', 'without credentials'],
  ])('refuses invalid configuration %s from %s', (plugin, marketplace, message) => {
    const { changed } = renderDialog();
    add(plugin, marketplace);
    expect(screen.getByRole('alert')).toHaveTextContent(message);
    expect(changed).not.toHaveBeenCalled();
  });

  it('accepts a public HTTPS Git source and limits the selection to eight plugins', () => {
    const initial = Array.from({ length: 7 }, (_, i) => ({
      plugin: `plugin-${i}@marketplace`,
      marketplace: 'owner/marketplace',
    }));
    const { changed } = renderDialog({ initial });
    const last = {
      plugin: 'final@public-marketplace',
      marketplace: 'https://example.com/plugins.git',
    };
    add(last.plugin, last.marketplace);
    expect(changed).toHaveBeenLastCalledWith([...initial, last]);
    expect(screen.getByRole('button', { name: 'Add plugin' })).toBeDisabled();
    expect(within(screen.getByRole('dialog')).getAllByRole('listitem')).toHaveLength(8);
  });

  it('keeps selected plugins removable when the runtime cannot install them', () => {
    const { changed } = renderDialog({
      initial: [reviewPlugin],
      blockedReason: 'Choose a supported agent.',
    });
    expect(screen.getByRole('alert')).toHaveTextContent('Choose a supported agent.');
    expect(screen.getByLabelText('Plugin')).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Add plugin' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: `Remove ${reviewPlugin.plugin}` }));
    expect(changed).toHaveBeenLastCalledWith([]);
  });

  it('dismisses with Escape or Done', () => {
    const { onClose } = renderDialog();
    fireEvent.keyDown(screen.getByLabelText('Plugin'), { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: 'Done' }));
    expect(onClose).toHaveBeenCalledTimes(2);
  });
});
