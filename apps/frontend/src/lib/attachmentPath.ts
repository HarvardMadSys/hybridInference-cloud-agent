// What may be written under /uploads.
//
// The same predicate the control plane applies, stated here so a rejected path
// is reported next to the file that produced it rather than as a 400 after the
// bytes have already travelled. The browser check is a courtesy; the server's
// is the boundary.
//
// The rules match `_repository_content_endpoint` in `cloud_agent/github_app.py`
// — a path arriving from a directory upload is no more trusted than one
// arriving from a repository.

/** A drive letter prefix (`C:/…`), which is not a relative path anywhere. */
const DRIVE_PREFIX = /^[A-Za-z]:[\\/]/;

/** Control characters, including the line breaks that would split one file
 *  reference into several lines in the prompt listing. */
const CONTROL_CHARACTER = /[\u0000-\u001f\u007f]/;

export function isSafeUploadPath(path: string, maxLength: number): boolean {
  if (!path || path.length > maxLength) return false;
  if (path.startsWith('/') || DRIVE_PREFIX.test(path)) return false;
  const parts = path.split('/');
  return parts.every(
    (part) =>
      part.length > 0 &&
      part !== '.' &&
      part !== '..' &&
      part.toLowerCase() !== '.git' &&
      !part.includes('\\') &&
      !CONTROL_CHARACTER.test(part),
  );
}

/**
 * The path a picked file takes under /uploads.
 *
 * `webkitRelativePath` is set for a directory upload and empty otherwise, so a
 * folder keeps its shape and a plain selection lands at the top. Leading `./`
 * segments some browsers emit are dropped; anything else is left exactly as
 * given, because rewriting an unsafe path into a safe one hides the problem
 * rather than reporting it.
 */
export function uploadPathFor(file: File): string {
  const relative = (file as File & { webkitRelativePath?: string }).webkitRelativePath;
  const raw = relative && relative.length > 0 ? relative : file.name;
  return raw.replace(/^(\.\/)+/, '');
}
