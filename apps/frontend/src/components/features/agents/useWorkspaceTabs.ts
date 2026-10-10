'use client';

import { useState } from 'react';
import type { AgentJobApi } from '@/lib/api/agents';
import { latestJob } from './conversationJob';
import type { AgentJob } from './types';

export type WorkspaceTab = 'git' | 'terminal' | 'files' | 'side-chat' | `side-chat-${string}`;
export interface SideChatTab {
  id: 'side-chat' | `side-chat-${string}`;
  number: number;
  anchor?: { jobId: string; sandboxId: string };
  reviewPrompt?: string;
}
interface TabsState {
  selected: WorkspaceTab;
  chats: SideChatTab[];
  nextNumber: number;
}

export function sideChatAnchor(source: AgentJob, jobs: AgentJobApi[]) {
  const rows = jobs.filter((row) => row.thread_id === source.threadId);
  const anchor =
    latestJob(rows.filter((row) => ['running', 'publishing'].includes(row.state))) ??
    latestJob(rows);
  return { jobId: anchor?.id ?? source.id, sandboxId: source.sandboxId ?? '' };
}

function read(key: string): TabsState {
  // A workspace opens on Git with no side chat: one is created by the + button.
  const initial: TabsState = { selected: 'git', chats: [], nextNumber: 1 };
  try {
    const raw = sessionStorage.getItem(key);
    if (!raw) return initial;
    const value = JSON.parse(raw) as TabsState;
    if (
      !Array.isArray(value.chats) ||
      !Number.isSafeInteger(value.nextNumber) ||
      value.nextNumber < 1 ||
      value.chats.some(
        (chat) =>
          !chat ||
          !/^side-chat(?:-[\w-]+)?$/.test(chat.id) ||
          !Number.isSafeInteger(chat.number) ||
          chat.number < 1 ||
          chat.number >= value.nextNumber ||
          (chat.anchor &&
            (typeof chat.anchor.jobId !== 'string' || typeof chat.anchor.sandboxId !== 'string')) ||
          (chat.reviewPrompt !== undefined &&
            (typeof chat.reviewPrompt !== 'string' || !chat.reviewPrompt.trim() || !chat.anchor)),
      ) ||
      new Set(value.chats.map((chat) => chat.id)).size !== value.chats.length
    )
      return initial;
    if (
      !['git', 'terminal', 'files'].includes(value.selected) &&
      !value.chats.some((chat) => chat.id === value.selected)
    )
      value.selected = 'git';
    return value;
  } catch {
    return initial;
  }
}

/** Open tabs belong to this browser window; child sessions remain durable. */
export function useWorkspaceTabs(
  userId: string | undefined,
  source: AgentJob,
  jobs: AgentJobApi[],
) {
  const key = `agents.workspace.tabs.${encodeURIComponent(userId ?? 'signed-out')}:${encodeURIComponent(source.threadId ?? source.id)}`;
  const [stored, setStored] = useState(() => ({ key, state: read(key) }));
  // Reset before rendering children when navigating to another thread/account.
  if (stored.key !== key) setStored({ key, state: read(key) });
  const state = stored.key === key ? stored.state : read(key);
  function update(next: TabsState) {
    setStored({ key, state: next });
    try {
      sessionStorage.setItem(key, JSON.stringify(next));
    } catch {
      // The mounted workspace still works when tab preferences cannot be saved.
    }
  }
  return {
    selected: state.selected,
    chats: state.chats,
    select(selected: WorkspaceTab) {
      update({ ...state, selected });
    },
    add() {
      const chat: SideChatTab = {
        // The first chat of a conversation keeps the original tab's locator so
        // a receipt written by the earlier UI reopens instead of forking again.
        id: state.nextNumber === 1 ? 'side-chat' : `side-chat-${crypto.randomUUID()}`,
        number: state.nextNumber,
        anchor: sideChatAnchor(source, jobs),
      };
      update({
        selected: chat.id,
        chats: [...state.chats, chat],
        nextNumber: state.nextNumber + 1,
      });
      return chat.id;
    },
    review(prompt: string) {
      const existing = state.chats.find(
        (chat) => chat.reviewPrompt && chat.anchor?.jobId === source.id,
      );
      if (existing) {
        update({ ...state, selected: existing.id });
        return existing.id;
      }
      const chat: SideChatTab = {
        id: `side-chat-${crypto.randomUUID()}`,
        number: state.nextNumber,
        anchor: { jobId: source.id, sandboxId: source.sandboxId ?? '' },
        reviewPrompt: prompt,
      };
      update({
        selected: chat.id,
        chats: [...state.chats, chat],
        nextNumber: state.nextNumber + 1,
      });
      return chat.id;
    },
    close(id: SideChatTab['id']) {
      const index = state.chats.findIndex((chat) => chat.id === id);
      const chats = state.chats.filter((chat) => chat.id !== id);
      const selected =
        state.selected === id
          ? (chats[Math.min(index, chats.length - 1)]?.id ?? 'git')
          : state.selected;
      // Do not delete receipts or stop jobs: closing is only a workspace action.
      update({ ...state, selected, chats });
      return selected;
    },
  };
}
