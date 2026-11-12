import type { AdminSandbox } from '@/lib/api/adminSandboxes';

import { deletionStatus } from './present';

const DELETION_NOTES = {
  pending: { label: 'Deletion pending', className: 'text-amber-700 dark:text-amber-400' },
  failed: { label: 'Deletion failed', className: 'text-red-700 dark:text-red-400' },
} as const;

/** A row's lifecycle. A finished deletion is shown in place of the lost state it leaves. */
export function SandboxStatus({ sandbox }: { sandbox: AdminSandbox }) {
  const deletion = deletionStatus(sandbox);
  const note = deletion === 'pending' || deletion === 'failed' ? DELETION_NOTES[deletion] : null;
  const executing = sandbox.active_attempt_count > 0;
  return (
    <>
      <span
        className={
          executing ? 'text-blue-700 dark:text-blue-400' : 'text-gray-500 dark:text-gray-400'
        }
      >
        {deletion === 'deleted'
          ? 'deleted'
          : sandbox.state !== 'running'
            ? sandbox.state.replaceAll('_', ' ')
            : executing
              ? 'Executing'
              : 'Retained'}
      </span>
      {note && <span className={`mt-1 block ${note.className}`}>{note.label}</span>}
      {executing && (
        <span className="mt-1 block tabular-nums text-gray-500 dark:text-gray-400">
          {sandbox.active_attempt_count} active{' '}
          {sandbox.active_attempt_count === 1 ? 'turn' : 'turns'}
        </span>
      )}
    </>
  );
}
