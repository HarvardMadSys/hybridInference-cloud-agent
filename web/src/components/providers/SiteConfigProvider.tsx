'use client';

// Deployment identity for the shell.
//
// The value is resolved on the server, once per request, and handed down as a
// prop — see `config/site.server.ts`. This component is only the transport:
// it exists so the shell's client components can read the identity through
// context without a second network call, and so the server and the first
// client render agree on it.
//
// It used to read `NEXT_PUBLIC_*` at module scope, which is the same thing as
// reading it during `next build`: the name and links of whichever deployment
// compiled the bundle were frozen into it.
//
// The context default is `NEUTRAL_SITE_CONFIG`, never a real deployment's
// brand. A component rendered outside the provider — a story, a test, a route
// added above the root layout — must show the generic identity rather than
// inherit one it was never given.

import { createContext, useContext } from 'react';

import { NEUTRAL_SITE_CONFIG, type RuntimeSiteConfig } from '@/config/site';

const SiteConfigContext = createContext<RuntimeSiteConfig>(NEUTRAL_SITE_CONFIG);

export function SiteConfigProvider({
  config,
  children,
}: {
  config: RuntimeSiteConfig;
  children: React.ReactNode;
}) {
  return <SiteConfigContext.Provider value={config}>{children}</SiteConfigContext.Provider>;
}

export function useSiteConfig(): RuntimeSiteConfig {
  return useContext(SiteConfigContext);
}

export function useBranding(): RuntimeSiteConfig['branding'] {
  return useSiteConfig().branding;
}
