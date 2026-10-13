'use client';

import { useAuth, useLocale } from '@/components/providers';
import {
  clearUserMemory,
  consolidateUserMemory,
  deleteUserMemoryEntry,
  editUserMemoryEntry,
  getUserMemory,
  saveUserMemories,
  updateUserMemory,
} from '@/lib/api/userMemory';
import { MemoryPanel, type MemoryApi } from './MemoryPanel';

const USER_MEMORY: MemoryApi = {
  read: (signal) => getUserMemory(signal),
  toggle: (enabled, signal) => updateUserMemory(enabled, signal),
  clear: (signal) => clearUserMemory(signal),
  edit: (id, content, revision, signal) => editUserMemoryEntry(id, content, revision, signal),
  remove: (id, revision, signal) => deleteUserMemoryEntry(id, revision, signal),
  save: (revision, memories, signal) => saveUserMemories(revision, memories, signal),
  consolidate: (revision, model, signal) => consolidateUserMemory(revision, model, signal),
};

/** Memory that applies to every project. */
export function UserMemorySettings() {
  const { state } = useAuth();
  const { t } = useLocale();
  const userId = state.user?.id;

  return (
    <>
      <p className="text-xs leading-relaxed text-muted">{t('settings.memory.hint')}</p>
      {userId ? (
        // A new account gets a fresh panel immediately, including before effects run.
        <MemoryPanel key={`user:${userId}`} api={USER_MEMORY} />
      ) : (
        <p className="mt-3 text-sm text-muted">{t('settings.memory.signIn')}</p>
      )}
    </>
  );
}
