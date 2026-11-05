import { redirect } from 'next/navigation';

/**
 * Accounts are the People view of Admin → Users now. Old addresses, including
 * the links already mailed to administrators, open the same page there.
 */
export default async function AccountsAddressPage({
  params,
}: {
  params: Promise<{ path?: string[] }>;
}) {
  const { path = [] } = await params;
  redirect(['/admin/users', ...path.map(encodeURIComponent)].join('/'));
}
