import type { AgentJob } from '../types';

/** All tabs watching one conversation boundary share the server's submission receipt. */
export function watchPullRequestRequestId(threadId: string, latestJobId: string): string {
  return `watch-pr:v2:${threadId}:${latestJobId}`;
}

/**
 * A follow-up action uses the pull request the agent opened, never an inferred
 * branch URL. The sandbox's `git` and `gh` act on GitHub as the conversation's
 * owner through the platform's relay, which refuses GraphQL, so every GitHub
 * read and write the prompt names is a REST call.
 */
export function watchPullRequestPrompt(job: AgentJob): string {
  const api = `repos/${job.repo}`;
  return [
    `Watch the pull request ${job.prUrl ?? job.workspacePrUrl} in ${job.repo} until its tests pass and required reviews are approved, then merge it. This workspace's local branch is ${job.branch}.`,
    `In this workspace, \`git\` (through \`origin\`) and \`gh\` act on GitHub as me. \`gh\` subcommands that use GraphQL, such as \`gh pr view\`, \`gh pr checks\` and \`gh pr merge\`, are refused; use the REST API through \`gh api\`. With N as the PR number from its URL: \`gh api ${api}/pulls/N\` for the PR, its head branch, head SHA and mergeability; \`gh api ${api}/commits/<head sha>/check-runs\` and \`gh api ${api}/commits/<head sha>/status\` for checks; \`gh api ${api}/pulls/N/reviews\` for reviews; \`gh api ${api}/pulls/N/comments\` and \`gh api ${api}/issues/N/comments\` for review threads and comments. If GitHub refuses access, for example because I have not connected my GitHub account or authorized this repository, explain what I need to connect or authorize and stop; do not look for other credentials.`,
    `Verify that the PR belongs to ${job.repo} and read its head branch from the PR. If it is already merged, report that and stop; if it is closed without merging, report that and do not reopen it. If it is a draft, mark it ready for review so checks and reviews can run. GitHub offers that only through GraphQL, so if you cannot mark it ready through REST, tell me to mark it ready; never merge a draft.`,
    "Monitor checks and review feedback using bounded waits. If actionable test failures or review feedback need code changes, make them in this workspace, run the relevant validation, commit, and push the commits to the PR's head branch, for example `git push origin HEAD:<head branch>`; do not force-push. Then keep watching the new head. Do not treat local test results, or checks and approvals for an older head, as sufficient.",
    `Before merging, re-read the latest PR head, checks, reviews, review comments and mergeability. Merge only when all required tests and checks pass on the current head, required reviews approve it, and no blocking change requests or actionable unresolved review comments remain. Respect branch protection and the repository’s merge policy; never bypass them or force a merge. Merge with \`gh api -X PUT ${api}/pulls/N/merge -f sha=<head sha>\`, adding \`-f merge_method=squash\` or \`rebase\` when the repository requires it, so GitHub refuses the merge if the head has moved.`,
    'Keep routine unchanged progress quiet. Report when the PR merges, a blocker needs my attention, or this turn cannot keep watching; do not claim a background watch continues after the turn ends.',
  ].join('\n\n');
}
