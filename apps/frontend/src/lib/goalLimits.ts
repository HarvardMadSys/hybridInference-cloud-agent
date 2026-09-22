// Fallbacks for older control planes; /config advertises the server bounds.
//
// Only the objective is bounded, and that is the whole list. There is no round
// ceiling and no round default to fall back to, because the server has neither:
// a goal runs until it is finished, stopped, cleared, blocked on its owner, or
// one of the run-time limits that already governs every turn applies. A client
// that wants a bound states one; nothing here may invent one.
export const DEFAULT_GOAL_LIMITS = {
  max_objective_chars: 4000,
};
export type GoalLimits = typeof DEFAULT_GOAL_LIMITS;
