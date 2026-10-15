'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { useLocale, useT } from '@/components/providers';
import {
  createInvitation,
  decideJoinRequest,
  deleteTeam,
  invitationLink,
  leaveTeam,
  listInvitations,
  listJoinRequests,
  listMembers,
  newRequestKey,
  removeMember,
  renameTeam,
  revokeInvitation,
  setTeamMemberRole,
  transferOwnership,
  type Team,
  type TeamInvitation,
  type TeamJoinRequest,
  type TeamMember,
  type TeamPerson,
} from '@/lib/api/teams';
import { APIError } from '@/lib/utils/errors';
import { DANGER, GHOST, INPUT, SECONDARY, formatTeamTime, teamErrorMessage } from './teamUi';

type Confirming =
  | { kind: 'remove' | 'transfer' | 'promote' | 'demote'; userId: string }
  | { kind: 'leave' | 'delete' }
  | null;

export function TeamDetails({ team, onChanged }: { team: Team; onChanged: () => void }) {
  const t = useT();
  const owner = team.role === 'owner';
  const manager = owner || team.role === 'admin';
  const [members, setMembers] = useState<TeamMember[] | null>(null);
  const [requests, setRequests] = useState<TeamJoinRequest[]>([]);
  const [invitations, setInvitations] = useState<TeamInvitation[]>([]);
  const [loadFailed, setLoadFailed] = useState(false);
  const [reload, setReload] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState<Confirming>(null);
  const [link, setLink] = useState<string | null>(null);
  const [linkLost, setLinkLost] = useState(false);
  const inviteKey = useRef(newRequestKey());

  useEffect(() => {
    const controller = new AbortController();
    setLoadFailed(false);
    void Promise.all([
      listMembers(team.id, controller.signal),
      manager ? listJoinRequests(team.id, controller.signal) : Promise.resolve([]),
      manager ? listInvitations(team.id, controller.signal) : Promise.resolve([]),
    ])
      .then(([nextMembers, nextRequests, nextInvitations]) => {
        if (controller.signal.aborted) return;
        setMembers(nextMembers);
        setRequests(nextRequests);
        setInvitations(nextInvitations);
      })
      .catch((cause) => {
        if (controller.signal.aborted) return;
        setLoadFailed(true);
        // The role or membership this card was opened with is out of date:
        // re-read the list, since a retry here would ask again as that role.
        if (
          cause instanceof APIError &&
          (cause.code === 'forbidden' || cause.code === 'not_found')
        ) {
          onChanged();
        }
      });
    return () => controller.abort();
  }, [team.id, manager, reload, onChanged]);

  /** Run one change, then re-read this team and, when asked, the team list. */
  const run = useCallback(
    async (action: () => Promise<unknown>, { teamChanged = false } = {}) => {
      if (busy) return false;
      setBusy(true);
      setError(null);
      try {
        await action();
        setConfirming(null);
        setReload((value) => value + 1);
        if (teamChanged) onChanged();
        return true;
      } catch (cause) {
        setError(teamErrorMessage(t, cause));
        // A refusal can mean the page is stale; show what is true now.
        setReload((value) => value + 1);
        onChanged();
        setConfirming(null);
        return false;
      } finally {
        setBusy(false);
      }
    },
    [busy, onChanged, t],
  );

  if (loadFailed) {
    return (
      <div
        role="alert"
        className="mt-4 flex flex-wrap items-center gap-3 text-sm text-status-error"
      >
        {t('teams.loadFailed')}
        <button type="button" className={SECONDARY} onClick={() => setReload((v) => v + 1)}>
          {t('teams.retry')}
        </button>
      </div>
    );
  }
  if (members === null) {
    return (
      <p role="status" className="mt-4 text-sm text-muted">
        {t('teams.loading')}
      </p>
    );
  }

  return (
    <div className="space-y-6 pt-4" aria-busy={busy}>
      {error ? (
        <p role="alert" className="text-sm text-status-error">
          {error}
        </p>
      ) : null}

      {manager ? (
        <RenameTeam
          team={team}
          busy={busy}
          onRename={(name) => run(() => renameTeam(team, name), { teamChanged: true })}
        />
      ) : null}

      <section aria-labelledby={`members-${team.id}`}>
        <h3 id={`members-${team.id}`} className="text-sm font-medium text-foreground">
          {t('teams.members.title')}
        </h3>
        <ul className="mt-2 divide-y divide-border">
          {members.map((member) => (
            <li key={member.user_id} className="py-3">
              <div className="flex flex-wrap items-center gap-3">
                <Person person={member} />
                <span className="rounded-full bg-surface-muted px-2 py-0.5 text-xs text-muted">
                  {t(`teams.role.${member.role}`)}
                </span>
                {member.role !== 'owner' && (owner || (manager && member.role === 'member')) ? (
                  <div className="ml-auto flex flex-wrap gap-1">
                    {owner ? (
                      <>
                        <button
                          type="button"
                          className={GHOST}
                          disabled={busy}
                          onClick={() =>
                            setConfirming({
                              kind: member.role === 'admin' ? 'demote' : 'promote',
                              userId: member.user_id,
                            })
                          }
                        >
                          {t(
                            member.role === 'admin'
                              ? 'teams.members.revokeAdmin'
                              : 'teams.members.makeAdmin',
                          )}
                        </button>
                        <button
                          type="button"
                          className={GHOST}
                          disabled={busy}
                          onClick={() =>
                            setConfirming({ kind: 'transfer', userId: member.user_id })
                          }
                        >
                          {t('teams.members.makeOwner')}
                        </button>
                      </>
                    ) : null}
                    <button
                      type="button"
                      className={GHOST}
                      disabled={busy}
                      onClick={() => setConfirming({ kind: 'remove', userId: member.user_id })}
                    >
                      {t('teams.members.remove')}
                    </button>
                  </div>
                ) : null}
              </div>
              {confirming && 'userId' in confirming && confirming.userId === member.user_id ? (
                <Confirm
                  title={t(MEMBER_ACTIONS[confirming.kind].title, { name: nameOf(member) })}
                  description={t(MEMBER_ACTIONS[confirming.kind].help)}
                  confirmLabel={t(MEMBER_ACTIONS[confirming.kind].label)}
                  busy={busy}
                  onCancel={() => setConfirming(null)}
                  onConfirm={() =>
                    run(
                      () => {
                        if (confirming.kind === 'transfer') {
                          return transferOwnership(team, member.user_id);
                        }
                        if (confirming.kind === 'remove') {
                          return removeMember(team.id, member.user_id);
                        }
                        return setTeamMemberRole(
                          team,
                          member.user_id,
                          confirming.kind === 'promote' ? 'admin' : 'member',
                        );
                      },
                      { teamChanged: true },
                    )
                  }
                />
              ) : null}
            </li>
          ))}
        </ul>
      </section>

      {manager ? (
        <section aria-labelledby={`requests-${team.id}`}>
          <h3 id={`requests-${team.id}`} className="text-sm font-medium text-foreground">
            {t('teams.requests.title')}
          </h3>
          {requests.length === 0 ? (
            <p className="mt-2 text-sm text-muted">{t('teams.requests.empty')}</p>
          ) : (
            <ul className="mt-2 divide-y divide-border">
              {requests.map((request) => (
                <li key={request.id} className="flex flex-wrap items-center gap-3 py-3">
                  <Person person={request} />
                  <div className="ml-auto flex gap-2">
                    <button
                      type="button"
                      className={SECONDARY}
                      disabled={busy}
                      onClick={() =>
                        run(() => decideJoinRequest(team.id, request.id, true), {
                          teamChanged: true,
                        })
                      }
                    >
                      {t('teams.requests.approve')}
                    </button>
                    <button
                      type="button"
                      className={GHOST}
                      disabled={busy}
                      onClick={() => run(() => decideJoinRequest(team.id, request.id, false))}
                    >
                      {t('teams.requests.decline')}
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </section>
      ) : null}

      {manager ? (
        <section aria-labelledby={`invitations-${team.id}`}>
          <div className="flex flex-wrap items-center gap-3">
            <h3 id={`invitations-${team.id}`} className="text-sm font-medium text-foreground">
              {t('teams.invitations.title')}
            </h3>
            <button
              type="button"
              className={`${SECONDARY} ml-auto`}
              disabled={busy}
              onClick={() =>
                run(async () => {
                  setLink(null);
                  setLinkLost(false);
                  const created = await createInvitation(team.id, inviteKey.current);
                  inviteKey.current = newRequestKey();
                  if (created.token) setLink(invitationLink(created.token));
                  else setLinkLost(true);
                })
              }
            >
              {t('teams.invitations.create')}
            </button>
          </div>
          <p className="mt-1 text-xs leading-5 text-muted">{t('teams.invitations.help')}</p>
          {link ? <NewLink link={link} onDone={() => setLink(null)} /> : null}
          {linkLost ? (
            <p role="alert" className="mt-2 text-sm text-status-error">
              {t('teams.invitations.lost')}
            </p>
          ) : null}
          <InvitationList
            invitations={invitations}
            busy={busy}
            onRevoke={(invitation) => run(() => revokeInvitation(team.id, invitation.id))}
          />
        </section>
      ) : null}

      <section className="border-t border-border pt-4">
        {confirming?.kind === 'leave' || confirming?.kind === 'delete' ? (
          <Confirm
            title={t(confirming.kind === 'leave' ? 'teams.leave.confirm' : 'teams.delete.confirm', {
              team: team.name,
            })}
            description={t(confirming.kind === 'leave' ? 'teams.leave.help' : 'teams.delete.help')}
            confirmLabel={t(
              confirming.kind === 'leave' ? 'teams.leave.action' : 'teams.delete.action',
            )}
            busy={busy}
            onCancel={() => setConfirming(null)}
            onConfirm={() =>
              run(() => (confirming.kind === 'leave' ? leaveTeam(team.id) : deleteTeam(team)), {
                teamChanged: true,
              })
            }
          />
        ) : (
          <button
            type="button"
            className={DANGER}
            disabled={busy}
            onClick={() => setConfirming({ kind: owner ? 'delete' : 'leave' })}
          >
            {t(owner ? 'teams.delete.action' : 'teams.leave.action')}
          </button>
        )}
        {owner ? (
          <p className="mt-2 text-xs leading-5 text-muted">{t('teams.leave.ownerHelp')}</p>
        ) : null}
      </section>
    </div>
  );
}

const MEMBER_ACTIONS = {
  transfer: {
    title: 'teams.members.confirmTransfer',
    help: 'teams.members.confirmTransferHelp',
    label: 'teams.members.makeOwner',
  },
  remove: {
    title: 'teams.members.confirmRemove',
    help: 'teams.members.confirmRemoveHelp',
    label: 'teams.members.remove',
  },
  promote: {
    title: 'teams.members.confirmAdmin',
    help: 'teams.members.adminHelp',
    label: 'teams.members.makeAdmin',
  },
  demote: {
    title: 'teams.members.confirmRevokeAdmin',
    help: 'teams.members.revokeAdminHelp',
    label: 'teams.members.revokeAdmin',
  },
} as const;

function nameOf(person: TeamPerson): string {
  return person.display_name || person.email || person.user_id;
}

function Person({ person }: { person: TeamPerson & { you?: boolean } }) {
  const t = useT();
  return (
    <div className="flex min-w-0 items-center gap-3">
      {/* eslint-disable-next-line @next/next/no-img-element -- a server-generated data URL */}
      <img
        src={person.avatar_data_url}
        alt=""
        className="h-9 w-9 shrink-0 rounded-full bg-surface-muted"
      />
      <div className="min-w-0">
        <p className="truncate text-sm text-foreground">
          {nameOf(person)}
          {person.you ? <span className="text-muted"> {t('teams.members.you')}</span> : null}
        </p>
        {person.display_name && person.email && person.email !== person.display_name ? (
          <p className="truncate text-xs text-muted">{person.email}</p>
        ) : null}
      </div>
    </div>
  );
}

function RenameTeam({
  team,
  busy,
  onRename,
}: {
  team: Team;
  busy: boolean;
  onRename: (name: string) => Promise<boolean>;
}) {
  const t = useT();
  const [name, setName] = useState(team.name);
  useEffect(() => setName(team.name), [team.name]);
  const trimmed = name.trim();
  return (
    <form
      className="flex flex-col gap-2 sm:flex-row sm:items-end"
      onSubmit={(event) => {
        event.preventDefault();
        if (trimmed && trimmed !== team.name) void onRename(trimmed);
      }}
    >
      <label className="min-w-0 flex-1 text-sm text-foreground">
        {t('teams.rename.label')}
        <input
          className={`${INPUT} mt-1`}
          value={name}
          maxLength={80}
          disabled={busy}
          onChange={(event) => setName(event.target.value)}
        />
      </label>
      <button
        type="submit"
        className={SECONDARY}
        disabled={busy || !trimmed || trimmed === team.name}
      >
        {t('teams.rename.save')}
      </button>
    </form>
  );
}

function NewLink({ link, onDone }: { link: string; onDone: () => void }) {
  const t = useT();
  const [copied, setCopied] = useState(false);
  return (
    <div className="mt-3 rounded-lg border border-border bg-surface-muted p-3">
      <label className="text-sm font-medium text-foreground">
        {t('teams.invitations.linkLabel')}
        <input
          readOnly
          className={`${INPUT} mt-1 font-mono text-xs`}
          value={link}
          onFocus={(event) => event.target.select()}
        />
      </label>
      <p className="mt-1 text-xs leading-5 text-muted">{t('teams.invitations.linkHelp')}</p>
      <div className="mt-2 flex flex-wrap gap-2">
        <button
          type="button"
          className={SECONDARY}
          onClick={async () => {
            try {
              await navigator.clipboard.writeText(link);
              setCopied(true);
            } catch {
              setCopied(false);
            }
          }}
        >
          {copied ? t('teams.invitations.copied') : t('teams.invitations.copy')}
        </button>
        <button type="button" className={GHOST} onClick={onDone}>
          {t('teams.invitations.done')}
        </button>
      </div>
    </div>
  );
}

function InvitationList({
  invitations,
  busy,
  onRevoke,
}: {
  invitations: TeamInvitation[];
  busy: boolean;
  onRevoke: (invitation: TeamInvitation) => void;
}) {
  const t = useT();
  const { locale } = useLocale();
  if (invitations.length === 0) return null;
  return (
    <ul className="mt-3 divide-y divide-border">
      {invitations.map((invitation) => {
        const status =
          invitation.status === 'claimed' && invitation.request_state
            ? invitation.request_state
            : invitation.status;
        const revocable =
          invitation.status === 'open' ||
          (invitation.status === 'claimed' && invitation.request_state === 'pending');
        const expires = formatTeamTime(locale, invitation.expires_at);
        const expiry =
          invitation.status === 'open' ? t('teams.invitations.expires', { expires }) : null;
        return (
          <li key={invitation.id} className="flex flex-wrap items-center gap-3 py-2 text-sm">
            <span className="text-foreground">{t(`teams.invitations.status.${status}`)}</span>
            <span className="text-xs text-muted">
              {t('teams.invitations.created', {
                created: formatTeamTime(locale, invitation.created_at),
              })}
              {expiry ? ` · ${expiry}` : ''}
            </span>
            {revocable ? (
              <button
                type="button"
                className={`${GHOST} ml-auto`}
                disabled={busy}
                onClick={() => onRevoke(invitation)}
              >
                {t('teams.invitations.revoke')}
              </button>
            ) : null}
          </li>
        );
      })}
    </ul>
  );
}

function Confirm({
  title,
  description,
  confirmLabel,
  busy,
  onConfirm,
  onCancel,
}: {
  title: string;
  description: string;
  confirmLabel: string;
  busy: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const t = useT();
  return (
    <div
      role="group"
      aria-label={title}
      className="mt-2 rounded-lg border border-border bg-surface-muted p-3"
    >
      <p className="text-sm font-medium text-foreground">{title}</p>
      <p className="mt-1 text-xs leading-relaxed text-muted">{description}</p>
      <div className="mt-3 flex flex-wrap gap-2">
        <button type="button" className={DANGER} disabled={busy} onClick={onConfirm}>
          {confirmLabel}
        </button>
        <button type="button" className={GHOST} disabled={busy} onClick={onCancel}>
          {t('teams.cancel')}
        </button>
      </div>
    </div>
  );
}
