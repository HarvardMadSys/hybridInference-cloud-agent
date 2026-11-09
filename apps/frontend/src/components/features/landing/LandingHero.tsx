'use client';

import { useEffect, type CSSProperties, type ReactNode } from 'react';
import { useT } from '@/components/providers/LocaleProvider';
import type { TranslationKey } from '@/lib/i18n/translate';
import { EXAMPLES, STAGES, type DemoStage } from './demoScript';
import { ProductWindow } from './ProductWindow';
import { useDemoPlayer, type DemoView } from './useDemoPlayer';
import { usePrefersReducedMotion, useStage } from './useStage';
import styles from './ProductDemo.module.css';

const FLOW: [TranslationKey, TranslationKey][] = [
  ['landing.flow1', 'landing.flow1Detail'],
  ['landing.flow2', 'landing.flow2Detail'],
  ['landing.flow3', 'landing.flow3Detail'],
];

const EXAMPLE_LABELS: TranslationKey[] = [
  'landing.exampleTests',
  'landing.exampleBug',
  'landing.exampleRefactor',
];

function flowState(stage: DemoStage, current: DemoStage) {
  if (stage < current) return 'past';
  return stage === current ? 'current' : 'next';
}

/** Each step can be replayed, and the current one fills while it plays. */
function FlowSteps({
  flow,
  onPlay,
}: {
  flow: DemoView['flow'];
  onPlay: (stage: DemoStage) => void;
}) {
  const t = useT();
  return (
    <div role="group" aria-label={t('landing.flowLabel')}>
      <div className="grid grid-cols-3 gap-2">
        {STAGES.map((stage) => {
          const state = flowState(stage, flow.stage);
          const [title, detail] = FLOW[stage];
          return (
            <button
              key={stage}
              type="button"
              className={`ui-focus-ring ${styles.flowStep}`}
              data-state={state}
              aria-current={state === 'current' ? 'step' : undefined}
              onClick={() => onPlay(stage)}
            >
              <span className={styles.flowNumber}>0{stage + 1}</span>
              <span className={styles.flowTitle}>{t(title)}</span>
              <span className="sr-only"> {t(detail)}</span>
              <span
                key={state === 'current' ? flow.run : 'idle'}
                className={styles.flowBar}
                data-running={state === 'current' && flow.duration > 0}
                style={{ '--dur': `${flow.duration}ms` } as CSSProperties}
              />
            </button>
          );
        })}
      </div>
      <p aria-hidden="true" className="mt-2 text-[13px] leading-5 text-muted">
        {t(FLOW[flow.stage][1])}
      </p>
    </div>
  );
}

function ExampleChips({
  current,
  onChoose,
}: {
  current: number;
  onChoose: (index: number) => void;
}) {
  const t = useT();
  return (
    <div
      className="mt-3 flex flex-wrap items-center gap-1.5 text-[13px] text-muted"
      role="group"
      aria-label={t('landing.examplesLabel')}
    >
      <span className="mr-1">{t('landing.tryAnother')}</span>
      {EXAMPLES.map((_, index) => (
        <button
          key={index}
          type="button"
          aria-pressed={index === current}
          onClick={() => onChoose(index)}
          className="ui-focus-ring h-7 rounded-full border border-border-strong/60 bg-surface px-[11px] text-[12.5px] text-muted transition-colors hover:text-foreground aria-pressed:border-foreground aria-pressed:bg-foreground aria-pressed:text-surface"
        >
          {t(EXAMPLE_LABELS[index])}
        </button>
      ))}
    </div>
  );
}

/**
 * The top of the home page: what the product is, and the product doing a task.
 * `actions` are the account links, which depend on whether sign-up is open.
 */
export function LandingHero({
  actions,
  actionHint,
}: {
  actions: ReactNode;
  actionHint?: ReactNode;
}) {
  const t = useT();
  const calm = usePrefersReducedMotion();
  const { view, player, pause } = useDemoPlayer(calm);
  const { scene, rig, entering, startPose } = useStage(calm);

  // The finished task holds on screen while nobody is looking at it.
  useEffect(() => {
    const element = scene.current;
    if (!element || typeof IntersectionObserver === 'undefined') return;
    const observer = new IntersectionObserver(
      ([entry]) => {
        pause.current.offscreen = !entry.isIntersecting;
      },
      { threshold: 0.2 },
    );
    observer.observe(element);
    return () => observer.disconnect();
  }, [scene, pause]);

  const replay = () => {
    void player.replay();
    const box = scene.current?.getBoundingClientRect();
    if (box && (box.top > window.innerHeight * 0.6 || box.bottom < 0)) {
      scene.current?.scrollIntoView({ block: 'center', behavior: calm ? 'auto' : 'smooth' });
    }
  };

  return (
    <section
      aria-labelledby="landing-title"
      className="mx-auto grid max-w-[1200px] items-center gap-7 px-[18px] pb-8 pt-7 sm:px-7 sm:pb-12 sm:pt-12 min-[1101px]:grid-cols-[minmax(280px,0.46fr)_minmax(0,1fr)] min-[1101px]:gap-9"
    >
      <div>
        <p className="inline-flex items-center gap-2 font-mono text-xs tracking-[0.04em] text-muted">
          <i aria-hidden="true" className="h-1.5 w-1.5 rounded-[2px] bg-accent" />
          {t('landing.kicker')}
        </p>
        <h1
          id="landing-title"
          className="mt-4 text-[clamp(36px,3.9vw,54px)] font-semibold leading-[1.04] tracking-[-0.045em] text-foreground"
        >
          {t('landing.titleLead')}
          <br />
          <span className="text-accent">{t('landing.titleAccent')}</span>
        </h1>
        <p className="mt-4 max-w-[42ch] text-pretty text-base leading-relaxed text-muted">
          {t('landing.lede')}
        </p>
        <div className="mt-5 flex flex-wrap gap-2.5 sm:mt-6">
          {actions}
          <button
            type="button"
            onClick={replay}
            className="ui-focus-ring inline-flex h-[46px] items-center gap-2 rounded-lg border border-border-strong/60 bg-surface px-4 text-sm font-medium text-foreground transition-colors hover:bg-surface-hover"
          >
            <svg aria-hidden="true" width="12" height="12" viewBox="0 0 12 12">
              <path d="M3 2v8l7-4Z" fill="currentColor" />
            </svg>
            {t('landing.seeItWork')}
          </button>
        </div>
        {actionHint}
      </div>
      <div className="min-w-0">
        <div
          ref={scene}
          role="group"
          aria-label={t('landing.demoLabel')}
          className={`${styles.scene} ${styles.heroScene}`}
          data-entering={entering}
          data-instant={view.instant}
          data-fading={view.fading}
          onPointerEnter={(event) => {
            if (event.pointerType === 'mouse') pause.current.hover = true;
          }}
          onPointerLeave={() => {
            pause.current.hover = false;
          }}
          // The workspace's tabs hold the demo for a keyboard as a pointer does.
          onFocus={() => {
            pause.current.focus = true;
          }}
          onBlur={(event) => {
            if (!event.currentTarget.contains(event.relatedTarget)) pause.current.focus = false;
          }}
        >
          <div className={styles.floor} />
          <div ref={rig} className={styles.rig} data-entering={entering} style={startPose}>
            <ProductWindow
              example={EXAMPLES[view.example]}
              frame={view.frame}
              instant={view.instant}
            />
          </div>
        </div>
        <div className="relative mt-3 sm:mx-3">
          <FlowSteps flow={view.flow} onPlay={(stage) => void player.play(stage)} />
          <ExampleChips current={view.example} onChoose={(index) => void player.choose(index)} />
        </div>
      </div>
    </section>
  );
}
