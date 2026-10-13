'use client';

import { useRef, useState } from 'react';
import { useLocale } from '@/components/providers';
import { ProjectMemorySettings } from './ProjectMemorySettings';
import { UserMemorySettings } from './UserMemorySettings';

const TABS = [
  { id: 'user', label: 'settings.memory.userTab' },
  { id: 'project', label: 'settings.memory.projectTab' },
] as const;
type Tab = (typeof TABS)[number]['id'];

/** User and project memory in one card. Both stay mounted, so switching tabs keeps drafts. */
export function MemorySettings() {
  const { t } = useLocale();
  const [selected, setSelected] = useState<Tab>('user');
  const tabButtons = useRef<(HTMLButtonElement | null)[]>([]);

  return (
    <section aria-labelledby="settings-memory" className="ui-card mt-4 bg-surface p-3 sm:p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 id="settings-memory" className="text-base font-semibold text-foreground">
          {t('settings.memory.title')}
        </h2>
        <div
          role="tablist"
          aria-label={t('settings.memory.tabs')}
          className="flex gap-1 rounded-lg border border-border bg-surface-muted p-1"
        >
          {TABS.map(({ id, label }, index) => (
            <button
              key={id}
              ref={(element) => {
                tabButtons.current[index] = element;
              }}
              type="button"
              role="tab"
              id={`settings-memory-tab-${id}`}
              aria-controls={`settings-memory-panel-${id}`}
              aria-selected={selected === id}
              tabIndex={selected === id ? 0 : -1}
              onClick={() => setSelected(id)}
              onKeyDown={(event) => {
                const nextTabs: Partial<Record<string, number>> = {
                  ArrowRight: (index + 1) % TABS.length,
                  ArrowLeft: (index - 1 + TABS.length) % TABS.length,
                  Home: 0,
                  End: TABS.length - 1,
                };
                const nextIndex = nextTabs[event.key];
                if (nextIndex === undefined) return;
                event.preventDefault();
                setSelected(TABS[nextIndex].id);
                tabButtons.current[nextIndex]?.focus();
              }}
              className={`ui-focus-ring min-h-9 rounded-md px-3 text-sm font-medium transition-colors [@media(pointer:coarse)]:min-h-11 ${
                selected === id
                  ? 'bg-surface text-foreground shadow-subtle'
                  : 'text-muted hover:bg-surface-hover hover:text-foreground'
              }`}
            >
              {t(label)}
            </button>
          ))}
        </div>
      </div>
      <p className="mt-2 text-sm leading-6 text-muted">{t('settings.memory.description')}</p>
      {TABS.map(({ id }) => (
        <div
          key={id}
          role="tabpanel"
          id={`settings-memory-panel-${id}`}
          aria-labelledby={`settings-memory-tab-${id}`}
          hidden={selected !== id}
          className="mt-4"
        >
          {id === 'user' ? <UserMemorySettings /> : <ProjectMemorySettings />}
        </div>
      ))}
    </section>
  );
}
