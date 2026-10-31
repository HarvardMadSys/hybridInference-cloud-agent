'use client';
// Cloudflare Turnstile, adapted from HybridInference components/auth/TurnstileChallenge.tsx.
// Rendered only when the deployment configured a site key; the token it
// yields goes with the request it guards, and the API checks it. A token is
// good for one request, so the page bumps `resetKey` after every attempt and
// a fresh challenge replaces the spent one (HybridInference's captchaReset).
import { useEffect, useRef } from 'react';

const SCRIPT = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';

interface TurnstileApi {
  render: (element: HTMLElement, options: Record<string, unknown>) => string;
  remove: (id: string) => void;
}
declare global {
  interface Window {
    turnstile?: TurnstileApi;
  }
}

let loading: Promise<void> | null = null;
function loadTurnstile(): Promise<void> {
  if (window.turnstile) return Promise.resolve();
  loading ??= new Promise((resolve, reject) => {
    const script = document.createElement('script');
    script.src = SCRIPT;
    script.async = true;
    script.onload = () => resolve();
    script.onerror = () => {
      loading = null;
      reject(new Error('captcha unavailable'));
    };
    document.head.appendChild(script);
  });
  return loading;
}

export function TurnstileWidget({
  siteKey,
  onToken,
  resetKey = 0,
}: {
  siteKey: string;
  onToken: (token: string | null) => void;
  /** Changing it discards the current token and renders a new challenge. */
  resetKey?: number;
}) {
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    let widget: string | null = null;
    let live = true;
    onToken(null);
    void loadTurnstile()
      .then(() => {
        if (!live || !box.current || !window.turnstile) return;
        widget = window.turnstile.render(box.current, {
          sitekey: siteKey,
          callback: (token: string) => onToken(token),
          'expired-callback': () => onToken(null),
          'error-callback': () => onToken(null),
        });
      })
      .catch(() => onToken(null));
    return () => {
      live = false;
      if (widget && window.turnstile) window.turnstile.remove(widget);
    };
  }, [siteKey, onToken, resetKey]);
  return <div ref={box} data-testid="turnstile" />;
}
