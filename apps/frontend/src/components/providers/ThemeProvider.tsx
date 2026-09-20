'use client';

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import {
  DARK_MEDIA_QUERY,
  DEFAULT_THEME_PREFERENCE,
  applyTheme,
  readStoredTheme,
  resolveTheme,
  systemPrefersDark,
  writeStoredTheme,
  type ResolvedTheme,
  type ThemePreference,
} from '@/lib/theme';

interface ThemeContextValue {
  /** What the user picked: light, dark, or follow the OS. */
  preference: ThemePreference;
  /** What that means right now. Only ever `light` or `dark`. */
  resolved: ResolvedTheme;
  setPreference: (preference: ThemePreference) => void;
}

const ThemeContext = createContext<ThemeContextValue>({
  preference: DEFAULT_THEME_PREFERENCE,
  resolved: 'light',
  setPreference: () => {},
});

export function ThemeProvider({ children }: { children: React.ReactNode }) {
  // The server has no storage and no media query, so the first render — on the
  // server and again during hydration — has to be the default. The class on
  // <html> is already correct by then: the boot script in the root layout set
  // it before first paint. This state catches up in the effect below.
  const [preference, setPreferenceState] = useState<ThemePreference>(DEFAULT_THEME_PREFERENCE);
  const [prefersDark, setPrefersDark] = useState(false);

  useEffect(() => {
    setPreferenceState(readStoredTheme());
    setPrefersDark(systemPrefersDark());
  }, []);

  // `system` is a live subscription, not a one-time reading: someone whose OS
  // flips at sunset expects the app to flip with it, without a reload.
  useEffect(() => {
    let media: MediaQueryList;
    try {
      media = window.matchMedia(DARK_MEDIA_QUERY);
    } catch {
      return undefined;
    }
    const onChange = (event: MediaQueryListEvent) => setPrefersDark(event.matches);
    media.addEventListener('change', onChange);
    return () => media.removeEventListener('change', onChange);
  }, []);

  const resolved = useMemo(() => resolveTheme(preference, prefersDark), [preference, prefersDark]);

  useEffect(() => {
    applyTheme(resolved);
  }, [resolved]);

  const setPreference = useCallback((next: ThemePreference) => {
    setPreferenceState(next);
    writeStoredTheme(next);
  }, []);

  const value = useMemo(
    () => ({ preference, resolved, setPreference }),
    [preference, resolved, setPreference],
  );

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

/**
 * Read the theme.
 *
 * Styling should come from Tailwind's `dark:` variants, which are pure CSS and
 * therefore correct during SSR and hydration. Reach for this hook only where a
 * colour has to be handed to something that is not CSS — the terminal emulator
 * and the toast library both take a JS palette — and where the component only
 * mounts on the client anyway.
 */
export function useTheme(): ThemeContextValue {
  return useContext(ThemeContext);
}
