import { redirect } from 'next/navigation';

/** The cross-session turn list is now the Turns level of the Sessions tab. */
export default function AllTurnsPage() {
  redirect('/admin/sessions?level=turns');
}
