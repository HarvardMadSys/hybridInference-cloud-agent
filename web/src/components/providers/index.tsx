'use client';

// Recreated from the source repo's provider stack, minus `QueryProvider`: no
// moved file uses TanStack Query, so shipping it would be a dependency with no
// caller.

import type { RuntimeSiteConfig } from '@/config/site';
import { AuthProvider } from './AuthProvider';
import { LocaleProvider } from './LocaleProvider';
import { ThemeProvider } from './ThemeProvider';
import { ToastProvider } from './ToastProvider';
import { SiteConfigProvider } from './SiteConfigProvider';

export function Providers({
  siteConfig,
  children,
}: {
  siteConfig: RuntimeSiteConfig;
  children: React.ReactNode;
}) {
  return (
    <SiteConfigProvider config={siteConfig}>
      {/* Theme wraps the toaster because `react-hot-toast` is styled with a
          JS palette rather than classes, so it has to read the resolved
          scheme from context. */}
      <ThemeProvider>
        <LocaleProvider>
          <AuthProvider>
            {children}
            <ToastProvider />
          </AuthProvider>
        </LocaleProvider>
      </ThemeProvider>
    </SiteConfigProvider>
  );
}

export { useAuth } from './AuthProvider';
export { useLocale, useT } from './LocaleProvider';
export { useTheme } from './ThemeProvider';
