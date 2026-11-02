'use client';

import { useState, type ReactNode } from 'react';
import { useT } from '@/components/providers/LocaleProvider';
import type { TranslationKey } from '@/lib/i18n/translate';
import { AgentMark } from './AgentMarks';
import { AGENT_IDS, AGENT_NAMES } from './demoScript';
import { CheckIcon, PullRequestIcon } from './ProductWindow';

const SECTION = 'mx-auto max-w-[1200px] px-[18px] pt-[72px] sm:px-7 md:pt-[84px] lg:pt-28';
const HEADING =
  'mt-3.5 text-[clamp(30px,3.4vw,42px)] font-semibold leading-[1.1] tracking-[-0.035em] text-foreground';
const LEDE = 'mt-4 max-w-[420px] text-[16.5px] leading-relaxed text-muted';
const CARD = 'rounded-[10px] border border-border-strong/50 bg-surface shadow-card';
const MINI_BUTTON =
  'inline-flex h-6 items-center rounded-md border border-border-strong/60 px-2 text-[11.5px] font-medium';

function Eyebrow({ number, children }: { number?: string; children: ReactNode }) {
  return (
    <p className="font-mono text-xs tracking-[0.02em] text-muted">
      {number && <b className="font-medium text-accent">{number}</b>}
      {number && ' / '}
      {children}
    </p>
  );
}

export function AgentStrip() {
  const t = useT();
  return (
    <section
      aria-label={t('landing.agentsLabel')}
      className="mx-auto max-w-[1200px] px-[18px] sm:px-7"
    >
      <div className="grid items-center gap-4 border-y border-border py-[26px] min-[1101px]:grid-cols-[minmax(0,300px)_minmax(0,1fr)] min-[1101px]:gap-8">
        <p className="text-sm leading-normal text-muted">
          <b className="block font-medium text-foreground">{t('landing.agentsTitle')}</b>
          {t('landing.agentsDetail')}
        </p>
        <ul className="flex flex-wrap items-center gap-x-[22px] gap-y-3.5 min-[1101px]:justify-between">
          {AGENT_IDS.map((agent) => (
            <li
              key={agent}
              className="inline-flex items-center gap-2.5 whitespace-nowrap text-base font-semibold tracking-[-0.02em] text-foreground/85 sm:text-lg"
            >
              <AgentMark agent={agent} className="h-[19px] w-[19px] sm:h-[22px] sm:w-[22px]" />
              {AGENT_NAMES[agent]}
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
}

type Mode = 'follow' | 'fork';

const MODES: Record<
  Mode,
  { label: TranslationKey; text: TranslationKey; note: TranslationKey; keep: TranslationKey[] }
> = {
  follow: {
    label: 'landing.nextMessage',
    text: 'landing.nextMessageText',
    note: 'landing.followNote',
    keep: ['landing.sameFiles', 'landing.sameEnvironment', 'landing.sameSession'],
  },
  fork: {
    label: 'landing.secondConversation',
    text: 'landing.secondConversationText',
    note: 'landing.forkNote',
    keep: ['landing.sharedFiles', 'landing.sharedRepo', 'landing.ownAgent'],
  },
};

/** 01: a conversation keeps its workspace; a fork shares it. */
export function ContinuitySection() {
  const t = useT();
  const [mode, setMode] = useState<Mode>('follow');
  const shown = MODES[mode];
  return (
    <section
      aria-labelledby="landing-continue"
      className={`${SECTION} grid items-center gap-10 min-[1101px]:grid-cols-[minmax(0,1fr)_minmax(0,1.15fr)] min-[1101px]:gap-[72px]`}
    >
      <div>
        <Eyebrow number="01">{t('landing.continueEyebrow')}</Eyebrow>
        <h2 id="landing-continue" className={HEADING}>
          {t('landing.continueTitleLead')}
          <br />
          {t('landing.continueTitleTail')}
        </h2>
        <p className={LEDE}>{t('landing.continueLede')}</p>
        <div
          role="group"
          aria-label={t('landing.continueModes')}
          className="mt-[26px] inline-flex gap-0.5 rounded-[10px] bg-surface-hover p-[3px]"
        >
          {(['follow', 'fork'] as const).map((value) => (
            <button
              key={value}
              type="button"
              aria-pressed={mode === value}
              onClick={() => setMode(value)}
              className="ui-focus-ring h-8 rounded-lg px-[13px] text-[13.5px] text-muted transition-colors aria-pressed:bg-surface aria-pressed:font-medium aria-pressed:text-foreground aria-pressed:shadow-subtle"
            >
              {t(value === 'follow' ? 'landing.followUp' : 'landing.fork')}
            </button>
          ))}
        </div>
        <p
          aria-live="polite"
          className="mt-3.5 min-h-[66px] max-w-[420px] text-sm leading-relaxed text-muted"
        >
          {t(shown.note)}
        </p>
      </div>
      <div
        aria-hidden="true"
        className="rounded-[18px] border border-border bg-surface-muted p-5 sm:p-7"
      >
        <div className="flex justify-between gap-3 font-mono text-[11.5px] text-muted">
          <span>your-org/web</span>
          <span>{t('landing.workspaceKept')}</span>
        </div>
        <div className={`${CARD} mt-5 flex max-w-[420px] flex-col gap-1.5 px-4 py-3.5`}>
          <span className="flex justify-between font-mono text-[11px] uppercase text-muted">
            <span>{t('landing.firstTask')}</span>
            <span className="text-status-success">✓ 31s</span>
          </span>
          <p className="text-[14.5px] text-foreground">{t('landing.firstTaskText')}</p>
        </div>
        <div
          key={mode}
          className={`${CARD} relative ml-[34px] mt-5 flex max-w-[420px] animate-[landing-fade_.25s_ease] flex-col motion-reduce:animate-none gap-1.5 px-4 py-3.5 sm:ml-[52px]`}
        >
          <span
            className="absolute -left-6 -top-5 h-12 w-4 rounded-bl-[9px] border-b-[1.5px] border-l-[1.5px] border-dashed border-border-strong/70 sm:-left-8 sm:w-6"
            aria-hidden="true"
          />
          <span className="flex justify-between font-mono text-[11px] uppercase text-accent">
            <span>{t(shown.label)}</span>
            <span>↳</span>
          </span>
          <p className="text-[14.5px] text-foreground">{t(shown.text)}</p>
        </div>
        <div className="mt-6 flex flex-wrap gap-2">
          {shown.keep.map((key) => (
            <span
              key={key}
              className="inline-flex h-7 items-center gap-[7px] rounded-full border border-border bg-surface px-[11px] text-[12.5px] text-muted"
            >
              <i className="h-1.5 w-1.5 rounded-full bg-status-success" />
              {t(key)}
            </span>
          ))}
        </div>
      </div>
    </section>
  );
}

function Shot({ children }: { children: ReactNode }) {
  return (
    <div
      aria-hidden="true"
      className="flex h-[150px] flex-col justify-center overflow-hidden rounded-[14px] border border-border bg-surface-muted p-4 sm:h-[172px]"
    >
      {children}
    </div>
  );
}

function AgentsShot() {
  return (
    <div className={`${CARD} grid grid-cols-2 gap-0.5 p-1.5 text-[12.5px]`}>
      {AGENT_IDS.map((agent, index) => (
        <span
          key={agent}
          className={`flex h-[30px] items-center gap-[7px] whitespace-nowrap rounded-[7px] px-[9px] ${index === 0 ? 'bg-foreground text-surface' : 'text-muted'}`}
        >
          <AgentMark agent={agent} className="h-3.5 w-3.5" />
          {AGENT_NAMES[agent]}
        </span>
      ))}
    </div>
  );
}

function ModelsShot() {
  const t = useT();
  const rows = ['Anthropic', 'OpenAI', 'FreeInference'];
  return (
    <div className={`${CARD} p-[5px] text-[12.5px]`}>
      {rows.map((name, index) => (
        <div
          key={name}
          className={`flex h-8 items-center justify-between gap-2 whitespace-nowrap rounded-[7px] px-[9px] ${index === 0 ? 'bg-surface-hover font-medium text-foreground' : 'text-muted'}`}
        >
          {name}
          <em className="font-mono text-[10.5px] not-italic text-status-success">
            {t('landing.stackModelVerified')}
          </em>
        </div>
      ))}
    </div>
  );
}

function EnvironmentShot() {
  const t = useT();
  return (
    <div className={`${CARD} flex flex-col gap-1.5 px-3 py-[11px]`}>
      <span className="text-[11px] text-muted">{t('landing.stackEnvironmentScript')}</span>
      <code className="block whitespace-pre rounded-[7px] bg-surface-hover px-[9px] py-[7px] font-mono text-[11px] leading-[1.7] text-foreground">
        {'npm ci\nnpx playwright install'}
      </code>
      <span className="flex justify-between font-mono text-[11px] text-muted">
        <span>DATABASE_URL</span>
        <span>••••••••</span>
      </span>
    </div>
  );
}

function ToolsShot() {
  const t = useT();
  return (
    <div className={`${CARD} flex flex-col gap-[9px] px-3 py-[11px]`}>
      <span className="flex items-center justify-between text-xs text-muted">
        issue-tracker
        <em className="inline-flex items-center gap-1.5 text-[11px] not-italic text-status-success">
          <i className="h-1.5 w-1.5 rounded-full bg-status-success" />
          {t('landing.stackToolsConnected')}
        </em>
      </span>
      <div className="flex flex-col gap-[7px] rounded-lg border border-status-warning/30 bg-status-warning/5 px-2.5 py-[9px]">
        <span className="text-[11.5px] text-status-warning">{t('landing.stackToolsWaiting')}</span>
        <code className="font-mono text-[11.5px] text-foreground">create_issue</code>
        <div className="flex gap-1.5">
          <span className={MINI_BUTTON}>{t('landing.stackToolsDeny')}</span>
          <span className={`${MINI_BUTTON} border-foreground bg-foreground text-surface`}>
            {t('landing.stackToolsAllow')}
          </span>
        </div>
      </div>
    </div>
  );
}

const STACK: { shot: ReactNode; title: TranslationKey; detail: TranslationKey }[] = [
  { shot: <AgentsShot />, title: 'landing.stackAgent', detail: 'landing.stackAgentDetail' },
  { shot: <ModelsShot />, title: 'landing.stackModel', detail: 'landing.stackModelDetail' },
  {
    shot: <EnvironmentShot />,
    title: 'landing.stackEnvironment',
    detail: 'landing.stackEnvironmentDetail',
  },
  { shot: <ToolsShot />, title: 'landing.stackTools', detail: 'landing.stackToolsDetail' },
];

/** 02: the agent, the model key, the environment and the tools are the owner's own. */
export function StackSection() {
  const t = useT();
  return (
    <section aria-labelledby="landing-stack" className={SECTION}>
      <div className="flex flex-col items-start gap-3.5 md:flex-row md:items-end md:justify-between md:gap-10">
        <div>
          <Eyebrow number="02">{t('landing.stackEyebrow')}</Eyebrow>
          <h2 id="landing-stack" className={HEADING}>
            {t('landing.stackTitle')}
          </h2>
        </div>
        <p className="max-w-[360px] text-[16.5px] leading-relaxed text-muted">
          {t('landing.stackLede')}
        </p>
      </div>
      <div className="mt-11 grid gap-x-5 gap-y-8 sm:grid-cols-2 min-[1101px]:grid-cols-4">
        {STACK.map((item) => (
          <div key={item.title}>
            <Shot>{item.shot}</Shot>
            <h3 className="mt-4 text-base font-semibold tracking-[-0.015em] text-foreground">
              {t(item.title)}
            </h3>
            <p className="mt-1.5 text-sm leading-relaxed text-muted">{t(item.detail)}</p>
          </div>
        ))}
      </div>
    </section>
  );
}

/** 03: the work is visible, and publishing is the owner's call. */
export function ReviewSection() {
  const t = useT();
  const checks: TranslationKey[] = [
    'landing.reviewTests',
    'landing.reviewAgent',
    'landing.reviewChecks',
  ];
  const points: TranslationKey[] = [
    'landing.reviewPoint1',
    'landing.reviewPoint2',
    'landing.reviewPoint3',
  ];
  return (
    <section
      aria-labelledby="landing-review"
      className={`${SECTION} grid items-center gap-10 min-[1101px]:grid-cols-[minmax(0,1.15fr)_minmax(0,1fr)] min-[1101px]:gap-[72px]`}
    >
      <div
        aria-hidden="true"
        className="order-last rounded-[18px] bg-[#0a0a0a] p-5 text-white sm:p-[30px] min-[1101px]:order-none dark:bg-surface-raised dark:ring-1 dark:ring-border"
      >
        <p className="flex items-center gap-2 font-mono text-xs text-neutral-400 dark:text-muted">
          <PullRequestIcon />
          {t('landing.demo.draftPr', { number: 128 })}
        </p>
        <div className="mt-4 rounded-[10px] bg-surface px-[22px] py-5 text-foreground shadow-card">
          <span className="font-mono text-[11.5px] text-muted">your-org/web · #128</span>
          <b className="mt-1.5 block text-[17px] font-semibold tracking-[-0.015em]">
            Add CSV export tests
          </b>
          <span className="mt-2 flex gap-3 font-mono text-[11.5px] text-muted">
            <span>
              <span className="text-status-success">+51</span>{' '}
              <span className="text-accent">−1</span>
            </span>
            <span>{t('landing.demo.fileCount', { count: 2 })}</span>
          </span>
          <span className="mt-4 flex flex-col gap-2 border-t border-border pt-3.5">
            {checks.map((key) => (
              <span key={key} className="flex items-center gap-[9px] text-[13px] text-muted">
                <CheckIcon className="text-status-success" />
                {t(key)}
              </span>
            ))}
          </span>
          <span className="mt-[18px] flex gap-1.5">
            <span className={`${MINI_BUTTON} h-7 px-[11px] text-[12.5px]`}>
              {t('landing.demo.review')}
            </span>
            <span
              className={`${MINI_BUTTON} h-7 border-foreground bg-foreground px-[11px] text-[12.5px] text-surface`}
            >
              {t('landing.demo.watchMerge')}
            </span>
          </span>
        </div>
        <p className="mt-[18px] text-[13px] text-neutral-400 dark:text-muted">
          {t('landing.reviewAfter')}
        </p>
      </div>
      <div>
        <Eyebrow number="03">{t('landing.reviewEyebrow')}</Eyebrow>
        <h2 id="landing-review" className={HEADING}>
          {t('landing.reviewTitleLead')}
          <br />
          {t('landing.reviewTitleTail')}
        </h2>
        <p className={LEDE}>{t('landing.reviewLede')}</p>
        <ul className="mt-[26px] flex flex-col gap-3">
          {points.map((key) => (
            <li key={key} className="flex items-center gap-[11px] text-[15px] text-foreground">
              <CheckIcon className="shrink-0 text-status-success" />
              {t(key)}
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
}

export function ClosingSection({
  actions,
  signupOpen,
}: {
  actions: ReactNode;
  signupOpen: boolean;
}) {
  const t = useT();
  return (
    <section
      aria-labelledby="landing-closing"
      className="mx-auto max-w-[1200px] px-[18px] pb-14 pt-20 sm:px-7 lg:pb-24 lg:pt-[120px]"
    >
      <div className="flex flex-wrap items-center justify-between gap-6 rounded-[18px] bg-[#0a0a0a] px-6 py-8 text-white sm:px-11 sm:py-10 dark:bg-surface-raised dark:ring-1 dark:ring-border">
        <div>
          <h2
            id="landing-closing"
            className="text-[30px] font-semibold leading-tight tracking-[-0.03em]"
          >
            {t('landing.closingTitle')}
          </h2>
          <p className="mt-2 font-mono text-[12.5px] text-neutral-400 dark:text-muted">
            {t(signupOpen ? 'landing.closingSteps' : 'landing.closingStepsSignIn')}
          </p>
        </div>
        <div className="flex flex-wrap gap-3">{actions}</div>
      </div>
    </section>
  );
}
