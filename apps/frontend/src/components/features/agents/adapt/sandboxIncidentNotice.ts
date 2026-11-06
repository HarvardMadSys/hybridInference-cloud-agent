// The one line a turn shows when something went wrong in its sandbox.
//
// The control plane appends a `lifecycle` event, phase `sandbox_incident`, to
// the running turns an incident concerns (docs/agents/specs/2026-11-04-sandbox-incidents.md).
// It says what happened in plain words: no limit, no exit code, no advice.

const LINES: Record<string, (alias: string, crashed: boolean) => string> = {
  out_of_memory: () => 'The environment ran out of memory, and some processes were stopped.',
  container_exited: () =>
    'The environment stopped unexpectedly. It starts again with your next message.',
  mcp_server_failed: (alias, crashed) =>
    crashed
      ? `${alias} stopped unexpectedly. It starts again when it is next used.`
      : `${alias} could not start, so its tools were not available.`,
};

/** The line for one incident; a kind this release does not know reads generically. */
export function sandboxIncidentMessage(payload: Record<string, unknown>): string {
  const kind = typeof payload.incident === 'string' ? payload.incident : '';
  const alias =
    typeof payload.alias === 'string' && payload.alias ? payload.alias : 'A tool server';
  const line = Object.hasOwn(LINES, kind) ? LINES[kind] : undefined;
  return line
    ? line(alias, payload.server_phase === 'crashed')
    : 'Something went wrong in the environment.';
}
