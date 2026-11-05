import { describe, expect, it } from 'vitest';

import { deletedWorkspacePaths, workspaceGitProjection } from './workspaceGitMetadata';

describe('workspace Git metadata projection', () => {
  it('projects distinct changed paths without claiming a parsed diff', () => {
    expect(
      workspaceGitProjection([], {
        gitMetadata: {
          available: true,
          revision: 'a'.repeat(64),
          branch: 'agent/work',
          changes: [
            { code: ' M', path: 'src/app.ts' },
            { code: 'MM', path: 'src/app.ts' },
          ],
          change_count: 2,
        },
      }),
    ).toMatchObject({
      diffFiles: ['src/app.ts'],
      workspaceGitRevision: 'a'.repeat(64),
      workspaceGitAvailable: true,
    });
  });

  it('combines patch and metadata deletions into unreadable live paths', () => {
    const deleted = deletedWorkspacePaths(
      [
        {
          path: 'from-patch.ts',
          add: 0,
          del: 1,
          lines: [{ marker: 'hunk', text: 'deleted file mode 100644' }],
        },
      ],
      [{ code: ' D', path: 'from-status.ts' }],
    );

    expect([...deleted]).toEqual(['from-patch.ts', 'from-status.ts']);
  });
});
