import { FirstRunSetup } from '@/components/features/auth/FirstRunSetup';
import { LocalAccountGate } from '@/components/features/auth/LocalAccountGate';
export default function Page() {
  return (
    <LocalAccountGate>
      <FirstRunSetup />
    </LocalAccountGate>
  );
}
