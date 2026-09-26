'use client';

import Link from 'next/link';
import { useRouter, useSearchParams } from 'next/navigation';
import { Suspense, useEffect, useRef, useState } from 'react';
import {
  callbackAlreadyUsed,
  markCallbackUsed,
  takeReturnPath,
} from '@/components/features/agents/githubReturn';
import { connectGitHub, connectGitLab } from '@/lib/api/agents';
import type { AgentIntegrationProvider } from '@/lib/api/agents';

function ConnectedInner() {
  const router = useRouter();
  const params = useSearchParams();
  const connectStarted = useRef(false);
  const [error, setError] = useState<string | null>(null);
  const [install, setInstall] = useState<{ url: string; alreadyReaches: boolean } | null>(null);
  const providerParam = params?.get('provider');
  const provider: AgentIntegrationProvider = providerParam === 'gitlab' ? 'gitlab' : 'github';
  const providerName = provider === 'gitlab' ? 'GitLab' : 'GitHub';
  const code = params?.get('code');
  const state = params?.get('state');
  const oauthError = params?.get('error_description') ?? params?.get('error');

  useEffect(() => {
    // React intentionally replays effects in development Strict Mode. OAuth
    // state is single-use, so submitting twice would turn a successful first
    // request into a misleading replay error from the second one.
    if (connectStarted.current) return;
    connectStarted.current = true;

    if (oauthError) {
      setError(oauthError);
      return;
    }
    if (!code) {
      setError(`${providerName} did not return an authorization code.`);
      return;
    }
    if (!state) {
      setError(`${providerName} did not return the connection state.`);
      return;
    }

    // Opened again after it already worked — Back from GitHub, a refresh.
    // Sending the spent code again would only be refused; carry on instead.
    if (callbackAlreadyUsed(state)) {
      router.replace(takeReturnPath() ?? `/integrations?connected=${provider}`);
      return;
    }

    async function completeConnection(authCode: string, authState: string) {
      if (provider === 'github') {
        const connection = await connectGitHub(authCode, authState);
        markCallbackUsed(authState);
        if (connection.install_url) {
          // The App is not on the owner's own account yet — perhaps on no
          // account at all. Where the owner started stays noted: installing
          // comes back through this page.
          setInstall({
            url: connection.install_url,
            alreadyReaches: connection.connections.length > 0,
          });
          return;
        }
        // Back to where the trip started — a task half written in the
        // composer — rather than to the Integrations page every time.
        const back = takeReturnPath();
        if (back) {
          router.replace(back);
          return;
        }
      } else {
        await connectGitLab(authCode, authState);
        markCallbackUsed(authState);
      }
      router.replace(`/integrations?connected=${provider}`);
    }

    void completeConnection(code, state).catch((cause: unknown) =>
      setError(cause instanceof Error ? cause.message : 'could not complete the connection'),
    );
  }, [code, oauthError, provider, providerName, router, state]);

  if (error) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3">
        <h1 className="text-xl font-semibold text-gray-900 dark:text-gray-50">
          Could not connect {providerName}
        </h1>
        <p className="max-w-md text-center text-sm text-gray-500 dark:text-gray-400">{error}</p>
        <Link
          href="/integrations"
          className="text-sm font-medium text-crimson dark:text-crimson-light hover:underline"
        >
          Back to integrations
        </Link>
      </div>
    );
  }

  if (install) {
    // Connected already reaches an organization's repositories when that
    // organization installed the App; the owner's own repositories need an
    // installation of their own. Asked here because it was nowhere else: a
    // member of such an organization used to skip this page entirely.
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3">
        <h1 className="text-xl font-semibold text-gray-900 dark:text-gray-50">
          {install.alreadyReaches ? 'Use your own repositories too' : 'Install the GitHub App'}
        </h1>
        <p className="max-w-md text-center text-sm text-gray-500 dark:text-gray-400">
          {install.alreadyReaches
            ? 'GitHub is connected, and the agent can already use repositories an organization gave the App. To choose repositories from your own account, install the App there too.'
            : 'GitHub authorization succeeded, but this account has no GitHub App installation yet. Install it on your personal account or an organization, and choose the repositories the agent may work on.'}
        </p>
        <div className="flex items-center gap-3">
          <a
            href={install.url}
            className="rounded-lg bg-crimson px-4 py-2 text-sm font-medium text-white hover:bg-crimson-dark"
          >
            {install.alreadyReaches ? 'Install on my account' : 'Install GitHub App'}
          </a>
          <button
            type="button"
            onClick={() => router.replace(takeReturnPath() ?? '/integrations?connected=github')}
            className="rounded-lg px-4 py-2 text-sm font-medium text-gray-600 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-white/10"
          >
            Not now
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-full items-center justify-center">
      <p className="text-sm text-gray-500 dark:text-gray-400">Connecting {providerName}…</p>
    </div>
  );
}

export default function AgentsConnectedPage() {
  return (
    <Suspense fallback={<div className="flex h-full items-center justify-center" />}>
      <ConnectedInner />
    </Suspense>
  );
}
