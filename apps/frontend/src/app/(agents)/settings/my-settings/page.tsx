import { LegacyModelConnectionsRedirect } from '@/components/features/settings/LegacyModelConnectionsRedirect';
import { MySettingsView } from '@/components/features/settings/MySettingsView';

export default function MySettingsPage() {
  return (
    <>
      <LegacyModelConnectionsRedirect />
      <MySettingsView />
    </>
  );
}
