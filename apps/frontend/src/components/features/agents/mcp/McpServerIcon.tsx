'use client';

import { useState } from 'react';

// Some common publishers omit icons from their Registry entries. Use their
// own public assets, matched by publisher ID rather than a display name.
const SERVICE_ICONS: Record<string, string> = {
  github: 'https://github.githubassets.com/favicons/favicon.svg',
  slack: 'https://slack.com/favicon.ico',
  'google-drive': 'https://ssl.gstatic.com/images/branding/product/2x/drive_2020q4_48dp.png',
  context7: 'https://context7.com/favicon.ico',
  playwright: 'https://playwright.dev/img/playwright-logo.svg',
  linear: 'https://linear.app/favicon.ico',
  notion: 'https://www.notion.so/images/favicon.ico',
  figma: 'https://static.figma.com/app/icon/1/favicon.ico',
  supabase: 'https://supabase.com/favicon/favicon-96x96.png',
  sentry: 'https://sentry.io/favicon.ico',
};
const PUBLISHERS: Record<string, string> = {
  'io.github.github/github-mcp-server': 'github',
  'io.github.upstash/context7': 'context7',
  'io.github.microsoft/playwright-mcp': 'playwright',
  'app.linear/linear': 'linear',
  'com.notion/mcp': 'notion',
  'com.figma.mcp/mcp': 'figma',
  'com.supabase/mcp': 'supabase',
  'io.github.getsentry/sentry-mcp': 'sentry',
};

/** Show publisher artwork, with a stable initial if the image cannot load. */
export function McpServerIcon({
  name,
  label,
  iconUrl,
}: {
  name: string;
  label: string;
  iconUrl?: string | null;
}) {
  const [failed, setFailed] = useState<string[]>([]);
  const src = [iconUrl, SERVICE_ICONS[PUBLISHERS[name] ?? name]].find(
    (url) => url?.startsWith('https://') && !failed.includes(url),
  );
  return (
    <span
      aria-hidden="true"
      className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-gray-100 bg-white text-sm font-semibold text-gray-700 dark:border-gray-200 dark:bg-gray-100 dark:text-gray-700"
    >
      {src ? (
        // Registry assets are untrusted images, never inline SVG or HTML.
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={src}
          alt=""
          width={24}
          height={24}
          className="h-6 w-6 object-contain"
          referrerPolicy="no-referrer"
          onError={() => setFailed((previous) => [...previous, src])}
        />
      ) : (
        label.slice(0, 1).toUpperCase()
      )}
    </span>
  );
}
