import { redirect } from 'next/navigation';
import { SETTINGS_PATHS } from '@/components/features/settings/navigation';

export default function SettingsIndexPage() {
  redirect(SETTINGS_PATHS.profile);
}
