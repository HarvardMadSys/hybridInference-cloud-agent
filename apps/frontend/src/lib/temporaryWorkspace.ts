// A temporary workspace is a conversation without a repository: the API says
// `repo: null`. Its sandbox starts empty, and nothing it does is published.
//
// Inside the app it is grouped, keyed, remembered and chosen like a project,
// under this one reserved key. A repository name always contains a slash and
// never starts with a colon, so the key cannot collide with one. It never
// leaves the app: every request translates it back with `apiRepo`.

export const TEMPORARY_WORKSPACE = ':temporary';

/** Whether a repository value — from the API or a UI key — means no repository. */
export function isTemporaryWorkspace(repo: string | null | undefined): boolean {
  return repo === null || repo === TEMPORARY_WORKSPACE;
}

/** The UI key for a conversation's repository: the reserved key when it has none. */
export function projectKey(repo: string | null): string {
  return repo ?? TEMPORARY_WORKSPACE;
}

/** Back to the API's spelling: `null` for a temporary workspace. */
export function apiRepo(key: string): string | null {
  return key === TEMPORARY_WORKSPACE ? null : key;
}
