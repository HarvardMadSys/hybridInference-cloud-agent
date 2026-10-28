import LoginPageClient from '@/components/features/auth/LoginPageClient';
import { readRuntimeAuthConfig } from '@/config/auth.server';

export default async function LoginPage() {
  return <LoginPageClient initialConfig={await readRuntimeAuthConfig()} />;
}
