import { redirect } from 'next/navigation';

/** The cross-session turn list is now the Turns level of the Activity tab. */
export default function AllTurnsPage() {
  redirect('/admin/activity?level=turns');
}
