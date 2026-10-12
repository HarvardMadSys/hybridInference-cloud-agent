'use client';

import { useRef, useState } from 'react';

import { searchSettingsItems, type SettingsSearchItem } from './settingsSearch';

export function SettingsSearch({
  items,
  loading,
  onInteract,
  onSelect,
}: {
  items: SettingsSearchItem[];
  loading: boolean;
  onInteract: () => void;
  onSelect: (item: SettingsSearchItem) => void;
}) {
  const [query, setQuery] = useState('');
  const input = useRef<HTMLInputElement>(null);
  const searching = query.trim() !== '';
  const results = searchSettingsItems(items, query);
  const select = (item: SettingsSearchItem) => {
    setQuery('');
    onSelect(item);
  };

  return (
    <div className="mt-6">
      <label htmlFor="admin-settings-search" className="text-sm font-medium text-foreground">
        Search settings
      </label>
      <div className="mt-2 flex items-center gap-2">
        <input
          ref={input}
          id="admin-settings-search"
          type="search"
          value={query}
          onFocus={onInteract}
          onChange={(event) => {
            onInteract();
            setQuery(event.target.value);
          }}
          onKeyDown={(event) => {
            if (event.nativeEvent.isComposing || event.keyCode === 229) return;
            if (event.key === 'Escape') {
              event.preventDefault();
              onInteract();
              setQuery('');
            } else if (event.key === 'Enter' && results.length > 0) {
              event.preventDefault();
              select(results[0]);
            }
          }}
          placeholder="Search by name, description, or configuration key"
          aria-describedby="admin-settings-search-help"
          aria-controls={searching ? 'admin-settings-search-results' : undefined}
          autoComplete="off"
          className="ui-input min-h-11 min-w-0 flex-1"
        />
        {query && (
          <button
            type="button"
            onClick={() => {
              onInteract();
              setQuery('');
              input.current?.focus();
            }}
            className="ui-button ui-button-secondary min-h-11 shrink-0"
          >
            Clear search
          </button>
        )}
      </div>
      <p id="admin-settings-search-help" className="mt-2 text-xs leading-5 text-muted">
        Find settings across all sections. Select a result to open its setting.
      </p>
      {searching && (
        <div
          id="admin-settings-search-results"
          role="region"
          aria-label="Settings search results"
          className="mt-3 overflow-hidden rounded-xl border border-border bg-surface"
        >
          <p
            role="status"
            className="border-b border-border bg-surface-muted px-4 py-3 text-sm text-muted"
          >
            {results.length > 0
              ? `${results.length} ${results.length === 1 ? 'setting' : 'settings'} found.${loading ? ' Loading more settings…' : ''}`
              : loading
                ? 'Loading settings…'
                : 'No settings found. Try another name or configuration key.'}
          </p>
          {results.length > 0 && (
            <ul className="max-h-80 overflow-y-auto divide-y divide-border">
              {results.map((item) => (
                <li key={item.id}>
                  <button
                    type="button"
                    aria-label={`Open ${item.label} in ${item.section}`}
                    onClick={() => select(item)}
                    className="ui-focus-ring w-full px-4 py-3 text-left transition-colors hover:bg-surface-hover"
                  >
                    <span className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
                      <span className="text-sm font-medium text-foreground">{item.label}</span>
                      <span className="text-xs text-muted">{item.section}</span>
                    </span>
                    {item.description && (
                      <span className="mt-1 block text-sm leading-6 text-muted">
                        {item.description}
                      </span>
                    )}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
