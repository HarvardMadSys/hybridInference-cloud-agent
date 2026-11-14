'use client';

import Link from 'next/link';
import type { ReactNode } from 'react';
import { MODEL_CONNECTIONS_HREF, requestModelConnections } from './navigation';

/**
 * A link that means "set up your own connection". It opens the section on a
 * Models page that is already drawn as well as on one it navigates to.
 */
export function ModelConnectionsLink({
  className,
  children,
}: {
  className?: string;
  children: ReactNode;
}) {
  return (
    <Link href={MODEL_CONNECTIONS_HREF} onClick={requestModelConnections} className={className}>
      {children}
    </Link>
  );
}
