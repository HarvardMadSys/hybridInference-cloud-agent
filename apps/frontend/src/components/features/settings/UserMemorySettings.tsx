'use client';

import { useCallback } from 'react';
import { useAuth, useLocale } from '@/components/providers';
import {
  clearUserMemory,
  consolidateUserMemory,
  deleteUserMemoryEntry,
  editUserMemoryEntry,
  getUserMemory,
  saveUserMemories,
  updateUserMemory,
  type UserMemoryView,
} from '@/lib/api/userMemory';
import { MemoryPanel, type MemoryApi } from './MemoryPanel';

const USER_MEMORY: MemoryApi<UserMemoryView> = {
  read: (signal) => getUserMemory(signal),
  toggle: (enabled, signal) => updateUserMemory(enabled, signal),
  clear: (signal) => clearUserMemory(signal),
  edit: (id, content, revision, signal) => editUserMemoryEntry(id, content, revision, signal),
  remove: (id, revision, signal) => deleteUserMemoryEntry(id, revision, signal),
  save: (revision, memories, signal) => saveUserMemories(revision, memories, signal),
  consolidate: (revision, model, signal) => consolidateUserMemory(revision, model, signal),
};

export type AvatarChange = (userId: string, dataUrl?: string) => void;

/** Memory that applies to every project. */
export function UserMemorySettings({ onAvatarChange }: { onAvatarChange?: AvatarChange }) {
  const { state } = useAuth();
  const { t } = useLocale();
  const userId = state.user?.id;
  const updateAvatar = useCallback(
    (view: UserMemoryView | null) => {
      if (userId) onAvatarChange?.(userId, view?.avatar?.data_url);
    },
    [userId, onAvatarChange],
  );

  return (
    <>
      <p className="text-xs leading-relaxed text-muted">{t('settings.memory.hint')}</p>
      {userId ? (
        // A new account gets a fresh panel immediately, including before effects run.
        <MemoryPanel key={`user:${userId}`} api={USER_MEMORY} onViewChange={updateAvatar} />
      ) : (
        <p className="mt-3 text-sm text-muted">{t('settings.memory.signIn')}</p>
      )}
    </>
  );
}
