import type { UserProfile } from './userProfile';

/** A saved profile, so the account menu shows a new name or picture at once. */
export const PROFILE_SAVED = 'agents:profile-saved';

export function announceProfileSaved(profile: UserProfile): void {
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new CustomEvent(PROFILE_SAVED, { detail: profile }));
  }
}

export function onProfileSaved(listener: (profile: UserProfile) => void): () => void {
  const handle = (event: Event) => listener((event as CustomEvent<UserProfile>).detail);
  window.addEventListener(PROFILE_SAVED, handle);
  return () => window.removeEventListener(PROFILE_SAVED, handle);
}
