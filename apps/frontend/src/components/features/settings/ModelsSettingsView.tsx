'use client';

import { useT } from '@/components/providers';
import { ModelConnectionsSettings } from './ModelConnectionsSettings';
import { ProvidedModelsCard, useProvidedSources } from './ProvidedModelsCard';
import { SettingsPage } from './SettingsPage';

/**
 * The models an owner's tasks can run on. Where the deployment provides some,
 * they come first, and the owner's own connections fold away beneath them: a
 * key of one's own is then the exception. Where it provides none, the
 * connections are the page. Nothing below the heading is drawn until it is
 * known which, so the section never folds under the reader.
 */
export function ModelsSettingsView() {
  const t = useT();
  const provided = useProvidedSources();
  return (
    <SettingsPage title={t('settings.models.title')} description={t('settings.models.description')}>
      {provided ? (
        <>
          <ProvidedModelsCard sources={provided} />
          <ModelConnectionsSettings folded={provided.length > 0} />
        </>
      ) : null}
    </SettingsPage>
  );
}
