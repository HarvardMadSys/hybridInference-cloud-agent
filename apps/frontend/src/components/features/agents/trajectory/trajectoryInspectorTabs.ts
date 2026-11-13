import type { TrajectoryStep } from './trajectorySteps';

export type AssistantTab = 'summary' | 'preview' | 'raw';

export const ASSISTANT_TABS: Array<{ key: AssistantTab; label: string }> = [
  { key: 'summary', label: 'Summary' },
  { key: 'preview', label: 'Preview' },
  { key: 'raw', label: 'Raw' },
];

export type ToolTab = 'summary' | 'payload' | 'result' | 'schema' | 'timing';

export const TOOL_TABS: Array<{ key: ToolTab; label: string }> = [
  { key: 'summary', label: 'Summary' },
  { key: 'payload', label: 'Payload' },
  { key: 'result', label: 'Result' },
  { key: 'schema', label: 'Schema' },
  { key: 'timing', label: 'Timing' },
];

export type HarnessTab = 'systemPrompt' | 'tools';

export const HARNESS_TABS: Array<{ key: HarnessTab; label: string }> = [
  { key: 'systemPrompt', label: 'System Prompt' },
  { key: 'tools', label: 'Tools' },
];

export type RunTab = 'overview' | 'usage' | 'raw';

export const RUN_TABS: Array<{ key: RunTab; label: string }> = [
  { key: 'overview', label: 'Overview' },
  { key: 'usage', label: 'Usage' },
  { key: 'raw', label: 'Raw' },
];

export type EventTab = 'summary' | 'raw';
export type StepTab = AssistantTab | ToolTab | EventTab | HarnessTab;

export type InspectorSelection =
  | { kind: 'step'; stepKey: string; tab: StepTab }
  /** `turnNo` comes from whatever opened the panel, so a turn stays itself
   *  even when the conversation's rows have not been read yet. */
  | { kind: 'run'; jobId: string; turnNo: number; tab: RunTab };

const EVENT_TABS: Array<{ key: EventTab; label: string }> = [
  { key: 'summary', label: 'Summary' },
  { key: 'raw', label: 'Raw' },
];

/**
 * The tabs a step actually has.
 *
 * A step takes a tab only when it holds something to put in it. An event whose
 * runtime reported a harness is that report and nothing else — the two tabs
 * that read it, with no Summary or Raw around them — and an event that
 * reported none keeps Summary and Raw rather than opening views that would
 * have to explain their own emptiness.
 */
export function tabsForStep(step: TrajectoryStep): Array<{ key: StepTab; label: string }> {
  if (step.kind === 'assistant') return ASSISTANT_TABS;
  if (step.kind === 'tool') return TOOL_TABS;
  return step.harness ? HARNESS_TABS : EVENT_TABS;
}

/**
 * Whether a tab belongs to the newly selected step, or must fall back.
 *
 * The fallback is the step's own first tab rather than a fixed name: a step
 * whose views do not include Summary must not silently land on one that is not
 * offered, or the strip and the panel would disagree about what is open.
 */
export function normalizeStepTab(step: TrajectoryStep, tab: StepTab): StepTab {
  const tabs = tabsForStep(step);
  return tabs.some((item) => item.key === tab) ? tab : tabs[0].key;
}
