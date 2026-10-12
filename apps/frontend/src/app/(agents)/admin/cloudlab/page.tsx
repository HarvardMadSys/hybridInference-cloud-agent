import { redirect } from 'next/navigation';

export default function AdminCloudLabPage() {
  redirect('/admin/settings?tab=cloudlab');
}
