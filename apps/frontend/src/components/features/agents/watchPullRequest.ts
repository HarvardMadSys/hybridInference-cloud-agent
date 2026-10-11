import type { AgentJob } from './types';

/** All tabs watching one conversation boundary share the server's submission receipt. */
export function watchPullRequestRequestId(threadId: string, latestJobId: string): string {
  return `watch-pr:v2:${threadId}:${latestJobId}`;
}

/** A follow-up action uses the published PR, never an inferred branch URL. */
export function watchPullRequestPrompt(job: AgentJob): string {
  return [
    `Watch the pull request ${job.prUrl} in ${job.repo} (source branch ${job.branch}) until its tests pass and required reviews are approved, then merge it.`,
    'First inspect the enabled source-control tools for this conversation. You need access to read the PR, checks, reviews and review threads, and to mark the draft ready and merge. If any required access is missing, explain what I need to connect or authorize and stop. This sandbox has no Git remote, GitHub CLI or GitHub credentials; use the enabled tools rather than trying to find credentials.',
    'Verify that the PR belongs to the repository and source branch above. If it is already merged, report that and stop; if it is closed without merging, report that and do not reopen it. Mark an open draft ready for review when the tools permit it so checks and reviews can run.',
    'Monitor checks and review feedback using bounded waits. If actionable test failures or review feedback need code changes, address them in this workspace and run the relevant validation, then end this turn so the platform can publish the changes. Explain that I can click Watch PR and merge again after publication to resume watching. Changes from an active turn are only published after it ends. Do not wait for your own changes to publish within this turn, and do not merge while they are unpublished. Do not treat local test results or checks and approvals for an older head as sufficient.',
    'Before merging, re-read the latest PR head, checks, reviews, unresolved review threads and mergeability. Merge only when all required tests and checks pass, required reviews approve the current changes, and no blocking change requests or actionable unresolved review threads remain. Respect branch protection and the repository’s merge policy; never bypass them or force a merge. Use the expected head SHA when the merge tool supports it.',
    'Keep routine unchanged progress quiet. Report when the PR merges, a blocker needs my attention, or this turn cannot keep watching; do not claim a background watch continues after the turn ends.',
  ].join('\n\n');
}
