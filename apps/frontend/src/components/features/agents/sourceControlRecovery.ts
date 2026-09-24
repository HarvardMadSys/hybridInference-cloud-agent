/**
 * How often a page that says GitHub is temporarily unavailable looks again.
 *
 * The control plane retries loading its GitHub App at this interval (issue
 * #47), so asking more often cannot find it back any sooner. Kept out of
 * `@/lib/api/agents` so that a test mocking that module cannot turn it into
 * `undefined` — which `setInterval` reads as zero.
 */
export const SOURCE_CONTROL_RECHECK_MS = 30_000;
