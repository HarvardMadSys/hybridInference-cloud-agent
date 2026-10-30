'use client';

import { useEffect, useState } from 'react';
import { useT } from '@/components/providers';
import type { SideChatTab, WorkspaceTab } from './useWorkspaceTabs';

export function WorkspaceTabBar({
  selected,
  chats,
  diffCount,
  onSelect,
  onAdd,
  onClose,
  onBack,
}: {
  selected: WorkspaceTab;
  chats: SideChatTab[];
  diffCount: number;
  onSelect: (tab: WorkspaceTab) => void;
  onAdd: () => WorkspaceTab;
  onClose: (id: SideChatTab['id']) => WorkspaceTab;
  onBack?: () => void;
}) {
  const t = useT();
  const [focusTab, setFocusTab] = useState<WorkspaceTab | null>(null);
  const tabs: { key: WorkspaceTab; label: string; chat?: SideChatTab }[] = [
    { key: 'git', label: 'Git' },
    { key: 'terminal', label: 'Terminal' },
    { key: 'files', label: 'Files' },
    { key: 'environment', label: t('environment.title') },
    ...chats.map((chat) => {
      const label = t(chat.reviewPrompt ? 'review.tab' : 'sideChat.tab');
      return {
        key: chat.id,
        label: chat.number === 1 ? label : `${label} ${chat.number}`,
        chat,
      };
    }),
  ];
  useEffect(() => {
    if (!focusTab) return;
    const tab = document.getElementById(`workspace-tab-${focusTab}`);
    tab?.focus();
    tab?.scrollIntoView?.({ block: 'nearest', inline: 'nearest' });
    setFocusTab(null);
  }, [focusTab]);
  return (
    <div className="relative z-10 flex shrink-0 flex-col border-b border-border bg-surface-muted text-[13px] font-medium">
      {onBack ? (
        <div className="border-b border-border px-3 py-1.5 lg:hidden">
          <button
            type="button"
            onClick={onBack}
            className="ui-button ui-button-ghost min-h-9 px-2 py-1 text-xs"
          >
            <svg
              aria-hidden="true"
              width="16"
              height="16"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.8"
            >
              <path d="m14 6-6 6 6 6" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
            Back to conversation
          </button>
        </div>
      ) : null}
      <div className="flex min-w-0 px-2 sm:px-3">
        <nav
          aria-label="Workspace views"
          role="tablist"
          className="flex min-w-0 flex-1 gap-1 overflow-x-auto"
        >
          {tabs.map((tab, index) => (
            <div
              key={tab.key}
              data-side-chat-id={tab.chat?.id}
              className="flex shrink-0 items-center"
            >
              <button
                id={`workspace-tab-${tab.key}`}
                type="button"
                role="tab"
                aria-controls={`workspace-panel-${tab.key}`}
                aria-selected={selected === tab.key}
                tabIndex={selected === tab.key ? 0 : -1}
                onClick={() => onSelect(tab.key)}
                onKeyDown={(event) => {
                  const target =
                    event.key === 'ArrowRight'
                      ? (index + 1) % tabs.length
                      : event.key === 'ArrowLeft'
                        ? (index + tabs.length - 1) % tabs.length
                        : event.key === 'Home'
                          ? 0
                          : event.key === 'End'
                            ? tabs.length - 1
                            : null;
                  if (target !== null) {
                    event.preventDefault();
                    onSelect(tabs[target].key);
                    setFocusTab(tabs[target].key);
                  }
                }}
                className={`ui-tab min-h-11 rounded-t-lg py-3 pl-3 aria-selected:bg-surface aria-selected:font-semibold aria-selected:text-accent ${tab.chat ? 'pr-1' : 'pr-3'}`}
              >
                {tab.label}
                {tab.key === 'git' && diffCount ? (
                  <span className="ml-1.5 rounded bg-surface-muted px-1.5 py-0.5 text-[10px] text-muted">
                    {diffCount}
                  </span>
                ) : null}
              </button>
              {tab.chat ? (
                <button
                  type="button"
                  aria-label={t('sideChat.close', { name: tab.label })}
                  title={t('sideChat.close', { name: tab.label })}
                  onClick={() => setFocusTab(onClose(tab.chat!.id))}
                  className="ui-icon-button ui-button-ghost mr-1 h-9 w-9 shrink-0 p-2"
                >
                  <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                    <path
                      d="m4 4 8 8m0-8-8 8"
                      stroke="currentColor"
                      strokeWidth="1.5"
                      strokeLinecap="round"
                    />
                  </svg>
                </button>
              ) : null}
            </div>
          ))}
        </nav>
        <div className="flex shrink-0 items-center pl-1">
          <button
            type="button"
            aria-label={t('sideChat.new')}
            title={t('sideChat.new')}
            onClick={() => setFocusTab(onAdd())}
            className="ui-icon-button ui-button-ghost h-9 w-9 shrink-0"
          >
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">
              <path
                d="M8 2v12M2 8h12"
                stroke="currentColor"
                strokeWidth="1.5"
                strokeLinecap="round"
              />
            </svg>
          </button>
          {onBack ? (
            <button
              type="button"
              aria-label="Close sidebar"
              title="Close sidebar"
              onClick={onBack}
              className="ui-icon-button ui-button-ghost h-9 w-9 shrink-0"
            >
              <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                <path
                  d="m4 4 8 8m0-8-8 8"
                  stroke="currentColor"
                  strokeWidth="1.5"
                  strokeLinecap="round"
                />
              </svg>
            </button>
          ) : null}
        </div>
      </div>
    </div>
  );
}
