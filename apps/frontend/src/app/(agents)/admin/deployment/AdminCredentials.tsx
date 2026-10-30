'use client';

import { useId, useState } from 'react';

import {
  type AdminCredential,
  createCredential,
  revokeCredential,
} from '@/lib/api/adminManagement';

import { scopeName, when } from './deploymentCopy';
import {
  BUTTON_CLASS,
  Empty,
  FieldRow,
  GroupCard,
  INPUT_CLASS,
  Pill,
  PRIMARY_CLASS,
  type Run,
  useConfirmation,
} from './deploymentUi';
import type { Revealed } from './SecurityKeys';

function usage(credential: AdminCredential): string {
  if (credential.revoked_at) return `Revoked ${when(credential.revoked_at)}`;
  const used = when(credential.last_used_at);
  const expires = when(credential.expires_at);
  return [used ? `Last used ${used}` : 'Never used', expires && `expires ${expires}`]
    .filter(Boolean)
    .join(' · ');
}

/** Named, revocable credentials for automation; the value is shown once, when created. */
export function AdminCredentials({
  credentials,
  run,
  reveal,
}: {
  credentials: AdminCredential[];
  run: Run;
  reveal: (secret: Revealed) => void;
}) {
  const id = useId();
  const [name, setName] = useState('');
  const [deployment, setDeployment] = useState(false);
  const [confirm, dialog] = useConfirmation();
  return (
    <GroupCard
      title="Administrator API credentials"
      icon="lock"
      description="Named credentials for automation, each revocable on its own. They cannot create other credentials, and change deployments only when allowed to. Each expires after 90 days."
    >
      <form
        onSubmit={(event) => {
          event.preventDefault();
          void run(async () => {
            const created = await createCredential(name, deployment, 90);
            setName('');
            setDeployment(false);
            reveal({
              title: `Credential “${created.name}”`,
              description: 'Shown only now. Store it where your automation reads it.',
              value: created.credential,
            });
            return `Credential “${created.name}” created.`;
          });
        }}
      >
        <FieldRow
          label="New credential"
          help="A name that says what uses it."
          htmlFor={`${id}-name`}
        >
          <div className="space-y-3">
            <input
              id={`${id}-name`}
              className={INPUT_CLASS}
              value={name}
              autoComplete="off"
              placeholder="Credential name, for example nightly-report"
              onChange={(event) => setName(event.target.value)}
            />
            <label className="flex cursor-pointer items-start gap-2.5 text-sm text-foreground">
              <input
                type="checkbox"
                className="mt-1 accent-[rgb(var(--ui-accent))]"
                checked={deployment}
                onChange={(event) => setDeployment(event.target.checked)}
              />
              <span>
                May change deployments
                <span className="block text-xs leading-5 text-muted">
                  Machines, releases, keys and the database on this page.
                </span>
              </span>
            </label>
            <button type="submit" className={PRIMARY_CLASS} disabled={!name.trim()}>
              Create credential
            </button>
          </div>
        </FieldRow>
      </form>
      {credentials.length === 0 ? (
        <Empty>No credential has been created.</Empty>
      ) : (
        <ul className="border-t border-border/70">
          {credentials.map((credential) => (
            <li
              key={credential.credential_id}
              className="flex flex-wrap items-center justify-between gap-3 border-b border-border/60 px-4 py-3.5 last:border-b-0 sm:px-5"
            >
              <div className="min-w-0">
                <p className="break-words text-sm font-medium text-foreground">{credential.name}</p>
                <p className="mt-1 text-xs leading-5 text-muted">{usage(credential)}</p>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                {credential.scopes.map((scope) => (
                  <Pill key={scope}>{scopeName(scope)}</Pill>
                ))}
                {credential.revoked_at ? (
                  <Pill tone="error">Revoked</Pill>
                ) : (
                  <button
                    type="button"
                    className={BUTTON_CLASS}
                    aria-label={`Revoke ${credential.name}`}
                    onClick={() =>
                      confirm({
                        title: `Revoke “${credential.name}”?`,
                        danger: true,
                        body: <p>Anything using it is refused from now on.</p>,
                        confirmLabel: 'Revoke',
                        action: () =>
                          void run(() =>
                            revokeCredential(credential.credential_id).then(
                              () => `Credential “${credential.name}” revoked.`,
                            ),
                          ),
                      })
                    }
                  >
                    Revoke
                  </button>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}
      {dialog}
    </GroupCard>
  );
}
