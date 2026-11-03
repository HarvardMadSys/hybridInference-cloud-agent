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

/** The three steps beside the window. The current one fills while it plays; any can be replayed. */
function FlowSteps({
  flow,
  onPlay,
}: {
  flow: DemoView['flow'];
  onPlay: (stage: DemoStage) => void;
}) {
  const t = useT();
  return (
    <div className="max-w-[410px]" role="group" aria-label={t('landing.flowLabel')}>
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
            <span className={styles.flowDetail}>{t(detail)}</span>
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
      className="mt-[18px] flex flex-wrap items-center gap-1.5 text-[13px] text-muted"
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
export function LandingHero({ actions }: { actions: ReactNode }) {
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
      className="mx-auto grid max-w-[1200px] items-center gap-2 px-[18px] pt-10 sm:px-7 min-[1101px]:min-h-[700px] min-[1101px]:grid-cols-[minmax(0,440px)_minmax(0,1fr)] min-[1101px]:pt-7"
    >
      <div className="relative z-[2] min-[901px]:max-[1100px]:grid min-[901px]:max-[1100px]:grid-cols-2 min-[901px]:max-[1100px]:items-end min-[901px]:max-[1100px]:gap-14">
        <div>
          <p className="inline-flex items-center gap-2 font-mono text-xs tracking-[0.04em] text-muted">
            <i aria-hidden="true" className="h-1.5 w-1.5 rounded-[2px] bg-accent" />
            {t('landing.kicker')}
          </p>
          <h1
            id="landing-title"
            className="mt-5 text-[clamp(46px,5.4vw,70px)] font-semibold leading-none tracking-[-0.045em] text-foreground"
          >
            {t('landing.titleLead')}
            <br />
            <span className="text-accent">{t('landing.titleAccent')}</span>
          </h1>
          <p className="mt-[22px] max-w-[410px] text-base leading-[1.55] text-muted sm:text-[17.5px]">
            {t('landing.lede')}
          </p>
          <div className="mt-7 flex flex-wrap gap-3">
            {actions}
            <button
              type="button"
              onClick={replay}
              className="ui-focus-ring inline-flex h-[46px] items-center gap-2 rounded-lg border border-border-strong/60 bg-surface px-[22px] text-[15px] font-medium text-foreground transition-colors hover:bg-surface-hover"
            >
              <svg aria-hidden="true" width="12" height="12" viewBox="0 0 12 12">
                <path d="M3 2v8l7-4Z" fill="currentColor" />
              </svg>
              {t('landing.seeItWork')}
            </button>
          </div>
        </div>
        <div className="mt-[34px] min-[901px]:max-[1100px]:mt-0">
          <FlowSteps flow={view.flow} onPlay={(stage) => void player.play(stage)} />
          <ExampleChips current={view.example} onChoose={(index) => void player.choose(index)} />
        </div>
      </div>
      <div
        ref={scene}
        role="img"
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
      >
        <div className={styles.floor} />
        {/* A plain box between the stage and the window, as in the approved
            prototype: it keeps the depth of the tilt as designed. */}
        <div>
          <div ref={rig} className={styles.rig} data-entering={entering} style={startPose}>
            <ProductWindow
              example={EXAMPLES[view.example]}
              frame={view.frame}
              instant={view.instant}
            />
          </div>
        </div>
      </div>
    </section>
  );
}
