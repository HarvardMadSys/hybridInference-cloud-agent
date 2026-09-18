// Fallbacks for older control planes; /config advertises the server bounds.
// They are the same numbers ``cloud_agent/goals/models.py`` enforces, and a
// client that guessed lower would refuse a goal the server would have taken.
export const DEFAULT_GOAL_LIMITS = {
  max_objective_chars: 4000,
  max_rounds: 50,
  default_rounds: 10,
};
export type GoalLimits = typeof DEFAULT_GOAL_LIMITS;
