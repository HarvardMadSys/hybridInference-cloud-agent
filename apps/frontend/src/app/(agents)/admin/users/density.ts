'use client';

// How tall the People table's rows are, as HybridInference's users table:
// compact rows hold one line, comfortable ones a second. The choice stays in
// this browser; a value it cannot read, or storage it cannot use, is compact.

import { useCallback, useEffect, useState } from 'react';

export type Density = 'compact' | 'comfortable';
export const DENSITY_KEY = 'agents.admin.users.density';

function stored(): Density {
  try {
    return window.localStorage.getItem(DENSITY_KEY) === 'comfortable' ? 'comfortable' : 'compact';
  } catch {
    return 'compact';
  }
}

export function useDensity(): [Density, (density: Density) => void] {
  // Compact on the server and the first render; the stored choice after mount.
  const [density, setDensity] = useState<Density>('compact');
  useEffect(() => setDensity(stored()), []);
  const choose = useCallback((next: Density) => {
    setDensity(next);
    try {
      window.localStorage.setItem(DENSITY_KEY, next);
    } catch {
      // Private browsing or a full store: the choice lasts until reload.
    }
  }, []);
  return [density, choose];
}
