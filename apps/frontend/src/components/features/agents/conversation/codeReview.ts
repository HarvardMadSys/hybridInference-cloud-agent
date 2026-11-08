import type { AgentJobApi } from '@/lib/api/agents';
import type { AgentJob } from '../types';

export const codeReviewFixPrompt = [
  'Fix the actionable issues you found in your completed code review above. This follow-up authorizes you to modify repository files and apply fixes, replacing the previous read-only review instruction.',
  'The shared workspace may have changed since your review. Confirm each finding still applies, make focused fixes for the confirmed issues, and add or update regression tests where appropriate. Run the relevant checks and summarize what you fixed, the validation results, and any remaining issues. If there are no actionable findings, say so without making unnecessary changes.',
  'Work in the current shared workspace using local files and Git. Do not fetch the pull request from GitHub: the workspace already holds the reviewed changes.',
].join('\n\n');

export function codeReviewPrompt(job: AgentJob, sourceJobs: AgentJobApi[]): string {
  // A PR review covers every turn, so it compares against the conversation's
  // first base, not a later turn's.
  const original = sourceJobs
    .filter((turn) => turn.thread_id === job.threadId)
    .reduce<AgentJobApi | undefined>((first, turn) => {
      if (!first || (turn.turn_no ?? 1) < (first.turn_no ?? 1)) return turn;
      return first;
    }, undefined);
  const baseSha = original?.base_sha || job.baseSha;
  const baseRef = original?.base_ref || job.baseRef;
  // The workspace already holds every reviewed change; fetching the PR from
  // GitHub would only review an older or different copy of it.
  return [
    `You are an independent code-review subagent for ${job.repo}. Review the code changes for ${job.prUrl ?? job.workspacePrUrl}.`,
    `Compare the current shared workspace against the original source base ${baseSha}${baseRef ? ` (${baseRef})` : ''}. The source branch is ${job.branch}. Include branch commits, staged and unstaged changes, and relevant untracked files.`,
    `Review with local Git. Do not fetch the pull request from GitHub: the workspace already holds every change it contains. For example, use \`git diff ${baseSha}\` for committed, staged, and unstaged changes, \`git log ${baseSha}..HEAD\` for branch commits, and \`git status\` for untracked files.`,
    'Focus on actionable bugs introduced by these changes: correctness, regressions, security, and missing validation. Inspect surrounding code and tests to confirm findings. Report each finding with severity, file path and line, its impact, and a concrete explanation. If there are no findings, say so and mention any material validation gaps.',
    'Keep this review read-only. Do not modify repository files, apply fixes, commit, push, or create or update pull requests. Return the review findings in this chat.',
  ].join('\n\n');
}
