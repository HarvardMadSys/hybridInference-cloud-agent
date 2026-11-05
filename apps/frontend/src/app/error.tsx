'use client';

import { RenderErrorFallback } from '@/components/RenderErrorFallback';

/**
 * What a page outside the agent shell shows when it throws while rendering, and
 * what the shell shows when its own layout does.
 *
 * `(agents)/error.tsx` sits inside the shell's layout, so it cannot catch that
 * layout, and `/login`, `/setup` and `/teams/join` are outside the shell
 * altogether. This one sits just inside the root layout: the document and its
 * providers stay, and Next clears it on the next navigation. The root layout
 * itself fails into `global-error.tsx`.
 */
export default function RootError({
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return <RenderErrorFallback reset={reset} />;
}
