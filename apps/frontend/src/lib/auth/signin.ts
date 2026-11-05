// Where sign-in sends the browser back to.

/** Only same-site paths, expressed the way the router speaks them. */
export function safeReturnTo(candidate: string | undefined): string {
  if (
    !candidate ||
    !candidate.startsWith('/') ||
    candidate.startsWith('//') ||
    /[\\\u0000-\u001f\u007f]/.test(candidate)
  )
    return '/';
  return candidate;
}

/** The login page, returning here: another account signs in beside this one. */
export function accountSignInUrl(pathname: string | null): string {
  return `/login?${new URLSearchParams({ return_to: safeReturnTo(pathname ?? '/') })}`;
}
