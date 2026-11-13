// @vitest-environment jsdom
import { act, renderHook } from '@testing-library/react';
import { expect, it } from 'vitest';
import { useEnvironmentStatus } from './useEnvironmentStatus';

it('reads as loading for a repository its picker has not reported for yet', () => {
  const { result, rerender } = renderHook(({ repo }) => useEnvironmentStatus(repo), {
    initialProps: { repo: '' },
  });
  expect(result.current[0]).toBe('loading');
  act(() => result.current[1]('ready'));
  expect(result.current[0]).toBe('ready');

  // The render that switches repository holds the last repository's report.
  const previous = result.current[1];
  rerender({ repo: 'owner/repository' });
  expect(result.current[0]).toBe('loading');
  // A late report from the picker that was replaced is not this repository's.
  act(() => previous('error'));
  expect(result.current[0]).toBe('loading');
  act(() => result.current[1]('error'));
  expect(result.current[0]).toBe('error');
  act(() => result.current[1]('ready'));
  expect(result.current[0]).toBe('ready');
});
