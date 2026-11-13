import { useCallback, useState } from 'react';

export type EnvironmentStatus = 'loading' | 'ready' | 'error';

/**
 * Whether the chosen repository's environments have arrived.
 *
 * The EnvironmentPicker is keyed by repository and reports its status from an
 * effect, so the render that switches repository still holds the previous
 * repository's report: "ready" for none chosen, or an "error" that is not this
 * one's. For that render the composer looked idle, and its plus menu enabled,
 * while nothing for the new repository had loaded. A report is kept with the
 * repository it was made for, and any other repository reads as loading.
 */
export function useEnvironmentStatus(repo: string) {
  const [report, setReport] = useState<{ repo: string; status: EnvironmentStatus }>({
    repo,
    status: 'loading',
  });
  const onStatusChange = useCallback(
    (status: EnvironmentStatus) => setReport({ repo, status }),
    [repo],
  );
  const status: EnvironmentStatus = report.repo === repo ? report.status : 'loading';
  return [status, onStatusChange] as const;
}
