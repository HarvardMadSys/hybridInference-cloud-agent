/**
 * Which answers about one connection the page may apply as they arrive.
 *
 * An edit, a disable and a delete can run on the same connection at once, and
 * their answers can come back in any order: an edit committed first but
 * answered last would put the card back to its state before the disable. So
 * an answer is applied only when no other operation on that connection
 * started or was still running while it ran. Otherwise nothing is applied,
 * and once the last of them settles the page re-reads what the server holds.
 */
export interface OperationTicket {
  id: string;
  epoch: number;
}

/** Runs one operation on a connection; its answer, or `undefined` when another overlapped. */
export type Operate = <T>(id: string, action: () => Promise<T>) => Promise<T | undefined>;

/** What to do with one settled operation's answer. */
export type Settlement =
  /** Nothing else touched the connection meanwhile: apply the answer. */
  | 'apply'
  /** Others overlapped and some still run: apply nothing yet. */
  | 'wait'
  /** Others overlapped and this was the last: re-read the server's list. */
  | 'refresh';

interface State {
  epoch: number;
  running: number;
  overlapped: boolean;
}

export class ConnectionOps {
  private readonly states = new Map<string, State>();

  begin(id: string): OperationTicket {
    const state = this.states.get(id) ?? { epoch: 0, running: 0, overlapped: false };
    if (state.running > 0) state.overlapped = true;
    state.epoch += 1;
    state.running += 1;
    this.states.set(id, state);
    return { id, epoch: state.epoch };
  }

  settle(ticket: OperationTicket): Settlement {
    const state = this.states.get(ticket.id);
    if (!state) return 'refresh';
    state.running -= 1;
    const alone = !state.overlapped && state.epoch === ticket.epoch;
    if (state.running > 0) return alone ? 'apply' : 'wait';
    this.states.delete(ticket.id);
    return alone ? 'apply' : 'refresh';
  }
}
