import type { AgentEvent } from './types';

type ToolUseEvent = Extract<AgentEvent, { kind: 'tool_use' }>;
const SUMMARY_LIMIT = 180;

function record(value: unknown): Record<string, unknown> {
  if (typeof value === 'string') {
    try {
      value = JSON.parse(value);
    } catch {
      return {};
    }
  }
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function text(...values: unknown[]): string | undefined {
  return values.find((value): value is string => typeof value === 'string' && !!value.trim());
}

function compact(value: string): string {
  const line = value.replace(/\s+/g, ' ').trim();
  return line.length > SUMMARY_LIMIT ? `${line.slice(0, SUMMARY_LIMIT - 1).trimEnd()}…` : line;
}

/** Describe recorded arguments only; a tool name or command never implies success. */
export function toolSummary(event: ToolUseEvent): string {
  const input = record(event.input);
  const key = event.tool.split(/\.|__/).at(-1)?.toLowerCase() ?? '';
  let subject: string | undefined;

  if (
    ['bash', 'shell', 'exec_command', 'shell_command', 'execute_command', 'run_command'].includes(
      key,
    )
  ) {
    subject = text(input.command, input.cmd, input.description);
  } else if (
    [
      'read',
      'read_file',
      'readfile',
      'edit',
      'edit_file',
      'editfile',
      'multiedit',
      'write',
      'write_file',
      'writefile',
    ].includes(key)
  ) {
    subject = text(input.file_path, input.path, input.filePath);
  } else if (
    ['search', 'grep', 'glob', 'ripgrep', 'search_files', 'text_search', 'find_files'].includes(key)
  ) {
    const query = text(input.query, input.pattern, input.glob);
    const path = text(input.path, input.directory);
    subject = query && path ? `${query} in ${path}` : (query ?? path);
  }

  subject ??= text(input.description, event.detail);
  const tool = text(event.tool) ?? 'Tool';
  return compact(subject ? `${tool}: ${subject}` : tool);
}

/** Keep unresolved, interrupted, and failed calls visible when completed activity folds. */
export function toolNeedsAttention(event: ToolUseEvent): boolean {
  if (event.outputIsError) return true;
  if (
    event.resultState &&
    ['failed', 'cancelled', 'unknown', 'not_recorded', 'waiting'].includes(event.resultState)
  ) {
    return true;
  }
  // An empty recorded output still constitutes a result. Explicit received state
  // also records completion when a runtime omits the result body.
  return event.resultState !== 'received' && event.output === undefined;
}
