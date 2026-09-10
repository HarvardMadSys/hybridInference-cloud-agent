'use client';

import { useEffect, useRef } from 'react';

/** Load the next page as its boundary approaches the scroll viewport. */
export function ArchiveAutoLoad({ onLoad }: { onLoad: () => void }) {
  const boundary = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const element = boundary.current;
    if (!element) return;
    let triggered = false;
    const observer = new IntersectionObserver(
      (entries) => {
        if (triggered || !entries.some((entry) => entry.isIntersecting)) return;
        triggered = true;
        observer.disconnect();
        onLoad();
      },
      { rootMargin: '200px' },
    );
    observer.observe(element);
    return () => observer.disconnect();
  }, [onLoad]);

  return <div ref={boundary} aria-hidden="true" className="h-px" />;
}
