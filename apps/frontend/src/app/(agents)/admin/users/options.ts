// The states an account can be filtered by, and how each one is labelled, in
// the administrator's language.

import type { TranslationKey, Translator } from '@/lib/i18n/translate';
import type { UserFilters } from './filters';

export const FILTERS = [
  { value: '', label: 'adminAccounts.filter.all' },
  { value: 'pending', label: 'adminAccounts.filter.pending' },
  { value: 'approved', label: 'adminAccounts.filter.approved' },
  { value: 'rejected', label: 'adminAccounts.filter.rejected' },
  { value: 'revoked', label: 'adminAccounts.filter.revoked' },
  { value: 'not_applied', label: 'adminAccounts.filter.notApplied' },
] as const;
export const STATES = [
  { value: '', label: 'adminAccounts.state.any' },
  { value: 'active', label: 'adminAccounts.state.active' },
  { value: 'suspended', label: 'adminAccounts.state.suspended' },
  { value: 'deleted', label: 'adminAccounts.state.deleted' },
] as const;
export const SORTS = [
  { value: 'created', label: 'adminAccounts.sort.created' },
  { value: 'last_login', label: 'adminAccounts.sort.lastLogin' },
] as const;

/** The counts above the table; each one shows the accounts it counts. */
export const COUNT_VIEWS: {
  key: string;
  label: TranslationKey;
  status: UserFilters['status'];
  state: UserFilters['state'];
}[] = [
  { key: 'all', label: 'adminAccounts.count.all', status: '', state: '' },
  { key: 'admission:pending', label: 'adminAccounts.count.waiting', status: 'pending', state: '' },
  {
    key: 'admission:approved',
    label: 'adminAccounts.count.approved',
    status: 'approved',
    state: '',
  },
  {
    key: 'status:suspended',
    label: 'adminAccounts.count.suspended',
    status: '',
    state: 'suspended',
  },
  { key: 'status:deleted', label: 'adminAccounts.count.deleted', status: '', state: 'deleted' },
];

/** The options with their labels in the administrator's language. */
export function translated<T extends { value: string; label: TranslationKey }>(
  options: readonly T[],
  t: Translator,
): { value: T['value']; label: string }[] {
  return options.map((option) => ({ value: option.value, label: t(option.label) }));
}

/** A state's own label, the one its filter shows; the code itself for one it does not know. */
export function stateLabel(
  options: readonly { value: string; label: TranslationKey }[],
  value: string,
  t: Translator,
) {
  const option = options.find((candidate) => candidate.value === value);
  return option ? t(option.label) : value.replaceAll('_', ' ');
}
