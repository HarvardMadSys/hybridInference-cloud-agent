'use client';

import { useEffect, useRef, useState } from 'react';
import { useT } from '@/components/providers';
import type { SideChatTab, WorkspaceTab } from './useWorkspaceTabs';

export function WorkspaceTabBar({
  selected,
  chats,
  diffCount,
  onSelect,
  onAdd,
  onClose,
}: {
  selected: WorkspaceTab;
  chats: SideChatTab[];
  diffCount: number;
  onSelect: (tab: WorkspaceTab) => void;
  onAdd: () => WorkspaceTab;
  onClose: (id: SideChatTab['id']) => WorkspaceTab;
}) {
  const t = useT();
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);
  const addRef = useRef<HTMLButtonElement>(null);
  const itemRef = useRef<HTMLButtonElement>(null);
  const [focusTab, setFocusTab] = useState<WorkspaceTab | null>(null);
  const tabs: { key: WorkspaceTab; label: string; chat?: SideChatTab }[] = [
    { key: 'git', label: 'Git' },
    { key: 'terminal', label: 'Terminal' },
    { key: 'files', label: 'Files' },
    ...chats.map((chat) => ({
      key: chat.id,
      label: chat.number === 1 ? t('sideChat.tab') : `${t('sideChat.tab')} ${chat.number}`,
      chat,
    })),
  ];
  useEffect(() => {
    if (!focusTab) return;
    const tab = document.getElementById(`workspace-tab-${focusTab}`);
    tab?.focus();
    tab?.scrollIntoView?.({ block: 'nearest', inline: 'nearest' });
    setFocusTab(null);
  }, [focusTab]);
  useEffect(() => {
    if (!menuOpen) return;
    itemRef.current?.focus();
    const outside = (event: PointerEvent) => {
      if (event.target instanceof Node && !menuRef.current?.contains(event.target))
        setMenuOpen(false);
    };
    document.addEventListener('pointerdown', outside);
    return () => document.removeEventListener('pointerdown', outside);
  }, [menuOpen]);
  return (
    <div className="relative z-10 flex shrink-0 border-b border-gray-200 bg-white px-4 text-[13px] font-medium dark:border-white/10 dark:bg-gray-900">
      <nav
        aria-label="Workspace views"
        role="tablist"
        className="flex min-w-0 gap-1 overflow-x-auto"
      >
        {tabs.map((tab, index) => (
          <div
            key={tab.key}
            data-side-chat-id={tab.chat?.id}
            className={`flex shrink-0 items-center border-b-2 ${
              selected === tab.key
                ? 'border-gray-900 text-gray-900 dark:border-gray-100 dark:text-gray-50'
                : 'border-transparent text-gray-500 dark:text-gray-400'
            }`}
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
              className={`py-3 pl-3 ${tab.chat ? 'pr-1' : 'pr-3'} hover:text-gray-900 focus-visible:outline-crimson dark:hover:text-gray-100`}
            >
              {tab.label}
              {tab.key === 'git' && diffCount ? (
                <span className="ml-1.5 rounded bg-gray-100 px-1.5 py-0.5 text-[10px] text-gray-500 dark:bg-white/5 dark:text-gray-400">
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
                className="mr-1 rounded p-1 text-gray-400 dark:text-gray-500 hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-crimson dark:hover:bg-white/10 dark:hover:text-gray-100"
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
      <div
        ref={menuRef}
        className="relative flex shrink-0 items-center pl-1"
        onBlur={(event) => {
          if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setMenuOpen(false);
        }}
        onKeyDown={(event) => {
          if (event.key === 'Escape' && menuOpen) {
            event.preventDefault();
            event.stopPropagation();
            setMenuOpen(false);
            addRef.current?.focus();
          }
        }}
      >
        <button
          ref={addRef}
          type="button"
          aria-label={t('sideChat.add')}
          title={t('sideChat.add')}
          aria-haspopup="menu"
          aria-expanded={menuOpen}
          onClick={() => setMenuOpen((open) => !open)}
          onKeyDown={(event) => {
            if (event.key === 'ArrowDown') {
              event.preventDefault();
              setMenuOpen(true);
            }
          }}
          className="rounded p-2 text-gray-500 hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-crimson dark:text-gray-400 dark:hover:bg-white/10 dark:hover:text-gray-100"
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
        {menuOpen ? (
          <div
            role="menu"
            aria-label={t('sideChat.add')}
            className="absolute right-0 top-full mt-1 w-48 rounded-lg border border-gray-200 bg-white p-1 shadow-lg dark:border-white/10 dark:bg-gray-900"
          >
            <button
              ref={itemRef}
              type="button"
              role="menuitem"
              onClick={() => {
                setMenuOpen(false);
                setFocusTab(onAdd());
              }}
              className="flex w-full items-center gap-2 rounded-md px-3 py-2 text-left text-gray-900 hover:bg-gray-100 focus:bg-gray-100 focus:outline-none dark:text-gray-100 dark:hover:bg-white/10 dark:focus:bg-white/10"
            >
              <span
                aria-hidden="true"
                className="text-lg leading-none text-gray-400 dark:text-gray-500"
              >
                +
              </span>
              {t('sideChat.new')}
            </button>
          </div>
        ) : null}
      </div>
    </div>
  );
}
