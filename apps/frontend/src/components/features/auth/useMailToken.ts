'use client';
import { useEffect, useRef, useState } from 'react';
/** Links carry a fragment so credentials never enter server access logs/referrers. */
export function useMailToken() {
  const captured = useRef(false);
  const [token, setToken] = useState<string | null>(null);
  useEffect(() => {
    if (captured.current) return;
    captured.current = true;
    const value = new URLSearchParams(window.location.hash.slice(1)).get('token') ?? '';
    setToken(value);
    window.history.replaceState(null, '', window.location.pathname);
  }, []);
  return token;
}
