'use client';

// Outside the `(agents)` route group on purpose, like `/login`: the shell's
// sign-in redirect would drop the invitation link's fragment before this page
// could keep it. `JoinTeamView` explains the hand-off.

import { JoinTeamView } from '@/components/features/teams/JoinTeamView';

export default function JoinTeamPage() {
  return <JoinTeamView />;
}
