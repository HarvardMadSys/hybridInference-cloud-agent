import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useT } from '@/components/providers';
import type { SidebarPreferences } from './sidebarPreferences';

export function SidebarViewMenu({
  value,
  onChange,
}: {
  value: SidebarPreferences;
  onChange: (field: 'group' | 'order', value: string) => void;
}) {
  const t = useT();
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const [position, setPosition] = useState<{ top: number; left: number } | null>(null);
  const close = () => {
    setPosition(null);
    trigger.current?.focus();
  };
  useEffect(() => {
    if (!position) return;
    menu.current?.querySelector<HTMLButtonElement>('[aria-checked="true"]')?.focus();
    const outside = (event: PointerEvent) => {
      if (
        !menu.current?.contains(event.target as Node) &&
        !trigger.current?.contains(event.target as Node)
      )
        setPosition(null);
    };
    const reposition = () => setPosition(null);
    document.addEventListener('pointerdown', outside);
    window.addEventListener('resize', reposition);
    window.addEventListener('scroll', reposition, true);
    return () => {
      document.removeEventListener('pointerdown', outside);
      window.removeEventListener('resize', reposition);
      window.removeEventListener('scroll', reposition, true);
    };
  }, [position]);
  return (
    <>
      <button
        ref={trigger}
        type="button"
        aria-label={t('sidebar.viewOptions')}
        title={t('sidebar.viewOptions')}
        aria-haspopup="menu"
        aria-expanded={Boolean(position)}
        className="rounded-md p-1 text-gray-400 transition-colors hover:bg-gray-200/60 hover:text-gray-700 dark:text-gray-500 dark:hover:bg-white/10 dark:hover:text-gray-200"
        onClick={() => {
          if (position) {
            close();
            return;
          }
          const rect = trigger.current!.getBoundingClientRect();
          setPosition({
            left: Math.max(8, Math.min(rect.right - 220, window.innerWidth - 228)),
            top: Math.max(8, Math.min(rect.bottom + 6, window.innerHeight - 264)),
          });
        }}
      >
        <svg
          width="16"
          height="16"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.8"
          aria-hidden="true"
        >
          <path d="M3 6h7m4 0h7M3 12h12m4 0h2M3 18h3m4 0h11" />
          <circle cx="12" cy="6" r="2" />
          <circle cx="17" cy="12" r="2" />
          <circle cx="8" cy="18" r="2" />
        </svg>
      </button>
      {position &&
        createPortal(
          <div
            ref={menu}
            role="menu"
            aria-label={t('sidebar.viewOptions')}
            style={position}
            className="fixed z-50 w-[220px] rounded-xl border border-gray-200 bg-white p-1.5 text-[13px] shadow-xl dark:border-white/10 dark:bg-gray-800"
            onKeyDown={(event) => {
              if (event.key === 'Escape') {
                event.preventDefault();
                event.stopPropagation();
                close();
              }
              if (event.key === 'Tab') setPosition(null);
              if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
              event.preventDefault();
              const options = [
                ...menu.current!.querySelectorAll<HTMLButtonElement>('[role="menuitemradio"]'),
              ];
              const current = options.indexOf(document.activeElement as HTMLButtonElement);
              const next =
                event.key === 'Home'
                  ? 0
                  : event.key === 'End'
                    ? options.length - 1
                    : (current + (event.key === 'ArrowDown' ? 1 : -1) + options.length) %
                      options.length;
              options[next].focus();
            }}
          >
            {(['group', 'order'] as const).map((field) => (
              <div
                role="group"
                aria-label={t(field === 'group' ? 'sidebar.groupBy' : 'sidebar.orderBy')}
                key={field}
                className={
                  field === 'order' ? 'mt-1 border-t border-gray-200 pt-1 dark:border-white/10' : ''
                }
              >
                <div className="px-3 py-1.5 text-gray-500 dark:text-gray-400">
                  {t(field === 'group' ? 'sidebar.groupBy' : 'sidebar.orderBy')}
                </div>
                {(field === 'group'
                  ? (['project', 'flat'] as const)
                  : (['manual', 'updated'] as const)
                ).map((option) => (
                  <button
                    key={option}
                    role="menuitemradio"
                    aria-checked={value[field] === option}
                    tabIndex={-1}
                    className="flex w-full items-center justify-between rounded-md px-3 py-2.5 text-left text-gray-800 hover:bg-gray-100 focus:bg-gray-100 focus:outline-none dark:text-gray-100 dark:hover:bg-white/10 dark:focus:bg-white/10"
                    onClick={() => {
                      onChange(field, option);
                      close();
                    }}
                  >
                    {t(
                      option === 'project'
                        ? 'sidebar.byProject'
                        : option === 'flat'
                          ? 'sidebar.inOneList'
                          : option === 'manual'
                            ? 'sidebar.manual'
                            : 'sidebar.lastUpdated',
                    )}
                    {value[field] === option && <span aria-hidden="true">✓</span>}
                  </button>
                ))}
              </div>
            ))}
          </div>,
          document.body,
        )}
    </>
  );
}
