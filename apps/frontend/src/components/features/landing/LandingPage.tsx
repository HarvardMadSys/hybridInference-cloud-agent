'use client';

// What a signed-out visitor sees at the app's root: what Cloud Agent is, the
// product doing one task, and the way in. Every other protected route still
// sends a signed-out visitor to `/login`; only the root has something to show
// before there is an account.

import Link from 'next/link';
import { useAuthOptionsState } from '@/components/features/auth/useAuthOptions';
import { useSiteConfig } from '@/components/providers/SiteConfigProvider';
import { useT } from '@/components/providers/LocaleProvider';
import { BrandMark } from '@/components/BrandMark';
import { DeploymentFooter } from '@/components/DeploymentFooter';
import { SignedOutControls } from '@/components/SignedOutControls';
import { LandingHero } from './LandingHero';
import {
  AgentStrip,
  ClosingSection,
  ContinuitySection,
  ReviewSection,
  StackSection,
} from './LandingSections';

const PRIMARY =
  'ui-focus-ring inline-flex h-[46px] items-center justify-center rounded-lg bg-accent px-[22px] text-[15px] font-medium text-accent-foreground shadow-subtle transition-colors hover:bg-accent-hover';
const SECONDARY =
  'ui-focus-ring inline-flex h-[46px] items-center justify-center rounded-lg border px-[22px] text-[15px] font-medium transition-colors';

/** The way in: sign up when this deployment takes sign-ups, sign in always. */
function AccountActions({
  signupOpen,
  loading = false,
  onDark = false,
}: {
  signupOpen: boolean;
  loading?: boolean;
  onDark?: boolean;
}) {
  const t = useT();
  const secondary = onDark
    ? `${SECONDARY} border-white/25 text-white hover:bg-white/10 dark:hover:bg-surface-hover`
    : `${SECONDARY} border-border-strong/60 bg-surface text-foreground hover:bg-surface-hover`;
  if (loading) {
    return (
      <span
        aria-hidden="true"
        className={`inline-flex h-[46px] w-[108px] rounded-lg ${
          onDark ? 'bg-white/10' : 'bg-surface-hover'
        }`}
      />
    );
  }
  if (!signupOpen)
    return (
      <Link href="/login" className={PRIMARY}>
        {t('landing.signIn')}
      </Link>
    );
  return (
    <>
      <Link href="/signup" className={PRIMARY}>
        {t('landing.signUp')}
      </Link>
      {onDark && (
        <Link href="/login" className={secondary}>
          {t('landing.signIn')}
        </Link>
      )}
    </>
  );
}

export function LandingPage() {
  const t = useT();
  const { branding } = useSiteConfig();
  const { options, loading } = useAuthOptionsState();
  const signupOpen = Boolean(options?.signup_enabled || options?.phone_signup_enabled);
  return (
    <div className="flex min-h-screen flex-col overflow-x-clip bg-surface text-foreground">
      <header className="relative z-[3] mx-auto flex h-16 w-full max-w-[1200px] items-center justify-between gap-4 px-[18px] sm:px-7">
        <Link
          href="/"
          className="ui-focus-ring inline-flex min-w-0 items-center gap-2.5 rounded-md text-sm font-semibold tracking-tight"
        >
          <BrandMark />
          <span className="truncate">{branding.appName}</span>
        </Link>
        <div className="flex shrink-0 items-center gap-2">
          <SignedOutControls />
          <span aria-hidden="true" className="h-4 w-px bg-border" />
          <nav aria-label={t('landing.nav')} className="flex shrink-0 items-center gap-1.5">
            <Link
              href="/login"
              className="ui-focus-ring rounded-md px-2 py-2 text-sm text-muted transition-colors hover:text-foreground sm:px-3"
            >
              {t('landing.signIn')}
            </Link>
            {!loading && signupOpen && (
              <Link
                href="/signup"
                className="ui-focus-ring inline-flex h-9 items-center rounded-lg bg-accent px-3.5 text-sm font-medium text-accent-foreground transition-colors hover:bg-accent-hover"
              >
                {t('landing.signUp')}
              </Link>
            )}
          </nav>
        </div>
      </header>
      <main className="flex-1">
        <LandingHero actions={<AccountActions signupOpen={signupOpen} loading={loading} />} />
        <AgentStrip />
        <ContinuitySection />
        <StackSection />
        <ReviewSection />
        <ClosingSection
          signupOpen={signupOpen}
          actions={<AccountActions signupOpen={signupOpen} loading={loading} onDark />}
        />
      </main>
      <DeploymentFooter />
    </div>
  );
}
