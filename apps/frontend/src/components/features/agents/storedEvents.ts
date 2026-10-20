// One reading of a stored event's payload, and one rule for pairing a tool
// call with its result. The transcript, the trajectory rows and steps, and the
// turn footer's timings all read the stream through this module, so a call one
// view pairs is never left unpaired by another.

/** The object a payload field holds, or an empty one for anything else. */
export function record(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

/** A payload field as text: numbers and booleans spelled out, other values as JSON. */
export function text(value: unknown): string {
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  if (value === null || value === undefined) return '';
  try {
    return JSON.stringify(value);
  } catch {
    return '';
  }
}

/** Whether the platform cut this payload short before storing it. */
export function isTruncated(payload: Record<string, unknown>): boolean {
  return payload.truncated === true || typeof payload.truncation_notice === 'string';
}

/** Where an event stands in the pairing: the key of the call it makes or answers. */
export interface ToolKeys {
  call?: string;
  result?: string;
}

/** Calls and results by key, read under the pairing rule. */
export interface ToolPairs<Event> {
  /** Every result recorded for the key, in stream order. */
  resultsOf(key: string): Event[];
  /** How many calls carry the key. */
  callsOf(key: string): number;
  /** More than one call or more than one result carries the key. */
  ambiguous(key: string): boolean;
  /** The one result answering the key's one call, if the pairing is certain. */
  resultOf(key: string): Event | undefined;
}

/**
 * Index tool calls and results by key.
 *
 * A key is a tool id within one attempt: a retried attempt repeats its ids, so
 * a bare id would pair a call with another attempt's answer. `keysOf` returns
 * the key an event calls or answers, and nothing for an event that is neither.
 *
 * **One rule for a repeated id.** A key pairs only when exactly one call and
 * one result carry it. A second call or a second result makes which answer
 * belongs to which call unknowable, so the key is `ambiguous`, `resultOf`
 * returns nothing, and a view reports neither a status nor a duration it
 * cannot prove. Every record is still there to read.
 */
export function pairToolCalls<Event>(
  events: Iterable<Event>,
  keysOf: (event: Event) => ToolKeys,
): ToolPairs<Event> {
  const calls = new Map<string, number>();
  const results = new Map<string, Event[]>();
  for (const event of events) {
    const { call, result } = keysOf(event);
    if (call) calls.set(call, (calls.get(call) ?? 0) + 1);
    if (result) {
      const answers = results.get(result);
      if (answers) answers.push(event);
      else results.set(result, [event]);
    }
  }
  const pairs: ToolPairs<Event> = {
    resultsOf: (key) => results.get(key) ?? [],
    callsOf: (key) => calls.get(key) ?? 0,
    ambiguous: (key) => pairs.callsOf(key) > 1 || pairs.resultsOf(key).length > 1,
    resultOf: (key) => (pairs.ambiguous(key) ? undefined : pairs.resultsOf(key)[0]),
  };
  return pairs;
}
