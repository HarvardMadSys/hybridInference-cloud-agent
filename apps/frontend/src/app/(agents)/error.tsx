'use client';

import { RenderErrorFallback } from '@/components/RenderErrorFallback';

/**
 * What a page in the agent shell shows when it throws while rendering.
 *
 * Without a boundary, one value the UI could not draw — a stored message, a
 * workspace entry — unmounted the whole app, and reopening that conversation
 * did it again. This stands in for the page alone: the header and sidebar
 * around it stay usable, and Next clears it on the next navigation.
 */
export default function AgentsError({
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return <RenderErrorFallback reset={reset} />;
}
