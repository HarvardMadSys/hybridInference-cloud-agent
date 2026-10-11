/** Resolve a sandbox file reference without turning external links into file reads. */
export function workspaceLinkPath(href: string): string | null {
  if (!href || href !== href.trim() || /[\u0000-\u001f\u007f\\]/.test(href)) return null;
  let source = href;
  const explicitWorkspace =
    source.startsWith('/workspace/') || source.startsWith('file:///workspace/');
  if (source.startsWith('file:///workspace/')) source = source.slice('file://'.length);
  else if (/^[a-z][a-z\d+.-]*:/i.test(source) || source.startsWith('//')) return null;
  source = source.split(/[?#]/, 1)[0];
  try {
    source = decodeURIComponent(source);
  } catch {
    return null;
  }
  if (/[\u0000-\u001f\u007f\\%]/.test(source)) return null;
  if (source.startsWith('/workspace/')) source = source.slice('/workspace/'.length);
  else if (source.startsWith('/')) return null;
  source = source.replace(/:\d+(?::\d+)?$/, '');
  const segments = source.split('/');
  if (segments.some((segment) => segment === '..' || segment === '' || segment.includes(':')))
    return null;
  const path = segments.filter((segment) => segment !== '.').join('/');
  if (!path || path.length > 1024) return null;
  // A bare navigation word or an in-page anchor is not a file reference.
  return explicitWorkspace ||
    path.includes('/') ||
    /\.[^/]+$/.test(path) ||
    /^(?:Dockerfile|Containerfile|Makefile|LICENSE|LICENCE|README|NOTICE|COPYING|Procfile|Gemfile)$/i.test(
      path,
    )
    ? path
    : null;
}
