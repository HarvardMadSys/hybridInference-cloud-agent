'use client';

import '../styles/globals.css';
import { LocaleProvider } from '@/components/providers/LocaleProvider';
import { RenderErrorFallback } from '@/components/RenderErrorFallback';

/**
 * What the app shows when the root layout itself throws.
 *
 * It replaces that layout, so it renders the document, and none of the
 * layout's providers are above it: no site configuration, no session, and no
 * theme boot script, so it paints in the light theme. The one provider it
 * mounts is the locale's, which needs nothing from the server and keeps the
 * message in the reader's language. The stylesheet is imported here because a
 * layout that failed on the server never put its own on the page. It runs no
 * script of its own, so the nonce CSP is unaffected.
 */
export default function GlobalError({
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <html lang="en" className="h-full">
      <body className="flex min-h-screen flex-col bg-surface-muted font-sans text-foreground antialiased">
        <LocaleProvider>
          <RenderErrorFallback reset={reset} />
        </LocaleProvider>
      </body>
    </html>
  );
}
