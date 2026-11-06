'use client';

import { useLayoutEffect, useRef } from 'react';

/** Restore the list after settings closes, including folders whose rows load later. */
export function useSidebarScroll(visible: boolean, userId?: string) {
  const element = useRef<HTMLDivElement>(null);
  const saved = useRef({ userId, top: 0 });
  useLayoutEffect(() => {
    if (saved.current.userId !== userId) saved.current = { userId, top: 0 };
    const node = element.current;
    if (!visible || !node) return;
    const target = saved.current.top;
    let restoring = target > 0;
    const observer = new MutationObserver(restore);
    function stopRestoring() {
      restoring = false;
      observer.disconnect();
    }
    function restore() {
      node!.scrollTop = target;
      if (node!.scrollTop >= target) stopRestoring();
    }
    function remember() {
      if (!restoring) saved.current.top = node!.scrollTop;
    }
    if (restoring) {
      observer.observe(node, { childList: true, subtree: true });
      restore();
    }
    node.addEventListener('scroll', remember);
    node.addEventListener('wheel', stopRestoring, { passive: true });
    node.addEventListener('touchstart', stopRestoring, { passive: true });
    node.addEventListener('keydown', stopRestoring);
    return () => {
      observer.disconnect();
      node.removeEventListener('scroll', remember);
      node.removeEventListener('wheel', stopRestoring);
      node.removeEventListener('touchstart', stopRestoring);
      node.removeEventListener('keydown', stopRestoring);
    };
  }, [visible, userId]);
  return element;
}
