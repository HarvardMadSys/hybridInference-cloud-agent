'use client';
// The second step of first-run setup: confirm the address people open this
// site at. It is the one required setting the deployment cannot work out on
// its own; account mail links, CSRF trusted origins and OAuth callbacks use it.
import { useEffect, useState, type FormEvent, type ReactNode } from 'react';
import { useT } from '@/components/providers/LocaleProvider';
import { getAgentAdminSettings, setAgentAdminSettings } from '@/lib/api/adminSettings';
import { getErrorMessage } from '@/lib/utils/errors';
import { AccountField, AccountFrame, AccountNotice } from './AccountFrame';

const KEY = 'web_base_url';

interface Loaded {
  revision: number;
  saved: string;
}

function savedValue(values: Record<string, unknown>): string {
  const value = values[KEY];
  return typeof value === 'string' ? value.trim() : '';
}

/**
 * The address to confirm: the saved one when there is one, else the address
 * this page is open at; and the revision a change is made against.
 */
function useSiteAddress() {
  const [value, setValue] = useState(() => window.location.origin);
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    getAgentAdminSettings(controller.signal)
      .then((snapshot) => {
        if (controller.signal.aborted) return;
        const saved = savedValue(snapshot.values);
        setLoaded({ revision: snapshot.revision, saved });
        if (saved) setValue(saved);
      })
      .catch((cause) => {
        if (!controller.signal.aborted) setError(getErrorMessage(cause));
      });
    return () => controller.abort();
  }, []);
  return { value, setValue, loaded, loadError: error };
}

export function SiteAddressStep({ steps, onDone }: { steps: ReactNode; onDone: () => void }) {
  const t = useT();
  const { value, setValue, loaded, loadError } = useSiteAddress();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  async function save(event: FormEvent) {
    event.preventDefault();
    const address = value.trim();
    if (loaded && loaded.saved === address) return onDone();
    setBusy(true);
    setError(null);
    try {
      const revision = loaded?.revision ?? (await getAgentAdminSettings()).revision;
      await setAgentAdminSettings({ [KEY]: address }, revision);
      onDone();
    } catch (cause) {
      setError(getErrorMessage(cause));
      setBusy(false);
    }
  }
  const shownError = error ?? loadError;
  return (
    <AccountFrame
      title={t('setup.siteTitle')}
      description={t('setup.siteDescription')}
      steps={steps}
    >
      <form className="space-y-4" onSubmit={save}>
        {shownError && <AccountNotice error>{shownError}</AccountNotice>}
        {loaded?.saved && <AccountNotice>{t('setup.siteSaved')}</AccountNotice>}
        <AccountField id="web-base-url" label={t('setup.siteUrl')} hint={t('setup.siteHint')}>
          <input
            id="web-base-url"
            className="ui-input w-full font-mono"
            type="url"
            inputMode="url"
            autoComplete="url"
            autoCapitalize="none"
            spellCheck={false}
            required
            aria-describedby="web-base-url-hint"
            value={value}
            onChange={(e) => setValue(e.target.value)}
          />
        </AccountField>
        <button className="ui-button ui-button-primary w-full" disabled={busy}>
          {busy ? t('account.saving') : t('setup.siteSave')}
        </button>
        <button
          type="button"
          className="ui-button ui-button-secondary w-full"
          disabled={busy}
          onClick={onDone}
        >
          {t('setup.siteSkip')}
        </button>
      </form>
    </AccountFrame>
  );
}
