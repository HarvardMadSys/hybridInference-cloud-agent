'use client';

import type { ReactNode } from 'react';
import { PublicPageFrame } from '@/components/PublicPageFrame';
import { useT } from '@/components/providers/LocaleProvider';

/** An illustration of a terminal command branching into a code change. */
function AuthArtwork() {
  return (
    <svg
      aria-hidden="true"
      className="mx-auto w-full max-w-[360px]"
      viewBox="0 0 360 286"
      fill="none"
    >
      <g stroke="#eea9bc" strokeOpacity=".24">
        <circle cx="180" cy="143" r="130" strokeDasharray="2 5" />
        <circle cx="180" cy="143" r="102" />
        <path d="M180 13v260M50 143h260" strokeDasharray="2 5" />
      </g>
      <g stroke="#efacbf" strokeWidth="1.5">
        <path d="M112 151v50a14 14 0 0 0 14 14h114" />
        <path d="M112 179h60a14 14 0 0 0 14-14V98a14 14 0 0 1 14-14h66" />
        <circle cx="112" cy="179" r="4" fill="#f8c1cf" />
        <circle cx="250" cy="84" r="4" fill="#f8c1cf" />
        <circle cx="157" cy="215" r="4" fill="#f8c1cf" />
      </g>
      <rect x="56" y="67" width="174" height="91" rx="12" fill="#21121b" opacity=".42" />
      <rect x="51" y="60" width="174" height="91" rx="12" fill="#502a3b" stroke="#ad7088" />
      <path d="M51 85h174" stroke="#ad7088" strokeOpacity=".65" />
      <g fill="#e6a8bb">
        <circle cx="65" cy="73" r="2" />
        <circle cx="73" cy="73" r="2" opacity=".65" />
        <circle cx="81" cy="73" r="2" opacity=".4" />
      </g>
      <path
        d="m70 109 7 7-7 7m15 0h9"
        stroke="#ffe2ea"
        strokeWidth="2.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path d="M107 116h66m-66 12h43" stroke="#d98aa3" strokeWidth="3" strokeLinecap="round" />
      <rect x="218" y="187" width="82" height="68" rx="12" fill="#21121b" opacity=".42" />
      <rect x="213" y="180" width="82" height="68" rx="12" fill="#6a3549" stroke="#be7f95" />
      <g stroke="#fdd3df" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round">
        <path d="M246 208v17m0-9h6a9 9 0 0 0 9-9v-4" />
        <circle cx="246" cy="204" r="3.5" fill="#6a3549" />
        <circle cx="246" cy="228" r="3.5" fill="#6a3549" />
        <circle cx="261" cy="200" r="3.5" fill="#6a3549" />
      </g>
    </svg>
  );
}

export function AuthFormFrame({
  title,
  description,
  steps,
  longForm = false,
  children,
}: {
  title: string;
  description?: string;
  steps?: ReactNode;
  longForm?: boolean;
  children: ReactNode;
}) {
  const t = useT();
  return (
    <PublicPageFrame className="bg-surface sm:bg-surface-muted">
      <main className="mx-auto flex w-full max-w-[1024px] flex-1 items-center px-2.5 py-8 min-[360px]:px-5 sm:px-8 sm:py-12">
        <div className="mx-auto grid w-full max-w-[480px] bg-surface sm:rounded-[24px] sm:border sm:border-border sm:shadow-[0_16px_44px_-30px_rgb(var(--ui-foreground)/0.3)] min-[900px]:max-w-none min-[900px]:grid-cols-[44%_1fr] min-[900px]:p-1.5">
          <div
            className={`hidden flex-col rounded-[18px] bg-[radial-gradient(ellipse_at_40%_20%,#583044_0%,#301923_68%,#27141e_100%)] px-8 py-9 text-[#fff2f6] min-[900px]:flex xl:px-10 ${longForm ? '' : 'justify-center'}`}
          >
            <div className={longForm ? 'sticky top-12' : undefined}>
              <AuthArtwork />
              <p className="mt-4 text-balance text-[30px] font-semibold leading-tight tracking-[-0.04em]">
                {t('auth.artworkTitle')}
              </p>
              <p className="mt-3 text-pretty text-sm leading-6 text-[#e6bccc]">
                {t('auth.artworkDescription')}
              </p>
            </div>
          </div>
          <section
            aria-labelledby="auth-heading"
            className="account-form min-w-0 self-center px-0 py-4 sm:px-10 sm:py-10 min-[900px]:px-12 min-[900px]:py-12"
          >
            {steps}
            <h2
              id="auth-heading"
              className="text-[32px] font-semibold leading-tight tracking-[-0.04em] text-foreground"
            >
              {title}
            </h2>
            {description && <p className="mt-2 text-sm leading-6 text-muted">{description}</p>}
            <div className="mt-7 space-y-5">{children}</div>
          </section>
        </div>
      </main>
    </PublicPageFrame>
  );
}
