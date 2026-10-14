'use client';

import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import toast from 'react-hot-toast';
import { useAuth } from '@/components/providers';
import { useLocale } from '@/components/providers/LocaleProvider';
import { useSiteConfig } from '@/components/providers/SiteConfigProvider';
import { useTheme } from '@/components/providers/ThemeProvider';
import { AvatarImage } from '@/components/features/profile/AvatarImage';
import { useUserProfile } from '@/components/features/profile/useUserProfile';
import {
  BookIcon,
  CheckIcon,
  ChevronIcon,
  ContrastIcon,
  ExternalIcon,
  GearIcon,
  GlobeIcon,
  HelpIcon,
  HomeIcon,
  LogOutIcon,
  MailIcon,
  MonitorIcon,
  MoonIcon,
  MoreIcon,
  SunIcon,
} from '@/components/features/settings/icons';
import { LOCALES, LOCALE_LABELS } from '@/lib/i18n/locales';
import { THEME_PREFERENCES, type ThemePreference } from '@/lib/theme';

type Submenu = 'appearance' | 'language' | 'help';

const THEME_ICONS: Record<ThemePreference, typeof SunIcon> = {
  light: SunIcon,
  dark: MoonIcon,
  system: MonitorIcon,
};

const THEME_LABEL_KEYS = {
  light: 'settings.appearance.light',
  dark: 'settings.appearance.dark',
  system: 'settings.appearance.system',
} as const;

const MENU_WIDTH = 248;
const SUBMENU_WIDTH = 216;
// A submenu grows with its longest label, such as a deployment's
// "<organization> Docs", up to this width; past it the label is cut.
const SUBMENU_MAX_WIDTH = 360;
const EDGE = 8;

const PANEL =
  'fixed z-[70] rounded-xl border border-border bg-surface p-1.5 text-[13px] text-foreground shadow-xl';
const ITEM =
  'flex min-h-9 w-full items-center gap-2.5 rounded-lg px-2.5 py-1.5 text-left outline-none transition-colors hover:bg-surface-hover focus-visible:bg-surface-hover [@media(pointer:coarse)]:min-h-11';
const SEPARATOR = 'my-1 h-px bg-border';

/**
 * The signed-in account and everything about it: who you are, the website's
 * own preferences, help, and signing out. One place for them instead of a row
 * of header links, the way most chat apps keep them in the sidebar's corner.
 *
 * `sidebar` is the full row at the bottom of the task list; `header` is the
 * avatar alone, for when the task list is hidden and the row with it.
 */
export function AccountMenu({ variant }: { variant: 'sidebar' | 'header' }) {
  const { t, locale, setLocale } = useLocale();
  const { preference, setPreference } = useTheme();
  const { state, logout } = useAuth();
  const { branding } = useSiteConfig();
  const router = useRouter();
  const pathname = usePathname();
  const userId = state.user?.id;
  const { profile } = useUserProfile(userId);

  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const submenuPanel = useRef<HTMLDivElement>(null);
  const submenuParents = useRef<Partial<Record<Submenu, HTMLButtonElement | null>>>({});
  const [open, setOpen] = useState(false);
  const [submenu, setSubmenu] = useState<Submenu | null>(null);
  // Whether opening the submenu should move focus into it: yes from the
  // keyboard, no when the pointer merely passes over its parent.
  const focusSubmenu = useRef(false);
  const [menuPosition, setMenuPosition] = useState<{ left: number; top: number } | null>(null);
  const [submenuPosition, setSubmenuPosition] = useState<{ left: number; top: number } | null>(
    null,
  );

  const name = [profile?.first_name, profile?.last_name].filter(Boolean).join(' ').trim();
  const label = name || profile?.email || '';
  const imageDataUrl = profile?.image_data_url ?? undefined;
  const companionDataUrl = profile?.companion_data_url || undefined;
  const docsLabel =
    branding.orgName || branding.appName
      ? t('nav.docsNamed', { name: branding.orgName || branding.appName })
      : t('nav.docs');
  const hasHelp = Boolean(branding.docsUrl || branding.contactUrl);

  const close = (returnFocus = true) => {
    setOpen(false);
    setSubmenu(null);
    setMenuPosition(null);
    setSubmenuPosition(null);
    if (returnFocus) trigger.current?.focus();
  };

  // Placed after the panel has rendered, because its height depends on which
  // links this deployment has. Centered on the trigger, kept on screen; above
  // it when it sits low on the screen (the sidebar row), below it otherwise
  // (the header avatar).
  useLayoutEffect(() => {
    if (!open || !menu.current || !trigger.current) return;
    const anchor = trigger.current.getBoundingClientRect();
    const height = menu.current.offsetHeight;
    const width = Math.min(MENU_WIDTH, window.innerWidth - EDGE * 2);
    const centered = anchor.left + (anchor.width - width) / 2;
    const left = Math.max(EDGE, Math.min(centered, window.innerWidth - width - EDGE));
    const above = anchor.top > window.innerHeight / 2;
    let top = above ? anchor.top - height - 6 : anchor.bottom + 6;
    top = Math.max(EDGE, Math.min(top, window.innerHeight - height - EDGE));
    setMenuPosition({ left, top });
    menu.current.querySelector<HTMLElement>('[role="menuitem"]')?.focus();
  }, [open]);

  // Beside its parent item: to the right when it fits, else to the left, else
  // over the menu (a phone-width sidebar has room for neither).
  useLayoutEffect(() => {
    const parent = submenu ? submenuParents.current[submenu] : null;
    if (!submenu || !parent || !menu.current || !submenuPanel.current || !menuPosition) {
      setSubmenuPosition(null);
      return;
    }
    const item = parent.getBoundingClientRect();
    const panel = menu.current.getBoundingClientRect();
    const height = submenuPanel.current.offsetHeight;
    const width = submenuPanel.current.offsetWidth;
    let left = panel.right + 4;
    if (left + width > window.innerWidth - EDGE) left = panel.left - width - 4;
    if (left < EDGE) left = Math.max(EDGE, window.innerWidth - width - EDGE);
    const top = Math.max(EDGE, Math.min(item.top - 6, window.innerHeight - height - EDGE));
    setSubmenuPosition({ left, top });
    if (focusSubmenu.current) {
      focusSubmenu.current = false;
      focusIntoSubmenu();
    }
  }, [submenu, menuPosition]);

  function focusIntoSubmenu() {
    const panel = submenuPanel.current;
    if (!panel) return;
    const items = panel.querySelectorAll<HTMLElement>('[role="menuitem"], [role="menuitemradio"]');
    (panel.querySelector<HTMLElement>('[aria-checked="true"]') ?? items[0])?.focus();
  }

  // Outside clicks close it; so do resizing and scrolling, which would leave
  // a fixed panel floating away from the row it belongs to.
  useEffect(() => {
    if (!open) return;
    const dismiss = () => {
      setOpen(false);
      setSubmenu(null);
      setMenuPosition(null);
      setSubmenuPosition(null);
    };
    const outside = (event: PointerEvent) => {
      const target = event.target as Node;
      if (
        !menu.current?.contains(target) &&
        !submenuPanel.current?.contains(target) &&
        !trigger.current?.contains(target)
      )
        dismiss();
    };
    document.addEventListener('pointerdown', outside);
    window.addEventListener('resize', dismiss);
    window.addEventListener('scroll', dismiss, true);
    return () => {
      document.removeEventListener('pointerdown', outside);
      window.removeEventListener('resize', dismiss);
      window.removeEventListener('scroll', dismiss, true);
    };
  }, [open]);

  const openSubmenu = (key: Submenu, withFocus: boolean) => {
    if (key === submenu) {
      // Already open: the pointer opened it on its way to this click. Nothing
      // re-renders, so focus it now rather than leave the request pending for
      // whichever submenu the pointer next passes over.
      if (withFocus) focusIntoSubmenu();
      return;
    }
    focusSubmenu.current = withFocus;
    setSubmenu(key);
  };

  const closeSubmenu = () => {
    const parent = submenu ? submenuParents.current[submenu] : null;
    setSubmenu(null);
    parent?.focus();
  };

  function moveFocus(panel: HTMLElement | null, key: string) {
    if (!panel) return;
    const items = [
      ...panel.querySelectorAll<HTMLElement>('[role="menuitem"], [role="menuitemradio"]'),
    ];
    if (!items.length) return;
    const current = items.indexOf(document.activeElement as HTMLElement);
    const next =
      key === 'Home'
        ? 0
        : key === 'End'
          ? items.length - 1
          : (current + (key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
    items[next].focus();
  }

  const handleLogout = async () => {
    close(false);
    try {
      await logout();
    } catch {
      // The session cookie is still set, so this browser is still signed in.
      toast.error(t('nav.logOutFailed'));
      return;
    }
    router.replace('/');
  };

  const avatar = (size: string) => (
    <span
      aria-hidden="true"
      className={`${size} flex shrink-0 items-center justify-center overflow-hidden rounded-full border border-border bg-surface text-[11px] font-semibold text-muted`}
    >
      {/* The uploaded photo, else the account's fixed companion, both from
          the profile read; an initial until it answers. */}
      {imageDataUrl || companionDataUrl ? (
        <AvatarImage imageDataUrl={imageDataUrl} companionDataUrl={companionDataUrl} />
      ) : (
        (label.charAt(0) || '·').toUpperCase()
      )}
    </span>
  );

  const parentItem = (key: Submenu, icon: React.ReactNode, text: string) => (
    <button
      ref={(element) => {
        submenuParents.current[key] = element;
      }}
      type="button"
      role="menuitem"
      tabIndex={-1}
      aria-haspopup="menu"
      aria-expanded={submenu === key}
      className={`${ITEM} ${submenu === key ? 'bg-surface-hover' : ''}`}
      onPointerEnter={() => openSubmenu(key, false)}
      onClick={() => openSubmenu(key, true)}
      onKeyDown={(event) => {
        if (event.key === 'ArrowRight') {
          event.preventDefault();
          openSubmenu(key, true);
        }
      }}
    >
      <span className="text-muted">{icon}</span>
      <span className="min-w-0 flex-1 truncate">{text}</span>
      <ChevronIcon className="h-3.5 w-3.5 shrink-0 text-muted" />
    </button>
  );

  // Hovering an ordinary item closes an open submenu, as in a native menu.
  const plain = { tabIndex: -1, onPointerEnter: () => setSubmenu(null) } as const;

  return (
    <>
      <button
        ref={trigger}
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={label ? `${t('nav.account')}: ${label}` : t('nav.account')}
        title={t('nav.account')}
        onClick={() => (open ? close() : setOpen(true))}
        onKeyDown={(event) => {
          if (!open && (event.key === 'ArrowDown' || event.key === 'ArrowUp')) {
            event.preventDefault();
            setOpen(true);
          }
        }}
        className={
          variant === 'sidebar'
            ? `ui-focus-ring flex min-h-10 w-full items-center gap-2.5 rounded-lg px-2 py-1.5 text-left text-[13px] font-medium text-foreground transition-colors hover:bg-surface-hover [@media(pointer:coarse)]:min-h-11 ${open ? 'bg-surface-hover' : ''}`
            : 'ui-focus-ring inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-full transition-colors hover:bg-surface-hover [@media(pointer:coarse)]:h-11 [@media(pointer:coarse)]:w-11'
        }
      >
        {avatar('h-7 w-7')}
        {variant === 'sidebar' ? (
          <>
            <span className="min-w-0 flex-1 truncate">{label}</span>
            <MoreIcon className="h-4 w-4 shrink-0 text-muted" />
          </>
        ) : null}
      </button>
      {open &&
        createPortal(
          <>
            <div
              ref={menu}
              role="menu"
              aria-label={t('nav.account')}
              style={{
                width: Math.min(MENU_WIDTH, window.innerWidth - EDGE * 2),
                ...(menuPosition ?? { left: -9999, top: -9999 }),
              }}
              className={PANEL}
              onKeyDown={(event) => {
                if (event.key === 'Escape') {
                  event.preventDefault();
                  event.stopPropagation();
                  close();
                } else if (event.key === 'Tab') {
                  close(false);
                } else if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
                  event.preventDefault();
                  setSubmenu(null);
                  moveFocus(menu.current, event.key);
                }
              }}
            >
              {profile?.email ? (
                <div className="truncate px-2.5 pb-1.5 pt-1 text-xs text-muted">
                  {profile.email}
                </div>
              ) : null}
              {branding.gatewayUrl ? (
                <a
                  role="menuitem"
                  href={`${branding.gatewayUrl}/dashboard`}
                  className={ITEM}
                  onClick={() => close(false)}
                  {...plain}
                >
                  <HomeIcon className="h-4 w-4 shrink-0 text-muted" />
                  <span className="min-w-0 flex-1 truncate">{t('nav.dashboard')}</span>
                </a>
              ) : null}
              <Link
                role="menuitem"
                href="/integrations"
                aria-current={pathname === '/integrations' ? 'page' : undefined}
                className={`${ITEM} ${pathname === '/integrations' ? 'font-medium' : ''}`}
                onClick={() => close(false)}
                {...plain}
              >
                <GearIcon className="h-4 w-4 shrink-0 text-muted" />
                <span className="min-w-0 flex-1 truncate">{t('settings.title')}</span>
              </Link>
              <div role="separator" className={SEPARATOR} />
              {parentItem(
                'appearance',
                <ContrastIcon className="h-4 w-4 shrink-0" />,
                t('settings.appearance.title'),
              )}
              {parentItem(
                'language',
                <GlobeIcon className="h-4 w-4 shrink-0" />,
                t('settings.language.title'),
              )}
              {hasHelp
                ? parentItem('help', <HelpIcon className="h-4 w-4 shrink-0" />, t('nav.help'))
                : null}
              <div role="separator" className={SEPARATOR} />
              <button
                type="button"
                role="menuitem"
                className={ITEM}
                onClick={() => void handleLogout()}
                {...plain}
              >
                <LogOutIcon className="h-4 w-4 shrink-0 text-muted" />
                <span className="min-w-0 flex-1 truncate">{t('nav.logOut')}</span>
              </button>
            </div>
            {submenu ? (
              <div
                ref={submenuPanel}
                role="menu"
                aria-label={
                  submenu === 'appearance'
                    ? t('settings.appearance.title')
                    : submenu === 'language'
                      ? t('settings.language.title')
                      : t('nav.help')
                }
                style={{
                  width: 'max-content',
                  minWidth: Math.min(SUBMENU_WIDTH, window.innerWidth - EDGE * 2),
                  maxWidth: Math.min(SUBMENU_MAX_WIDTH, window.innerWidth - EDGE * 2),
                  ...(submenuPosition ?? { left: -9999, top: -9999 }),
                }}
                className={PANEL}
                onKeyDown={(event) => {
                  if (event.key === 'Escape' || event.key === 'ArrowLeft') {
                    event.preventDefault();
                    event.stopPropagation();
                    closeSubmenu();
                  } else if (event.key === 'Tab') {
                    close(false);
                  } else if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
                    event.preventDefault();
                    moveFocus(submenuPanel.current, event.key);
                  }
                }}
              >
                {submenu === 'appearance' &&
                  THEME_PREFERENCES.map((option) => {
                    const Icon = THEME_ICONS[option];
                    return (
                      <button
                        key={option}
                        type="button"
                        role="menuitemradio"
                        aria-checked={preference === option}
                        tabIndex={-1}
                        className={ITEM}
                        onClick={() => setPreference(option)}
                      >
                        <Icon className="h-4 w-4 shrink-0 text-muted" />
                        <span className="min-w-0 flex-1 truncate">
                          {t(THEME_LABEL_KEYS[option])}
                        </span>
                        {preference === option ? (
                          <CheckIcon className="h-4 w-4 shrink-0 text-accent" />
                        ) : null}
                      </button>
                    );
                  })}
                {submenu === 'language' &&
                  LOCALES.map((option) => (
                    <button
                      key={option}
                      type="button"
                      role="menuitemradio"
                      aria-checked={locale === option}
                      tabIndex={-1}
                      lang={option}
                      className={ITEM}
                      onClick={() => setLocale(option)}
                    >
                      <span className="min-w-0 flex-1 truncate">{LOCALE_LABELS[option]}</span>
                      {locale === option ? (
                        <CheckIcon className="h-4 w-4 shrink-0 text-accent" />
                      ) : null}
                    </button>
                  ))}
                {submenu === 'help' ? (
                  <>
                    {/* Hidden rather than disabled when unset: an empty `href`
                        would reopen the current page in a new tab. */}
                    {branding.docsUrl ? (
                      <a
                        role="menuitem"
                        tabIndex={-1}
                        href={branding.docsUrl}
                        target="_blank"
                        rel="noopener noreferrer"
                        className={ITEM}
                        onClick={() => close(false)}
                      >
                        <BookIcon className="h-4 w-4 shrink-0 text-muted" />
                        <span className="min-w-0 flex-1 truncate">{docsLabel}</span>
                        <ExternalIcon className="h-3.5 w-3.5 shrink-0 text-muted" />
                      </a>
                    ) : null}
                    {branding.contactUrl ? (
                      <a
                        role="menuitem"
                        tabIndex={-1}
                        href={branding.contactUrl}
                        // A mail link opens the mail client; a new tab for it
                        // would be left blank.
                        {...(branding.contactUrl.startsWith('mailto:')
                          ? {}
                          : { target: '_blank', rel: 'noopener noreferrer' })}
                        className={ITEM}
                        onClick={() => close(false)}
                      >
                        <MailIcon className="h-4 w-4 shrink-0 text-muted" />
                        <span className="min-w-0 flex-1 truncate">{t('nav.contact')}</span>
                        {branding.contactUrl.startsWith('mailto:') ? null : (
                          <ExternalIcon className="h-3.5 w-3.5 shrink-0 text-muted" />
                        )}
                      </a>
                    ) : null}
                  </>
                ) : null}
              </div>
            ) : null}
          </>,
          document.body,
        )}
    </>
  );
}
