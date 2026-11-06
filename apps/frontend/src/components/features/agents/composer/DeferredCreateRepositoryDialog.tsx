'use client';

import { lazy, Suspense, useState, type ComponentProps } from 'react';
import { useT } from '@/components/providers';
import { ComposerDialogLoading } from './ComposerDialogLoading';

type Props = ComponentProps<typeof import('./CreateRepositoryDialog').CreateRepositoryDialog>;

const CreateRepositoryDialog = lazy(() =>
  import('./CreateRepositoryDialog').then((module) => ({ default: module.CreateRepositoryDialog })),
);

/** Load on first use, then retain the creation receipt for access retries. */
export function DeferredCreateRepositoryDialog(props: Props) {
  const t = useT();
  const [opened, setOpened] = useState(props.open);
  if (props.open && !opened) setOpened(true);
  if (!props.open && !opened) return null;
  return (
    <Suspense
      fallback={
        props.open ? (
          <ComposerDialogLoading title={t('repositoryCreate.title')} onClose={props.onClose} />
        ) : null
      }
    >
      <CreateRepositoryDialog {...props} />
    </Suspense>
  );
}
