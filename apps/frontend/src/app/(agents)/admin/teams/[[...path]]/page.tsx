import { redirect } from 'next/navigation';

/** Teams are a view of Admin → Users now; an old address opens the same team there. */
export default async function TeamsAddressPage({
  params,
}: {
  params: Promise<{ path?: string[] }>;
}) {
  const { path = [] } = await params;
  redirect(['/admin/users/teams', ...path.map(encodeURIComponent)].join('/'));
}
