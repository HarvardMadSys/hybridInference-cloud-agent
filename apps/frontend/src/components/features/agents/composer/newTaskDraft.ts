import type { AgentPluginInputApi } from '@/lib/api/agents';
import { readDraftPlugins } from '@/lib/pluginConfiguration';

/** What New task keeps of an unsent task between visits. */
export interface NewTaskDraft {
  task: string;
  repo: string;
  runtime: string;
  /**
   * The model the owner picked in this composer, or empty. Never the model
   * shown: one the server filled in is resolved again on the next visit.
   * Drafts written before this field have only `model`, which mixed the two
   * and so is ignored.
   */
  chosenModel: string;
  /**
   * The owner connection `chosenModel` was picked from, or empty: what every
   * draft written before sources existed held, and a pick that cannot run.
   */
  chosenModelConnectionId: string;
  effort: string;
  branch: string;
  plugins: AgentPluginInputApi[];
}

export function readNewTaskDraft(value: string): NewTaskDraft | null {
  try {
    const saved: unknown = JSON.parse(value);
    if (!saved || typeof saved !== 'object') return null;
    const record = saved as Record<string, unknown>;
    const fields = ['task', 'repo', 'runtime', 'effort', 'branch'] as const;
    if (!fields.every((field) => typeof record[field] === 'string')) return null;
    const plugins = readDraftPlugins(record.plugins);
    if (plugins === null) return null;
    const chosenModel = typeof record.chosenModel === 'string' ? record.chosenModel : '';
    const chosenModelConnectionId =
      chosenModel && typeof record.chosenModelConnectionId === 'string'
        ? record.chosenModelConnectionId
        : '';
    return { ...(saved as NewTaskDraft), chosenModel, chosenModelConnectionId, plugins };
  } catch {
    return null;
  }
}
