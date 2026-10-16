// The control plane's default when a stored renewal policy predates the field:
// two hours before its lease ends, a Host starts moving its sandboxes to other
// Hosts one at a time through their checkpoints.
export const DEFAULT_MOVE_OUT_BEFORE_SECONDS = 7200;
