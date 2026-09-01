import '../styles/globals.css';
import { config } from '@/config/env';
import { Providers } from '@/components/providers';
import { themeBootScript } from '@/lib/theme';

// The root document.
//
// The moved agent shell (`(agents)/layout.tsx`) is a *nested* layout: it is the
// same file the source repo mounted under /agents, and it renders a sidebar and
// a header, not an `<html>` document. Rather than graft `<html>`/`<body>` into
// it — an edit to a moved file, and one that would make its own test render a
// document inside a `<div>` — the shell keeps its shape inside the `(agents)`
// route group, and this file provides the document around it. Route groups do
// not appear in URLs, so `/`, `/[jobId]`, `/archived`, `/connected` and
// `/integrations` are exactly the routes F3 asks for.
//
// `/login` sits outside the group on purpose: it runs before there is a
// session, so it must not be behind the shell's `ProtectedRoute`. (`/callback`
// used to live here for the same reason; the code exchange moved into the
// control plane, so this app no longer has one.)

export const metadata = {
  title: config.appName,
  description: 'Run coding agents in sandboxed containers against your repositories.',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    // `suppressHydrationWarning` is load-bearing, not noise suppression: the
    // boot script below deliberately mutates this element's class and
    // `color-scheme` before React hydrates, so server and client markup are
    // *expected* to differ here. It applies to this element's own attributes
    // only, not to the tree beneath it.
    <html lang="en" className="h-full" suppressHydrationWarning>
      <head>
        {/* Runs before first paint. Without it every load renders light and
            then flips, which for a dark-mode user is a full-screen white
            flash on every navigation into the app. */}
        <script dangerouslySetInnerHTML={{ __html: themeBootScript() }} />
      </head>
      <body className="flex min-h-screen flex-col bg-gray-50 text-black antialiased dark:bg-gray-950 dark:text-gray-100">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
