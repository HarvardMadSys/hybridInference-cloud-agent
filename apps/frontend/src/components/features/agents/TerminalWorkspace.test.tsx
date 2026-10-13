// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { StrictMode } from 'react';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  createAgentTerminal,
  deleteAgentTerminal,
  listAgentTerminals,
  resizeAgentTerminal,
  streamAgentTerminal,
  writeAgentTerminalInput,
  type AgentTerminalApi,
} from '@/lib/api/agents';

import { TerminalWorkspace } from './TerminalWorkspace';

const xtermHarness = vi.hoisted(() => ({
  instances: [] as Array<{
    options: Record<string, unknown>;
    rows: number;
    cols: number;
    write: ReturnType<typeof vi.fn>;
    writeln: ReturnType<typeof vi.fn>;
    reset: ReturnType<typeof vi.fn>;
    focus: ReturnType<typeof vi.fn>;
    dispose: ReturnType<typeof vi.fn>;
    emitData: (data: string) => void;
    emitBinary: (data: string) => void;
  }>,
  fits: [] as Array<{ fit: ReturnType<typeof vi.fn> }>,
  // Parse callbacks of writes xterm has not finished, while a test holds them.
  heldWrites: null as Array<() => void> | null,
}));

vi.mock('@xterm/xterm', () => ({
  Terminal: class MockTerminal {
    options: Record<string, unknown>;
    rows = 24;
    cols = 80;
    write = vi.fn((_data: unknown, callback?: () => void) => {
      if (!callback) return;
      if (xtermHarness.heldWrites) xtermHarness.heldWrites.push(callback);
      else callback();
    });
    writeln = vi.fn();
    reset = vi.fn();
    focus = vi.fn();
    dispose = vi.fn();
    private dataHandler: (data: string) => void = () => undefined;
    private binaryHandler: (data: string) => void = () => undefined;

    constructor(options: Record<string, unknown>) {
      this.options = options;
      xtermHarness.instances.push(this);
    }

    loadAddon() {}
    open() {}
    onData(handler: (data: string) => void) {
      this.dataHandler = handler;
      return { dispose: vi.fn() };
    }
    emitData(data: string) {
      this.dataHandler(data);
    }
    onBinary(handler: (data: string) => void) {
      this.binaryHandler = handler;
      return { dispose: vi.fn() };
    }
    emitBinary(data: string) {
      this.binaryHandler(data);
    }
  },
}));

vi.mock('@xterm/addon-fit', () => ({
  FitAddon: class MockFitAddon {
    fit = vi.fn();
    constructor() {
      xtermHarness.fits.push(this);
    }
  },
}));

vi.mock('@/lib/api/agents', () => ({
  createAgentTerminal: vi.fn(),
  deleteAgentTerminal: vi.fn(),
  listAgentTerminals: vi.fn(),
  resizeAgentTerminal: vi.fn(),
  streamAgentTerminal: vi.fn(),
  writeAgentTerminalInput: vi.fn(),
}));

function terminal(id: string, shell = 'zsh'): AgentTerminalApi {
  return {
    id,
    shell,
    state: 'running',
    cwd: '/workspace',
    rows: 24,
    cols: 80,
    last_seq: 0,
  };
}

function rect(left: number, right: number): DOMRect {
  return {
    bottom: 20,
    height: 20,
    left,
    right,
    top: 0,
    width: right - left,
    x: left,
    y: 0,
    toJSON: () => ({}),
  } as DOMRect;
}

describe('TerminalWorkspace', () => {
  beforeEach(() => {
    xtermHarness.instances.length = 0;
    xtermHarness.fits.length = 0;
    xtermHarness.heldWrites = null;
    vi.mocked(createAgentTerminal).mockReset();
    vi.mocked(deleteAgentTerminal).mockReset();
    vi.mocked(listAgentTerminals).mockReset();
    vi.mocked(resizeAgentTerminal).mockReset().mockResolvedValue();
    vi.mocked(writeAgentTerminalInput).mockReset().mockResolvedValue();
    vi.mocked(streamAgentTerminal)
      .mockReset()
      .mockImplementation(async (_job, _terminal, opts) => {
        opts.onOutput({ seq: 1, data: 'aGk=' });
      });
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it('uses the sandbox manager session limit when deciding whether another terminal can open', async () => {
    vi.mocked(listAgentTerminals).mockResolvedValue({
      terminals: [terminal('term-1')],
      max_sessions: 1,
    });
    render(<TerminalWorkspace jobId="job-1" active />);
    await screen.findByRole('application', { name: 'Terminal 1 terminal' });
    expect(screen.getByRole('button', { name: 'New terminal' })).toBeDisabled();
    expect(createAgentTerminal).not.toHaveBeenCalled();
  });

  it('passes the advertised input limit to the active terminal without recreating it', async () => {
    const first = terminal('term-1');
    vi.mocked(listAgentTerminals).mockResolvedValue({
      terminals: [first],
      max_sessions: 4,
      max_input_bytes: 1024,
    });
    render(<TerminalWorkspace jobId="job-1" active />);
    await screen.findByRole('application', { name: 'Terminal 1 terminal' });
    await waitFor(() => expect(xtermHarness.instances).toHaveLength(1));
    xtermHarness.instances[0].emitData('paste');
    await waitFor(() =>
      expect(writeAgentTerminalInput).toHaveBeenCalledWith('job-1', first.id, 'paste', 1024),
    );
    expect(xtermHarness.instances).toHaveLength(1);
  });

  it('resets the advertised session limit when switching workspaces', async () => {
    vi.mocked(listAgentTerminals)
      .mockResolvedValueOnce({ terminals: [terminal('term-1')], max_sessions: 1 })
      .mockResolvedValueOnce([terminal('term-2')]);
    const view = render(<TerminalWorkspace jobId="job-1" active />);
    await screen.findByRole('application', { name: 'Terminal 1 terminal' });
    expect(screen.getByRole('button', { name: 'New terminal' })).toBeDisabled();
    view.rerender(<TerminalWorkspace jobId="job-2" active />);
    await screen.findByRole('application', { name: 'Terminal 1 terminal' });
    await waitFor(() => expect(screen.getByRole('button', { name: 'New terminal' })).toBeEnabled());
  });

  it('creates, switches, splits, and kills real terminal sessions', async () => {
    const first = terminal('term-1');
    const second = terminal('term-2', 'bash');
    const third = terminal('term-3');
    const fourth = terminal('term-4');
    vi.mocked(listAgentTerminals).mockResolvedValue([]);
    vi.mocked(createAgentTerminal)
      .mockResolvedValueOnce(first)
      .mockResolvedValueOnce(second)
      .mockResolvedValueOnce(third)
      .mockResolvedValueOnce(fourth);
    vi.mocked(deleteAgentTerminal).mockResolvedValue();

    render(<TerminalWorkspace jobId="job-1" active />);
    const emptyNew = await screen.findByRole('button', { name: 'New terminal' });
    fireEvent.click(emptyNew);

    expect(
      await screen.findByRole('application', { name: 'Terminal 1 terminal' }),
    ).toBeInTheDocument();
    expect(screen.queryByText('pytest -q')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'New terminal' }));
    await waitFor(() => expect(createAgentTerminal).toHaveBeenCalledTimes(2));
    expect(screen.getByRole('application', { name: 'Terminal 2 terminal' })).toBeInTheDocument();
    const tablist = screen.getByRole('tablist', { name: 'Terminals in pane 1' });
    expect(within(tablist).getAllByRole('tab')).toHaveLength(2);
    expect(within(tablist).getByRole('tab', { name: 'Terminal 1 · zsh' })).toHaveAttribute(
      'aria-selected',
      'false',
    );
    expect(within(tablist).getByRole('tab', { name: 'Terminal 2 · bash' })).toHaveAttribute(
      'aria-selected',
      'true',
    );
    fireEvent.click(within(tablist).getByRole('tab', { name: 'Terminal 1 · zsh' }));
    expect(
      await screen.findByRole('application', { name: 'Terminal 1 terminal' }),
    ).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Terminal 1 · zsh' })).toHaveAttribute(
      'aria-selected',
      'true',
    );

    fireEvent.click(screen.getByRole('button', { name: 'Split terminal' }));
    await waitFor(() => expect(createAgentTerminal).toHaveBeenCalledTimes(3));
    expect(screen.getAllByRole('application')).toHaveLength(2);

    const firstPane = screen.getByRole('region', { name: 'Terminal 1 pane' });
    fireEvent.click(within(firstPane).getByRole('button', { name: 'New terminal' }));
    await waitFor(() => expect(createAgentTerminal).toHaveBeenCalledTimes(4));
    expect(screen.getAllByRole('application')).toHaveLength(2);
    expect(screen.getByRole('application', { name: 'Terminal 4 terminal' })).toBeInTheDocument();
    expect(screen.getByRole('application', { name: 'Terminal 3 terminal' })).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Kill Terminal 4' }));
    await waitFor(() => expect(deleteAgentTerminal).toHaveBeenCalledWith('job-1', fourth.id));
    expect(screen.getAllByRole('application')).toHaveLength(1);
  });

  it('navigates terminal tabs with the keyboard without moving focus into a shell', async () => {
    vi.mocked(listAgentTerminals).mockResolvedValue([
      terminal('term-1'),
      terminal('term-2', 'bash'),
      terminal('term-3'),
    ]);
    render(<TerminalWorkspace jobId="job-1" active />);
    await waitFor(() => expect(xtermHarness.instances).toHaveLength(1));

    const tablist = screen.getByRole('tablist', { name: 'Terminals in pane 1' });
    const first = within(tablist).getByRole('tab', { name: 'Terminal 1 · zsh' });
    const tabs = within(tablist).getAllByRole('tab');
    expect(tabs.map((tab) => tab.tabIndex)).toEqual([0, -1, -1]);
    for (const tab of tabs) {
      const panel = document.getElementById(tab.getAttribute('aria-controls')!);
      expect(panel).toHaveAttribute('role', 'tabpanel');
      expect(panel).toHaveAttribute('aria-labelledby', tab.id);
      expect(panel?.hidden).toBe(tab !== first);
    }
    first.focus();
    xtermHarness.instances[0].focus.mockClear();

    let mountedTerminals = 1;
    for (const [key, label] of [
      ['ArrowRight', 'Terminal 2 · bash'],
      ['End', 'Terminal 3 · zsh'],
      ['ArrowRight', 'Terminal 1 · zsh'],
      ['ArrowLeft', 'Terminal 3 · zsh'],
      ['Home', 'Terminal 1 · zsh'],
    ]) {
      fireEvent.keyDown(document.activeElement!, { key });
      const selected = screen.getByRole('tab', { name: label });
      expect(selected).toHaveFocus();
      expect(selected).toHaveAttribute('aria-selected', 'true');
      expect(selected).toHaveAttribute('tabindex', '0');
      const panel = document.getElementById(selected.getAttribute('aria-controls')!);
      expect(panel).toBeVisible();
      mountedTerminals += 1;
      await waitFor(() => expect(xtermHarness.instances).toHaveLength(mountedTerminals));
    }
    expect(xtermHarness.instances.every((xterm) => xterm.focus.mock.calls.length === 0)).toBe(true);
    expect(writeAgentTerminalInput).not.toHaveBeenCalled();
  });

  it('keeps keyboard focus in its pane when selecting a shell visible in the other pane', async () => {
    vi.mocked(listAgentTerminals).mockResolvedValue([terminal('term-1')]);
    vi.mocked(createAgentTerminal).mockResolvedValue(terminal('term-2', 'bash'));
    render(<TerminalWorkspace jobId="job-1" active />);
    await screen.findByRole('application', { name: 'Terminal 1 terminal' });
    fireEvent.click(screen.getByRole('button', { name: 'Split terminal' }));
    await waitFor(() => expect(screen.getAllByRole('application')).toHaveLength(2));
    await waitFor(() => expect(xtermHarness.instances).toHaveLength(2));
    for (const xterm of xtermHarness.instances) xterm.focus.mockClear();

    const firstPaneTabs = screen.getByRole('tablist', { name: 'Terminals in pane 1' });
    const selected = within(firstPaneTabs).getByRole('tab', { name: 'Terminal 1 · zsh' });
    selected.focus();
    fireEvent.keyDown(selected, { key: 'ArrowRight' });

    const firstPane = screen.getByRole('tablist', { name: 'Terminals in pane 1' });
    const secondPane = screen.getByRole('tablist', { name: 'Terminals in pane 2' });
    expect(within(firstPane).getByRole('tab', { name: 'Terminal 2 · bash' })).toHaveFocus();
    expect(within(secondPane).getByRole('tab', { name: 'Terminal 1 · zsh' })).toHaveAttribute(
      'aria-selected',
      'true',
    );
    const panelIds = screen.getAllByRole('tab').map((tab) => tab.getAttribute('aria-controls'));
    expect(new Set(panelIds).size).toBe(panelIds.length);
    await waitFor(() => expect(xtermHarness.instances).toHaveLength(4));
    expect(xtermHarness.instances.every((xterm) => xterm.focus.mock.calls.length === 0)).toBe(true);
  });

  it('focuses the shell when selecting a terminal with the pointer', async () => {
    vi.mocked(listAgentTerminals).mockResolvedValue([
      terminal('term-1'),
      terminal('term-2', 'bash'),
    ]);
    render(<TerminalWorkspace jobId="job-1" active />);
    await waitFor(() => expect(xtermHarness.instances).toHaveLength(1));
    const second = screen.getByRole('tab', { name: 'Terminal 2 · bash' });
    second.focus();
    fireEvent.click(second, { detail: 1 });
    await waitFor(() => expect(xtermHarness.instances).toHaveLength(2));
    expect(xtermHarness.instances[1].focus).toHaveBeenCalled();
  });

  it('removes the last terminal when cleanup finishes after delete reports an error', async () => {
    const only = terminal('term-only');
    vi.mocked(listAgentTerminals).mockResolvedValueOnce([only]).mockResolvedValueOnce([]);
    vi.mocked(deleteAgentTerminal).mockRejectedValue(
      new Error('terminal cleanup could not be confirmed; retry kill'),
    );

    render(<TerminalWorkspace jobId="job-1" active />);
    fireEvent.click(await screen.findByRole('button', { name: 'Kill Terminal 1' }));

    await waitFor(() => expect(listAgentTerminals).toHaveBeenCalledTimes(2));
    expect(deleteAgentTerminal).toHaveBeenCalledWith('job-1', only.id);
    expect(screen.queryByRole('application')).not.toBeInTheDocument();
    expect(screen.getByText('No open terminals')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('finishes its initial terminal load under React Strict Mode', async () => {
    vi.mocked(listAgentTerminals).mockResolvedValue([]);

    render(
      <StrictMode>
        <TerminalWorkspace jobId="job-1" active />
      </StrictMode>,
    );

    expect(await screen.findByRole('button', { name: 'New terminal' })).toBeEnabled();
    expect(screen.queryByText('Loading terminals…')).not.toBeInTheDocument();
  });

  it('waits for workspace readiness, then loads automatically', async () => {
    vi.mocked(listAgentTerminals).mockResolvedValue([]);

    const view = render(<TerminalWorkspace jobId="job-1" active ready={false} />);

    expect(screen.getByText('Loading terminals…')).toBeInTheDocument();
    expect(screen.queryByRole('note')).not.toBeInTheDocument();
    expect(listAgentTerminals).not.toHaveBeenCalled();

    view.rerender(<TerminalWorkspace jobId="job-1" active ready />);

    expect(await screen.findByRole('button', { name: 'New terminal' })).toBeEnabled();
    expect(listAgentTerminals).toHaveBeenCalledWith('job-1', { includeLimits: true });
    expect(screen.queryByText('Loading terminals…')).not.toBeInTheDocument();
  });

  it('keeps the selected tab visible when a split narrows the first pane', async () => {
    const first = terminal('term-1');
    const second = terminal('term-2', 'bash');
    const third = terminal('term-3');
    const fourth = terminal('term-4', 'bash');
    vi.mocked(listAgentTerminals).mockResolvedValue([first, second, third]);
    vi.mocked(createAgentTerminal).mockResolvedValue(fourth);
    let split = false;
    vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (
      this: Element,
    ) {
      if (this.getAttribute('role') === 'tablist') {
        return rect(0, split ? 100 : 300);
      }
      if (this.textContent?.includes('Terminal 3')) return rect(220, 300);
      return rect(0, 80);
    });

    render(<TerminalWorkspace jobId="job-1" active />);

    fireEvent.click(await screen.findByRole('tab', { name: 'Terminal 3 · zsh' }));

    expect(
      await screen.findByRole('application', { name: 'Terminal 3 terminal' }),
    ).toBeInTheDocument();
    expect(screen.getByRole('tablist', { name: 'Terminals in pane 1' }).scrollLeft).toBe(0);

    split = true;
    fireEvent.click(screen.getByRole('button', { name: 'Split terminal' }));

    const firstPane = await screen.findByRole('region', { name: 'Terminal 3 pane' });
    const tablist = within(firstPane).getByRole('tablist', { name: 'Terminals in pane 1' });
    await waitFor(() => expect(tablist.scrollLeft).toBe(200));
  });

  it('keeps the selected tab visible when the tab strip resizes', async () => {
    const first = terminal('term-1');
    const second = terminal('term-2', 'bash');
    const third = terminal('term-3');
    vi.mocked(listAgentTerminals).mockResolvedValue([first, second, third]);
    let paneWidth = 300;
    let resizeTabList: (() => void) | undefined;

    vi.stubGlobal(
      'ResizeObserver',
      class {
        constructor(private readonly callback: ResizeObserverCallback) {}
        observe(target: Element) {
          if (target.getAttribute('role') === 'tablist') {
            resizeTabList = () => this.callback([], this as unknown as ResizeObserver);
          }
        }
        disconnect() {}
        unobserve() {}
      },
    );
    vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (
      this: Element,
    ) {
      if (this.getAttribute('role') === 'tablist') return rect(0, paneWidth);
      if (this.textContent?.includes('Terminal 3')) return rect(220, 300);
      return rect(0, 80);
    });

    render(<TerminalWorkspace jobId="job-1" active />);
    fireEvent.click(await screen.findByRole('tab', { name: 'Terminal 3 · zsh' }));

    const tablist = screen.getByRole('tablist', { name: 'Terminals in pane 1' });
    expect(tablist.scrollLeft).toBe(0);
    await waitFor(() => expect(resizeTabList).toBeTypeOf('function'));

    paneWidth = 100;
    resizeTabList?.();

    expect(tablist.scrollLeft).toBe(200);
  });

  it('swaps panes when selecting a terminal already visible in the other pane', async () => {
    const first = terminal('term-1');
    const second = terminal('term-2', 'bash');
    vi.mocked(listAgentTerminals).mockResolvedValue([first]);
    vi.mocked(createAgentTerminal).mockResolvedValue(second);

    render(<TerminalWorkspace jobId="job-1" active />);
    await screen.findByRole('application', { name: 'Terminal 1 terminal' });

    fireEvent.click(screen.getByRole('button', { name: 'Split terminal' }));
    await screen.findByRole('application', { name: 'Terminal 2 terminal' });

    const firstPane = screen.getByRole('region', { name: 'Terminal 1 pane' });
    fireEvent.click(within(firstPane).getByRole('tab', { name: 'Terminal 2 · bash' }));

    await waitFor(() => {
      const firstPaneTabs = screen.getByRole('tablist', { name: 'Terminals in pane 1' });
      const secondPaneTabs = screen.getByRole('tablist', { name: 'Terminals in pane 2' });
      expect(within(firstPaneTabs).getByRole('tab', { name: 'Terminal 2 · bash' })).toHaveAttribute(
        'aria-selected',
        'true',
      );
      expect(within(secondPaneTabs).getByRole('tab', { name: 'Terminal 1 · zsh' })).toHaveAttribute(
        'aria-selected',
        'true',
      );
    });
    expect(screen.getAllByRole('application', { name: 'Terminal 1 terminal' })).toHaveLength(1);
    expect(screen.getAllByRole('application', { name: 'Terminal 2 terminal' })).toHaveLength(1);
  });

  it('writes streamed bytes, sends xterm input, fits and resizes, then cleans up', async () => {
    const first = terminal('term-1');
    vi.mocked(listAgentTerminals).mockResolvedValue([first]);
    const streamSignal = { current: undefined as AbortSignal | undefined };
    vi.mocked(streamAgentTerminal).mockImplementation(async (_job, _terminal, opts) => {
      streamSignal.current = opts.signal;
      opts.onReset({ seq: 1, reason: 'output_truncated' });
      opts.onOutput({ seq: 1, data: 'aGk=' });
    });

    const view = render(<TerminalWorkspace jobId="job-1" active />);
    await screen.findByRole('application', { name: 'Terminal 1 terminal' });
    await waitFor(() => expect(xtermHarness.instances).toHaveLength(1));
    const xterm = xtermHarness.instances[0];

    // The reset is written in-band (RIS), so it cannot overtake output xterm
    // has queued but not yet drawn, as a synchronous `reset()` would.
    expect(xterm.write.mock.calls[0][0]).toBe('\x1bc\x1b[90m[Earlier output truncated]\x1b[0m\r\n');
    expect(Array.from(xterm.write.mock.calls[1][0] as Uint8Array)).toEqual([104, 105]);
    expect(xtermHarness.fits[0].fit).toHaveBeenCalled();
    expect(resizeAgentTerminal).toHaveBeenCalledWith('job-1', first.id, 24, 80);

    xterm.emitData('ls\r');
    await waitFor(() =>
      expect(writeAgentTerminalInput).toHaveBeenCalledWith('job-1', first.id, 'ls\r'),
    );

    view.unmount();
    expect(streamSignal.current?.aborted).toBe(true);
    expect(xterm.dispose).toHaveBeenCalled();
  });

  it('disables input when the terminal process is not running', async () => {
    const first = { ...terminal('term-1'), state: 'closed' as const };
    vi.mocked(listAgentTerminals).mockResolvedValue([first]);

    render(<TerminalWorkspace jobId="job-1" active />);
    await screen.findByRole('region', { name: 'Terminal 1 pane' });
    expect(listAgentTerminals).toHaveBeenCalledWith('job-1', { includeLimits: true });
    await waitFor(() => expect(xtermHarness.instances).toHaveLength(1));
    expect(xtermHarness.instances[0].options.disableStdin).toBe(true);
    xtermHarness.instances[0].emitData('pwd\r');
    expect(writeAgentTerminalInput).not.toHaveBeenCalled();
  });

  it('pauses an existing terminal during retry preparation and resumes it automatically', async () => {
    const first = terminal('term-1');
    vi.mocked(listAgentTerminals).mockResolvedValue([first]);

    const view = render(<TerminalWorkspace jobId="job-1" active ready />);
    await screen.findByRole('application', { name: 'Terminal 1 terminal' });
    await waitFor(() => expect(xtermHarness.instances).toHaveLength(1));
    const xterm = xtermHarness.instances[0];

    view.rerender(<TerminalWorkspace jobId="job-1" active ready={false} />);
    expect(xterm.options.disableStdin).toBe(true);
    xterm.emitData('blocked');
    expect(writeAgentTerminalInput).not.toHaveBeenCalled();

    view.rerender(<TerminalWorkspace jobId="job-1" active ready />);
    expect(xterm.options.disableStdin).toBe(false);
    xterm.emitData('resumed');
    await waitFor(() =>
      expect(writeAgentTerminalInput).toHaveBeenCalledWith('job-1', first.id, 'resumed'),
    );
  });

  it('cleans up a terminal created after navigating to another job', async () => {
    const stale = terminal('term-stale');
    let resolveCreate!: (session: AgentTerminalApi) => void;
    vi.mocked(listAgentTerminals).mockResolvedValue([]);
    vi.mocked(createAgentTerminal).mockImplementation(
      () =>
        new Promise<AgentTerminalApi>((resolve) => {
          resolveCreate = resolve;
        }),
    );
    vi.mocked(deleteAgentTerminal).mockResolvedValue();

    const view = render(<TerminalWorkspace jobId="job-a" active />);
    fireEvent.click(await screen.findByRole('button', { name: 'New terminal' }));
    view.rerender(<TerminalWorkspace jobId="job-b" active />);
    await waitFor(() =>
      expect(listAgentTerminals).toHaveBeenCalledWith('job-b', { includeLimits: true }),
    );

    resolveCreate(stale);
    await waitFor(() => expect(deleteAgentTerminal).toHaveBeenCalledWith('job-a', stale.id));
    expect(screen.queryByRole('application')).not.toBeInTheDocument();
  });

  it('cleans up a terminal whose creation finishes after unmount', async () => {
    const stale = terminal('term-stale');
    let resolveCreate!: (session: AgentTerminalApi) => void;
    vi.mocked(listAgentTerminals).mockResolvedValue([]);
    vi.mocked(createAgentTerminal).mockImplementation(
      () =>
        new Promise<AgentTerminalApi>((resolve) => {
          resolveCreate = resolve;
        }),
    );
    vi.mocked(deleteAgentTerminal).mockResolvedValue();

    const view = render(<TerminalWorkspace jobId="job-a" active />);
    fireEvent.click(await screen.findByRole('button', { name: 'New terminal' }));
    view.unmount();
    resolveCreate(stale);

    await waitFor(() => expect(deleteAgentTerminal).toHaveBeenCalledWith('job-a', stale.id));
  });

  it('shows a load error and retries before allowing new sessions', async () => {
    const existing = terminal('term-existing');
    vi.mocked(listAgentTerminals)
      .mockRejectedValueOnce(new Error('Workspace is unavailable'))
      .mockResolvedValueOnce([existing]);

    render(<TerminalWorkspace jobId="job-1" active />);

    expect(await screen.findByRole('alert')).toHaveTextContent('Workspace is unavailable');
    expect(screen.getByText('No open terminals')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'New terminal' })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));

    expect(
      await screen.findByRole('application', { name: 'Terminal 1 terminal' }),
    ).toBeInTheDocument();
    expect(listAgentTerminals).toHaveBeenCalledTimes(2);
  });

  it('does not update UI when a kill finishes after the job changes', async () => {
    const oldTerminal = terminal('term-old');
    const newTerminal = terminal('term-new');
    let resolveKill!: () => void;
    vi.mocked(listAgentTerminals).mockImplementation(async (jobId) =>
      jobId === 'job-a' ? [oldTerminal] : [newTerminal],
    );
    vi.mocked(deleteAgentTerminal).mockImplementation(
      () =>
        new Promise<void>((resolve) => {
          resolveKill = resolve;
        }),
    );

    const view = render(<TerminalWorkspace jobId="job-a" active />);
    fireEvent.click(await screen.findByRole('button', { name: 'Kill Terminal 1' }));
    expect(screen.getByRole('tab', { name: 'Terminal 1 · zsh' })).toBeDisabled();
    view.rerender(<TerminalWorkspace jobId="job-b" active />);
    const selectedTab = await screen.findByRole('tab', { name: 'Terminal 1 · zsh' });
    expect(selectedTab).toHaveAttribute('aria-selected', 'true');
    expect(selectedTab).toBeEnabled();

    resolveKill();
    await waitFor(() => expect(deleteAgentTerminal).toHaveBeenCalledWith('job-a', oldTerminal.id));
    expect(selectedTab).toHaveAttribute('aria-selected', 'true');
  });

  it('batches input while preserving byte order across an in-flight write', async () => {
    const first = terminal('term-1');
    vi.mocked(listAgentTerminals).mockResolvedValue([first]);
    let releaseFirst!: () => void;
    vi.mocked(writeAgentTerminalInput)
      .mockImplementationOnce(
        () =>
          new Promise<void>((resolve) => {
            releaseFirst = resolve;
          }),
      )
      .mockResolvedValueOnce();

    render(<TerminalWorkspace jobId="job-1" active />);
    await screen.findByRole('application', { name: 'Terminal 1 terminal' });
    await waitFor(() => expect(xtermHarness.instances).toHaveLength(1));

    xtermHarness.instances[0].emitData('a');
    xtermHarness.instances[0].emitData('b');
    await waitFor(() => expect(writeAgentTerminalInput).toHaveBeenCalledTimes(1));
    expect(writeAgentTerminalInput).toHaveBeenNthCalledWith(1, 'job-1', first.id, 'ab');

    xtermHarness.instances[0].emitData('c');
    xtermHarness.instances[0].emitData('d');
    releaseFirst();
    await waitFor(() => expect(writeAgentTerminalInput).toHaveBeenCalledTimes(2));
    expect(writeAgentTerminalInput).toHaveBeenNthCalledWith(2, 'job-1', first.id, 'cd');
  });

  it('does not make the first click wait for the terminal listing', async () => {
    // The listing answers "which terminals already exist", which is not a
    // precondition for opening another one — and it is a full round trip. It
    // used to disable the button until it landed, so the first click waited on
    // an answer it did not need.
    let settleListing: (items: AgentTerminalApi[]) => void = () => undefined;
    vi.mocked(listAgentTerminals).mockReturnValue(
      new Promise<AgentTerminalApi[]>((resolve) => {
        settleListing = resolve;
      }),
    );
    vi.mocked(createAgentTerminal).mockResolvedValue(terminal('term-1'));

    render(<TerminalWorkspace jobId="job-1" active />);

    const button = await screen.findByRole('button', { name: 'New terminal' });
    expect(button).toBeEnabled();

    fireEvent.click(button);
    expect(
      await screen.findByRole('application', { name: 'Terminal 1 terminal' }),
    ).toBeInTheDocument();

    settleListing([]);
    await waitFor(() => expect(listAgentTerminals).toHaveBeenCalledTimes(1));
  });

  it('keeps a terminal created while the listing was still in flight', async () => {
    // The listing was issued before that terminal existed, so its answer is
    // legitimately missing it. Replacing state with the answer would delete a
    // terminal the user is looking at.
    let settleListing: (items: AgentTerminalApi[]) => void = () => undefined;
    vi.mocked(listAgentTerminals).mockReturnValue(
      new Promise<AgentTerminalApi[]>((resolve) => {
        settleListing = resolve;
      }),
    );
    vi.mocked(createAgentTerminal).mockResolvedValue(terminal('term-1'));

    render(<TerminalWorkspace jobId="job-1" active />);
    fireEvent.click(await screen.findByRole('button', { name: 'New terminal' }));
    expect(
      await screen.findByRole('application', { name: 'Terminal 1 terminal' }),
    ).toBeInTheDocument();

    settleListing([]);

    await waitFor(() => expect(listAgentTerminals).toHaveBeenCalledTimes(1));
    expect(screen.getByRole('application', { name: 'Terminal 1 terminal' })).toBeInTheDocument();
  });

  it('asks the sandbox manager for a size it measured rather than a fixed 24x80', async () => {
    vi.mocked(listAgentTerminals).mockResolvedValue([]);
    vi.mocked(createAgentTerminal).mockResolvedValue(terminal('term-1'));
    vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(1200);
    vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockReturnValue(600);

    render(<TerminalWorkspace jobId="job-1" active />);
    fireEvent.click(await screen.findByRole('button', { name: 'New terminal' }));

    await waitFor(() => expect(createAgentTerminal).toHaveBeenCalledTimes(1));
    const size = vi.mocked(createAgentTerminal).mock.calls[0][1];
    expect(size).toBeDefined();
    expect(size!.cols).toBeGreaterThan(80);
    expect(size!.rows).toBeGreaterThan(24);
  });

  it('cannot create a terminal before the workspace is ready', async () => {
    // Two waits were removed together and only one of them should have been.
    // The listing is not a precondition; workspace readiness is — there is no
    // container to open a PTY in until the job reports `workspace_ready`, and
    // a create issued before then fails at the sandbox manager. A new conversation
    // starts in exactly this state.
    vi.mocked(listAgentTerminals).mockResolvedValue([]);
    vi.mocked(createAgentTerminal).mockResolvedValue(terminal('term-1'));

    const view = render(<TerminalWorkspace jobId="job-1" active ready={false} />);

    const button = await screen.findByRole('button', { name: 'New terminal' });
    expect(button).toBeDisabled();

    fireEvent.click(button);
    await waitFor(() => expect(createAgentTerminal).not.toHaveBeenCalled());
    expect(listAgentTerminals).not.toHaveBeenCalled();

    // And it becomes usable the moment the workspace is.
    view.rerender(<TerminalWorkspace jobId="job-1" active ready />);
    await waitFor(() => expect(screen.getByRole('button', { name: 'New terminal' })).toBeEnabled());
  });

  it('keeps its terminals open across turns of the same chat', async () => {
    // Every turn is its own job, but they share the chat's one workspace.
    const first = terminal('term-1');
    vi.mocked(listAgentTerminals).mockResolvedValue([first]);
    const view = render(<TerminalWorkspace jobId="job-1" workspaceKey="thread-1" active />);
    await screen.findByRole('application', { name: 'Terminal 1 terminal' });
    await waitFor(() => expect(xtermHarness.instances).toHaveLength(1));

    // The next turn locks the workspace while the agent prepares it...
    view.rerender(<TerminalWorkspace jobId="job-2" workspaceKey="thread-1" active ready={false} />);
    expect(screen.getByRole('application', { name: 'Terminal 1 terminal' })).toBeInTheDocument();
    view.rerender(<TerminalWorkspace jobId="job-2" workspaceKey="thread-1" active ready />);

    // ...and the same screen carries on, addressed through the new turn.
    xtermHarness.instances[0].emitData('pwd\r');
    await waitFor(() =>
      expect(writeAgentTerminalInput).toHaveBeenCalledWith('job-2', first.id, 'pwd\r'),
    );
    expect(xtermHarness.instances).toHaveLength(1);
    expect(streamAgentTerminal).toHaveBeenCalledTimes(1);
    expect(listAgentTerminals).toHaveBeenCalledTimes(1);
  });

  it('closes a terminal whose shell exits cleanly', async () => {
    const first = terminal('term-1');
    const second = terminal('term-2', 'bash');
    vi.mocked(listAgentTerminals).mockResolvedValue([first, second]);
    vi.mocked(deleteAgentTerminal).mockResolvedValue();
    vi.mocked(streamAgentTerminal).mockImplementation(async (_job, terminalId, opts) => {
      if (terminalId === first.id) opts.onExit({ seq: 1, exit_code: 0 });
    });

    render(<TerminalWorkspace jobId="job-1" active />);

    await waitFor(() => expect(deleteAgentTerminal).toHaveBeenCalledWith('job-1', first.id));
    const tab = await screen.findByRole('tab', { name: 'Terminal 1 · bash' });
    expect(tab).toHaveAttribute('aria-selected', 'true');
    expect(screen.getAllByRole('tab')).toHaveLength(1);
  });

  it('keeps a failed shell readable but gives its slot back', async () => {
    const first = terminal('term-1');
    vi.mocked(listAgentTerminals).mockResolvedValue({ terminals: [first], max_sessions: 1 });
    vi.mocked(createAgentTerminal).mockResolvedValue(terminal('term-2', 'bash'));
    vi.mocked(streamAgentTerminal).mockImplementation(async (_job, terminalId, opts) => {
      if (terminalId === first.id) opts.onExit({ seq: 3, exit_code: 2 });
    });

    render(<TerminalWorkspace jobId="job-1" active />);

    expect(await screen.findByRole('tab', { name: 'Terminal 1 · exited' })).toBeInTheDocument();
    expect(xtermHarness.instances[0].write).toHaveBeenCalledWith(
      '\r\n\x1b[90m[process exited with code 2]\x1b[0m\r\n',
    );
    expect(deleteAgentTerminal).not.toHaveBeenCalled();

    // At the limit of one, the ended shell no longer blocks a new terminal,
    // and makes way for it the same way the sandbox manager does.
    const create = screen.getByRole('button', { name: 'New terminal' });
    expect(create).toBeEnabled();
    fireEvent.click(create);
    expect(await screen.findByRole('tab', { name: 'Terminal 1 · bash' })).toBeInTheDocument();
    expect(screen.queryByRole('tab', { name: /exited/ })).not.toBeInTheDocument();
  });

  it('keeps an ended shell read-only when the workspace unlocks again', async () => {
    vi.mocked(listAgentTerminals).mockResolvedValue([terminal('term-1')]);
    vi.mocked(streamAgentTerminal).mockImplementation(async (_job, _terminal, opts) => {
      opts.onExit({ seq: 1, exit_code: 1 });
    });

    const view = render(<TerminalWorkspace jobId="job-1" active />);
    await waitFor(() => expect(xtermHarness.instances).toHaveLength(1));
    const xterm = xtermHarness.instances[0];
    await waitFor(() => expect(xterm.options.disableStdin).toBe(true));

    view.rerender(<TerminalWorkspace jobId="job-1" active ready={false} />);
    view.rerender(<TerminalWorkspace jobId="job-1" active ready />);

    expect(xterm.options.disableStdin).toBe(true);
    xterm.emitData('ls\r');
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(writeAgentTerminalInput).not.toHaveBeenCalled();
  });

  it('does not take focus from the chat when the workspace unlocks', async () => {
    vi.mocked(listAgentTerminals).mockResolvedValue([terminal('term-1')]);
    const composer = document.createElement('textarea');
    document.body.appendChild(composer);
    try {
      const view = render(<TerminalWorkspace jobId="job-1" active />);
      await waitFor(() => expect(xtermHarness.instances).toHaveLength(1));
      const xterm = xtermHarness.instances[0];
      composer.focus();
      xterm.focus.mockClear();

      // A turn locks and then unlocks the workspace while someone types.
      view.rerender(<TerminalWorkspace jobId="job-1" active ready={false} />);
      view.rerender(<TerminalWorkspace jobId="job-1" active ready />);

      expect(xterm.focus).not.toHaveBeenCalled();
      expect(document.activeElement).toBe(composer);
    } finally {
      composer.remove();
    }
  });

  it('does not open the first terminal over text being typed elsewhere', async () => {
    vi.mocked(listAgentTerminals).mockResolvedValue([terminal('term-1')]);
    const composer = document.createElement('textarea');
    document.body.appendChild(composer);
    composer.focus();
    try {
      render(<TerminalWorkspace jobId="job-1" active />);
      await waitFor(() => expect(xtermHarness.instances).toHaveLength(1));
      expect(xtermHarness.instances[0].focus).not.toHaveBeenCalled();
      expect(document.activeElement).toBe(composer);
    } finally {
      composer.remove();
    }
  });

  it('focuses a terminal the owner opens', async () => {
    vi.mocked(listAgentTerminals).mockResolvedValue([]);
    vi.mocked(createAgentTerminal).mockResolvedValue(terminal('term-1'));

    render(<TerminalWorkspace jobId="job-1" active />);
    const button = await screen.findByRole('button', { name: 'New terminal' });
    button.focus();
    fireEvent.click(button);

    await waitFor(() => expect(xtermHarness.instances).toHaveLength(1));
    expect(xtermHarness.instances[0].focus).toHaveBeenCalled();
  });

  it("does not send xterm's answers to queries replayed from history", async () => {
    const first = terminal('term-1');
    vi.mocked(listAgentTerminals).mockResolvedValue([first]);
    let live: ((seq: number) => void) | undefined;
    vi.mocked(streamAgentTerminal).mockImplementation(async (_job, _terminal, opts) => {
      opts.onAttached?.({ seq: 2 });
      opts.onOutput({ seq: 1, data: btoa('\x1b[6n') });
      opts.onOutput({ seq: 2, data: btoa('$ ') });
      live = (seq) => opts.onOutput({ seq, data: btoa('\x1b[c') });
    });
    xtermHarness.heldWrites = [];

    render(<TerminalWorkspace jobId="job-1" active />);
    await waitFor(() => expect(live).toBeTypeOf('function'));
    const xterm = xtermHarness.instances[0];

    // While xterm parses the history, a reply it sends answers that history:
    // a cursor report for a program that asked before this screen existed.
    // Keys typed meanwhile still reach the shell.
    xterm.emitData('\x1b[24;1R');
    xterm.emitData('ls\r');
    await waitFor(() =>
      expect(writeAgentTerminalInput).toHaveBeenCalledWith('job-1', first.id, 'ls\r'),
    );
    const replayed = xtermHarness.heldWrites.splice(0);
    expect(replayed).toHaveLength(2);
    replayed.forEach((parsed) => parsed());
    xtermHarness.heldWrites = null;

    // Live output is answered as usual.
    live!(3);
    xterm.emitData('\x1b[?1;2c');
    await waitFor(() =>
      expect(writeAgentTerminalInput).toHaveBeenCalledWith('job-1', first.id, '\x1b[?1;2c'),
    );
    expect(writeAgentTerminalInput).toHaveBeenCalledTimes(2);
  });

  it('takes the history boundary from the listing when the sandbox manager does not name it', async () => {
    // A sandbox manager that predates the `attached` frame, with two events retained.
    const first = { ...terminal('term-1'), last_seq: 2 };
    vi.mocked(listAgentTerminals).mockResolvedValue([first]);
    let live: ((seq: number) => void) | undefined;
    vi.mocked(streamAgentTerminal).mockImplementation(async (_job, _terminal, opts) => {
      opts.onOutput({ seq: 1, data: btoa('\x1b[6n') });
      opts.onOutput({ seq: 2, data: btoa('$ ') });
      live = (seq) => opts.onOutput({ seq, data: btoa('\x1b[6n') });
    });
    xtermHarness.heldWrites = [];

    render(<TerminalWorkspace jobId="job-1" active />);
    await waitFor(() => expect(live).toBeTypeOf('function'));
    const xterm = xtermHarness.instances[0];

    xterm.emitData('\x1b[24;1R');
    const replayed = xtermHarness.heldWrites.splice(0);
    expect(replayed).toHaveLength(2);
    replayed.forEach((parsed) => parsed());
    xtermHarness.heldWrites = null;

    live!(3);
    xterm.emitData('\x1b[5;1R');
    await waitFor(() =>
      expect(writeAgentTerminalInput).toHaveBeenCalledWith('job-1', first.id, '\x1b[5;1R'),
    );
    expect(writeAgentTerminalInput).toHaveBeenCalledTimes(1);
  });

  it('answers queries that arrived while the stream was reconnecting', async () => {
    const first = terminal('term-1');
    vi.mocked(listAgentTerminals).mockResolvedValue([first]);
    let reconnect: (() => void) | undefined;
    vi.mocked(streamAgentTerminal).mockImplementation(async (_job, _terminal, opts) => {
      opts.onAttached?.({ seq: 1 });
      opts.onOutput({ seq: 1, data: btoa('$ ') });
      // After a dropped connection the sandbox manager announces its newest event
      // again; what was produced meanwhile is late, not history.
      reconnect = () => {
        opts.onAttached?.({ seq: 3 });
        opts.onOutput({ seq: 2, data: btoa('\x1b[6n') });
        opts.onOutput({ seq: 3, data: btoa('?') });
      };
    });

    render(<TerminalWorkspace jobId="job-1" active />);
    await waitFor(() => expect(reconnect).toBeTypeOf('function'));
    const xterm = xtermHarness.instances[0];
    xtermHarness.heldWrites = [];
    reconnect!();

    // Nothing from the reconnect waits on being parsed as history...
    expect(xtermHarness.heldWrites).toHaveLength(0);
    xtermHarness.heldWrites = null;
    // ...so the program still waiting on its cursor report receives it.
    xterm.emitData('\x1b[5;1R');
    await waitFor(() =>
      expect(writeAgentTerminalInput).toHaveBeenCalledWith('job-1', first.id, '\x1b[5;1R'),
    );
  });

  it('sends binary input, such as an old-style mouse report, as bytes', async () => {
    const first = terminal('term-1');
    vi.mocked(listAgentTerminals).mockResolvedValue([first]);

    render(<TerminalWorkspace jobId="job-1" active />);
    await waitFor(() => expect(xtermHarness.instances).toHaveLength(1));
    xtermHarness.instances[0].emitData('a');
    xtermHarness.instances[0].emitBinary('\x1b[M \xa1\xbb');

    await waitFor(() => expect(writeAgentTerminalInput).toHaveBeenCalledTimes(2));
    expect(writeAgentTerminalInput).toHaveBeenNthCalledWith(1, 'job-1', first.id, 'a');
    const bytes = vi.mocked(writeAgentTerminalInput).mock.calls[1][2] as Uint8Array;
    expect(Array.from(bytes)).toEqual([0x1b, 0x5b, 0x4d, 0x20, 0xa1, 0xbb]);
  });

  it('sends each new size once, inside the bounds the API accepts', async () => {
    const first = terminal('term-1');
    vi.mocked(listAgentTerminals).mockResolvedValue([first]);
    let resizePane: (() => void) | undefined;
    vi.stubGlobal(
      'ResizeObserver',
      class {
        constructor(private readonly callback: ResizeObserverCallback) {}
        observe(target: Element) {
          if (target.getAttribute('role') !== 'tablist') {
            resizePane = () => this.callback([], this as unknown as ResizeObserver);
          }
        }
        disconnect() {}
        unobserve() {}
      },
    );

    render(<TerminalWorkspace jobId="job-1" active />);
    await waitFor(() =>
      expect(resizeAgentTerminal).toHaveBeenCalledWith('job-1', first.id, 24, 80),
    );
    await waitFor(() => expect(resizePane).toBeTypeOf('function'));

    resizePane!();
    await new Promise((resolve) => setTimeout(resolve, 120));
    expect(resizeAgentTerminal).toHaveBeenCalledTimes(1);

    const xterm = xtermHarness.instances[0];
    xterm.rows = 1;
    xterm.cols = 9;
    resizePane!();
    await waitFor(() =>
      expect(resizeAgentTerminal).toHaveBeenLastCalledWith('job-1', first.id, 2, 20),
    );
  });
});
