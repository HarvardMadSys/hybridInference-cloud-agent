// Fallbacks for older control planes; /config advertises the server bounds.
export const DEFAULT_SKILL_LIMITS = {
  max_selected: 16,
  max_selection_bytes: 128 * 1024,
  max_skill_bytes: 64 * 1024,
  max_name_length: 80,
  max_description_length: 1024,
};
export type SkillLimits = typeof DEFAULT_SKILL_LIMITS;
