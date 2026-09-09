'use client';

// Deployment identity for the shell.
//
// Recreated, not moved. The source provider fetched `/site-config` from the
// gateway at runtime so one build could serve several deployments. That
// endpoint belongs to the gateway and is not part of either contract this
// service consumes, so the values come from build-time environment instead —
// the same variables, one fewer network call, and nothing to fail open.
//
// The shape is unchanged (`branding` + `features`) because the moved layout
// destructures it.

import { createContext, useContext } from 'react';
import { config, gatewayUrl } from '@/config/env';

export interface RuntimeSiteConfig {
  branding: {
    appName: string;
    orgName: string;
    orgUrl: string;
    statusUrl: string;
    /** Origin of the gateway, for the links this app does not host itself. */
    gatewayUrl: string;
  };
  features: {
    /** The gateway's docs assistant. Off unless a deployment says otherwise. */
    rag: boolean;
  };
}

function gatewayOrigin(): string {
  if (!gatewayUrl) return '';
  try {
    return new URL(gatewayUrl).origin;
  } catch {
    return '';
  }
}

export const buildTimeSiteConfig: RuntimeSiteConfig = {
  branding: {
    appName: config.appName,
    orgName: process.env.NEXT_PUBLIC_ORG_NAME || '',
    orgUrl: process.env.NEXT_PUBLIC_ORG_URL || '',
    statusUrl: process.env.NEXT_PUBLIC_STATUS_URL || '',
    gatewayUrl: gatewayOrigin(),
  },
  features: {
    rag: process.env.NEXT_PUBLIC_FEATURE_RAG === 'true',
  },
};

const SiteConfigContext = createContext<RuntimeSiteConfig>(buildTimeSiteConfig);

export function SiteConfigProvider({ children }: { children: React.ReactNode }) {
  return (
    <SiteConfigContext.Provider value={buildTimeSiteConfig}>{children}</SiteConfigContext.Provider>
  );
}

export function useSiteConfig(): RuntimeSiteConfig {
  return useContext(SiteConfigContext);
}

export function useBranding(): RuntimeSiteConfig['branding'] {
  return useSiteConfig().branding;
}
