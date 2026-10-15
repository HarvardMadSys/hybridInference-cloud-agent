/** Small status marks shared by the invitation states. */
export function TeamJoinIcon({
  kind = 'invite',
}: {
  kind?: 'invite' | 'pending' | 'approved' | 'closed';
}) {
  return (
    <svg
      width="24"
      height="24"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {kind === 'invite' ? (
        <>
          <path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75" />
          <circle cx="9" cy="7" r="4" />
        </>
      ) : (
        <>
          <circle cx="12" cy="12" r="9" />
          {kind === 'pending' ? (
            <path d="M12 7v5l3 2" />
          ) : kind === 'approved' ? (
            <path d="m8 12 3 3 5-6" />
          ) : (
            <path d="m9 9 6 6m0-6-6 6" />
          )}
        </>
      )}
    </svg>
  );
}
