// Project recorded Codex item identities and recognize runtime-only notices.
// Callers must retain the original records for Raw/export.
import type { StoredEvent } from './trajectorySteps';

export function record(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function rawRecord(event: StoredEvent): Record<string, unknown> | null {
  if (event.event_type !== 'raw') return null;
  const payload = record(event.payload);
  let value = record(payload.notification);
  if (payload.protocol !== 'codex-app-server' || !value.method) {
    const text = payload.line ?? payload.text;
    if (typeof text !== 'string') return null;
    try {
      value = record(JSON.parse(text));
    } catch {
      return null;
    }
  }
  return value;
}

export function rawNotification(event: StoredEvent): Record<string, unknown> | null {
  const value = rawRecord(event);
  return value && typeof value.method === 'string' && value.method && value.id === undefined
    ? value
    : null;
}

/** Claude retains its first counter as a raw diagnostic, including in old runs. */
export function isRuntimeTelemetry(event: StoredEvent): boolean {
  const value = rawRecord(event);
  if (value?.type !== 'system' || value.subtype !== 'thinking_tokens') return false;
  // An explicit failure must remain visible even on an otherwise known counter.
  return ![record(event.payload), value].some(
    (payload) =>
      Boolean(payload.error) ||
      payload.is_error === true ||
      payload.status === 'failed' ||
      payload.status === 'error',
  );
}

export function notificationHasError(notification: Record<string, unknown>): boolean {
  const params = record(notification.params);
  const item = record(params.item);
  const status = record(params.status).type ?? params.status ?? item.status;
  return Boolean(
    notification.method === 'error' ||
    notification.error ||
    params.error ||
    item.error ||
    params.is_error === true ||
    status === 'failed' ||
    status === 'error',
  );
}

const PROGRESS_METHODS = new Set([
  'thread/started',
  'thread/status/changed',
  'thread/tokenUsage/updated',
  'turn/started',
  'remoteControl/status/changed',
  'account/rateLimits/updated',
  'item/agentMessage/delta',
  'item/reasoning/summaryTextDelta',
  'item/reasoning/summaryPartAdded',
  'item/reasoning/textDelta',
  'item/commandExecution/outputDelta',
  'item/mcpToolCall/progress',
]);

export function isRuntimeNotification(notification: Record<string, unknown>): boolean {
  if (notificationHasError(notification)) return false;
  const item = record(record(notification.params).item);
  if (PROGRESS_METHODS.has(String(notification.method))) return true;
  return (
    (notification.method === 'item/started' || notification.method === 'item/updated') &&
    ['agentMessage', 'reasoning', 'commandExecution', 'mcpToolCall'].includes(String(item.type))
  );
}

/** A piece of an answer, reported while the model was still writing it. */
export interface ReplyPreviewPiece {
  previewId: string;
  text: string;
  /** The text replaces what was shown so far instead of extending it. */
  reset: boolean;
}

/**
 * Read a stored answer preview. Every harness's host batches the answer text
 * into these `raw` records; the complete `message` with the same `preview_id`
 * replaces them.
 */
export function replyPreviewPiece(event: StoredEvent): ReplyPreviewPiece | null {
  if (event.event_type !== 'raw') return null;
  const payload = record(event.payload);
  if (
    payload.protocol !== 'reply-preview' ||
    typeof payload.preview_id !== 'string' ||
    typeof payload.delta !== 'string'
  ) {
    return null;
  }
  return { previewId: payload.preview_id, text: payload.delta, reset: payload.reset === true };
}

function scope(event: StoredEvent, id: string): string {
  return `${'job_id' in event ? event.job_id : ''}:${event.attempt_id}:${id}`;
}

/** Old normalized replies omitted item IDs. Do not guess their stream owner. */
export function projectProtocolEvents(events: StoredEvent[]): StoredEvent[] {
  const identified = new Set(
    events.flatMap((event) => {
      const id = record(event.payload).native_item_id;
      return typeof id === 'string' ? [scope(event, id)] : [];
    }),
  );
  return events.map((event) => {
    const piece = replyPreviewPiece(event);
    if (piece) {
      return {
        ...event,
        event_type: 'message',
        payload: {
          text: piece.text,
          preview_id: piece.previewId,
          stream_state: piece.reset ? 'reset' : 'delta',
        },
      };
    }
    const notification = rawNotification(event);
    if (!notification || notificationHasError(notification)) return event;
    const payload = record(event.payload);
    const params = record(notification.params);
    const item = record(params.item);
    const method = notification.method;
    const id =
      typeof item.id === 'string'
        ? item.id
        : typeof params.itemId === 'string'
          ? params.itemId
          : '';
    if (!id) return event;
    let type = '';
    let text = '';
    let state = '';
    if (
      item.type === 'userMessage' &&
      ['item/started', 'item/updated', 'item/completed'].includes(String(method))
    ) {
      type = 'input';
      const content = Array.isArray(item.content) ? item.content : [];
      text = content
        .map((block) => {
          const value = record(block);
          return value.type === 'text' && typeof value.text === 'string' ? value.text : '';
        })
        .filter(Boolean)
        .join('\n');
      state = 'snapshot';
    } else if (method === 'item/commandExecution/outputDelta' && typeof params.delta === 'string') {
      return {
        ...event,
        event_type: 'tool_output',
        payload: { ...payload, tool_use_id: id, text: params.delta },
      };
    } else if (payload.protocol === 'codex-app-server' || identified.has(scope(event, id))) {
      if (
        (method === 'item/started' || method === 'item/updated') &&
        ['agentMessage', 'reasoning'].includes(String(item.type))
      ) {
        type = item.type === 'reasoning' ? 'thinking' : 'message';
        text = typeof item.text === 'string' ? item.text : '';
        state = method === 'item/started' ? 'started' : 'snapshot';
      } else if (method === 'item/agentMessage/delta' && typeof params.delta === 'string') {
        type = 'message';
        text = params.delta;
        state = 'delta';
      } else if (
        ['item/reasoning/summaryTextDelta', 'item/reasoning/textDelta'].includes(String(method)) &&
        typeof params.delta === 'string'
      ) {
        type = 'thinking';
        text = params.delta;
        state = 'delta';
      }
    }
    if (!type) return event;
    return {
      ...event,
      event_type: type,
      payload: {
        ...payload,
        text,
        native_item_id: id,
        stream_state: state,
        // Reasoning summary and full text are separate native content channels.
        stream_channel: method === 'item/reasoning/textDelta' ? 'reasoning' : 'summary',
      },
    };
  });
}
