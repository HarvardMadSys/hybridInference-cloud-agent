/**
 * One shared, memoised load of the xterm runtime.
 *
 * The terminal libraries are ~330 KB of source (81 KB gzipped in the built
 * bundle) and used to be imported when a pane mounted — which is *after* the
 * create request returns, so the fetch sat on the click-to-prompt path rather
 * than beside it. The panel knows it may need them as soon as it becomes
 * visible, so it starts the load then and this promise is what the pane later
 * awaits.
 *
 * Memoised rather than left to the bundler's module cache: sharing the promise
 * is what makes "the pane reuses the prefetch" a property of this module
 * instead of an assumption about webpack.
 */
type TerminalRuntime = [typeof import('@xterm/xterm'), typeof import('@xterm/addon-fit')];

let pending: Promise<TerminalRuntime> | null = null;

/** Begin (or join) the load of the xterm runtime. */
export function loadTerminalRuntime(): Promise<TerminalRuntime> {
  pending ??= Promise.all([import('@xterm/xterm'), import('@xterm/addon-fit')]).catch(
    (cause: unknown) => {
      // A transient chunk-download failure must not poison every later pane.
      pending = null;
      throw cause;
    },
  );
  return pending;
}
