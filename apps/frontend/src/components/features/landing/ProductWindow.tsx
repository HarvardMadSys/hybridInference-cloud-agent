'use client';

import { useEffect, useRef, useState, type ReactNode } from 'react';
import { useT } from '@/components/providers/LocaleProvider';
import { AgentMark } from './AgentMarks';
import {
  AGENT_IDS,
  AGENT_MODELS,
  AGENT_NAMES,
  visibleTerminal,
  type DemoExample,
  type DemoFrame,
  type AgentId,
  type DemoStep,
  type TermTone,
} from './demoScript';
import styles from './ProductDemo.module.css';

/** Show `value`, fading the old one out first when it changes. */
function useCrossfade<T>(value: T, instant: boolean): [T, boolean] {
  const [shown, setShown] = useState(value);
  const [fading, setFading] = useState(false);
  useEffect(() => {
    if (Object.is(value, shown)) return;
    if (instant) {
      setShown(value);
      setFading(false);
      return;
    }
    setFading(true);
    const timer = window.setTimeout(() => {
      setShown(value);
      setFading(false);
    }, 170);
    return () => window.clearTimeout(timer);
  }, [value, shown, instant]);
  return [shown, fading];
}

function Crossfade({ value, instant }: { value: string; instant: boolean }) {
  const [shown, fading] = useCrossfade(value, instant);
  return (
    <span className={styles.swap} data-fading={fading}>
      {shown}
    </span>
  );
}

/** Counts up to `seconds` instead of jumping, unless the window is jumping. */
function Clock({ seconds, instant }: { seconds: number; instant: boolean }) {
  const [shown, setShown] = useState(seconds);
  useEffect(() => {
    if (instant || seconds <= shown) {
      if (seconds !== shown) setShown(seconds);
      return;
    }
    const timer = window.setTimeout(() => setShown((value) => value + 1), 45);
    return () => window.clearTimeout(timer);
  }, [seconds, shown, instant]);
  return <span>{`${Math.floor(shown / 60)}:${String(shown % 60).padStart(2, '0')}`}</span>;
}

function workedFor(seconds: number): string {
  return seconds >= 60 ? `${Math.floor(seconds / 60)}m ${seconds % 60}s` : `${seconds}s`;
}

function withCode(text: string): ReactNode[] {
  return text.split('`').map((part, index) => (index % 2 ? <code key={index}>{part}</code> : part));
}

function Diffstat({ added, removed }: { added?: number; removed?: number }) {
  return (
    <span>
      {added ? <span className={styles.ok}>+{added}</span> : null}
      {added && removed ? ' ' : null}
      {removed ? <span className={styles.removedText}>−{removed}</span> : null}
    </span>
  );
}

const TERM_TONE: Record<TermTone, string> = {
  command: styles.termCommand,
  ok: styles.termOk,
  bad: styles.termBad,
  dim: styles.termDim,
};

function Sidebar({ frame, example, instant }: WindowProps) {
  const t = useT();
  const title = frame.sent ? example.title : t('landing.demo.newTask');
  return (
    <aside className={styles.sidebar}>
      <span className={styles.newTask}>
        <PlusIcon />
        {t('landing.demo.newTask')}
      </span>
      <span className={styles.search}>{t('landing.demo.searchTasks')}</span>
      <span className={styles.label}>{t('landing.demo.projects')}</span>
      <span className={styles.project}>your-org/web</span>
      <span className={`${styles.task} ${styles.taskHere}`}>
        <Crossfade value={title} instant={instant} />
        <span className={styles.dot} data-state={frame.dot} />
      </span>
      <span className={styles.task}>
        <span>Fix flaky login spec</span>
        <span className={styles.dot} data-state="review" />
      </span>
      <span className={styles.task}>
        <span>Upgrade to Next 15</span>
      </span>
      <span className={styles.project}>lab/simulator</span>
      <span className={styles.task}>
        <span>Profile the solver loop</span>
        <span className={styles.dot} data-state="ask" />
      </span>
      <span className={styles.task}>
        <span>Port configs to TOML</span>
      </span>
    </aside>
  );
}

function StepRow({ step, started, done }: { step: DemoStep; started: boolean; done: boolean }) {
  const tone = step.tone === 'ok' ? styles.ok : step.tone === 'bad' ? styles.bad : '';
  return (
    <li className={styles.step} data-started={started} data-done={done}>
      <span className={styles.stepKind}>{step.kind}</span>
      <span className={styles.stepPath}>{step.path}</span>
      <span className={styles.stepResult}>
        <i className={styles.stepSpin} />
        <span className={`${styles.stepValue} ${tone}`}>
          {step.diff ? <Diffstat {...step.diff} /> : step.result}
        </span>
      </span>
    </li>
  );
}

function Thread({ frame, example }: WindowProps) {
  const t = useT();
  return (
    <div className={styles.thread}>
      <p className={styles.message} data-in={frame.sent} data-demo="message">
        {example.prompt}
      </p>
      <span className={styles.worked} data-state={frame.worked}>
        <span className={styles.workedRunning}>
          <span className={styles.spin} />
          {t('landing.demo.working')}
        </span>
        <span className={styles.workedDone}>
          <ChevronIcon />
          {t('landing.demo.workedFor', { time: workedFor(example.seconds) })}
        </span>
      </span>
      <ol className={styles.steps}>
        {example.steps.map((step, index) => (
          <StepRow
            key={index}
            step={step}
            started={index < frame.stepsStarted}
            done={index < frame.stepsDone}
          />
        ))}
      </ol>
      <p className={styles.answer} data-in={frame.answer}>
        {withCode(example.answer)}
      </p>
      <div className={styles.row} data-state={frame.row} data-demo="pr-row">
        <span className={styles.rowAsk}>
          <span>{t('landing.demo.notInPr')}</span>
          <span
            className={`${styles.button} ${styles.buttonDark}`}
            data-pressed={frame.row === 'pressed'}
          >
            {frame.row === 'busy' ? (
              <>
                <i className={styles.busy} />
                {t('landing.demo.opening')}
              </>
            ) : (
              t('landing.demo.createPr')
            )}
          </span>
        </span>
        <span className={styles.rowOpened}>
          <CheckIcon />
          {t('landing.demo.prOpened', { number: example.pr.number })}
        </span>
      </div>
    </div>
  );
}

function Composer({ frame, instant }: WindowProps) {
  const t = useT();
  return (
    <div className={styles.composer} data-focus={frame.focus}>
      <p className={styles.composerText}>
        {frame.focus ? (
          <>
            <span className={styles.typed}>{frame.typed}</span>
            <span className={styles.caret} />
          </>
        ) : (
          t('landing.demo.placeholder')
        )}
      </p>
      <div className={styles.composerBar}>
        <span className={`${styles.pill} ${styles.pillIcon}`}>
          <PlusIcon />
        </span>
        <span className={styles.pill}>
          <AgentPill agent={frame.agent} instant={instant} />
        </span>
        <span className={styles.pill}>
          <Crossfade value={AGENT_MODELS[frame.agent]} instant={instant} />
        </span>
        <span
          className={styles.send}
          data-ready={frame.typed.length > 0}
          data-pressed={frame.sendPressed}
        >
          <SendIcon />
        </span>
      </div>
    </div>
  );
}

function AgentPill({ agent, instant }: { agent: AgentId; instant: boolean }) {
  const [shown, fading] = useCrossfade(agent, instant);
  return (
    <span className={styles.swap} data-fading={fading}>
      <AgentMark agent={shown} className="h-[13px] w-[13px]" />
      {AGENT_NAMES[shown]}
    </span>
  );
}

const PANE_TAB = { files: 1, terminal: 2, changes: 0 } as const;

function Tabs({ pane }: { pane: DemoFrame['pane'] }) {
  const t = useT();
  const tabs = useRef<HTMLDivElement>(null);
  const [line, setLine] = useState({ left: 0, width: 0 });
  const labels = [
    t('landing.demo.changes'),
    t('landing.demo.files'),
    t('landing.demo.terminal'),
    t('landing.demo.review'),
  ];
  const first = labels[0];
  useEffect(() => {
    const tab = tabs.current?.children[PANE_TAB[pane]] as HTMLElement | undefined;
    if (tab?.offsetWidth) setLine({ left: tab.offsetLeft, width: tab.offsetWidth });
  }, [pane, first]);
  return (
    <div className={styles.tabs} ref={tabs}>
      {labels.map((label, index) => (
        <span key={index} data-on={index === PANE_TAB[pane]}>
          {label}
        </span>
      ))}
      <i
        className={styles.tabLine}
        style={{ width: line.width, transform: `translateX(${line.left}px)` }}
      />
    </div>
  );
}

function Workspace({ frame, example }: WindowProps) {
  let index = 0;
  return (
    <aside className={styles.workspace}>
      <Tabs pane={frame.pane} />
      <div className={styles.panes}>
        <div className={styles.pane} data-on={frame.pane === 'files'}>
          <div className={`${styles.tree} ${styles.mono}`}>
            {example.tree.map((entry) => (
              <div
                key={entry.name}
                className={entry.folder ? styles.folder : undefined}
                style={{ paddingLeft: entry.depth * 14 }}
              >
                {entry.name}
              </div>
            ))}
          </div>
        </div>
        <div className={styles.pane} data-on={frame.pane === 'terminal'}>
          <div className={`${styles.terminal} ${styles.mono}`}>
            {visibleTerminal(example, frame).map((line, key) => (
              <div key={key} className={TERM_TONE[line.tone]}>
                {line.text}
              </div>
            ))}
          </div>
        </div>
        <div className={`${styles.pane} ${styles.mono}`} data-on={frame.pane === 'changes'}>
          {example.files.map((file) => (
            <div key={file.path}>
              <div className={styles.file} style={{ '--i': index++ } as React.CSSProperties}>
                <span>{file.path}</span>
                <Diffstat added={file.added} removed={file.removed} />
              </div>
              <div className={styles.diff}>
                {file.lines.map((line, key) => (
                  <div
                    key={key}
                    className={
                      line.kind === 'added'
                        ? styles.added
                        : line.kind === 'removed'
                          ? styles.removed
                          : undefined
                    }
                    style={{ '--i': index++ } as React.CSSProperties}
                  >
                    <span className={styles.lineNumber}>{line.number}</span>
                    <span>{line.text}</span>
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
      </div>
    </aside>
  );
}

function Picker({ frame }: { frame: DemoFrame }) {
  const t = useT();
  const offset = AGENT_IDS.indexOf(frame.highlight) * 30;
  return (
    <div className={`${styles.float} ${styles.picker}`} data-show={frame.picker}>
      <p className={styles.pickerCaption}>{t('landing.demo.agent')}</p>
      <div className={styles.pickerList}>
        <i className={styles.pickerHighlight} style={{ transform: `translateY(${offset}px)` }} />
        {AGENT_IDS.map((agent) => (
          <span key={agent} className={styles.pickerItem} aria-pressed={agent === frame.agent}>
            <AgentMark agent={agent} className="h-3.5 w-3.5" />
            {AGENT_NAMES[agent]}
            <CheckMark />
          </span>
        ))}
      </div>
    </div>
  );
}

function StatusCard({ frame, example, instant }: WindowProps) {
  const t = useT();
  const card = useRef<HTMLDivElement>(null);
  const [height, setHeight] = useState<number | undefined>(undefined);
  const state = frame.status === 'none' ? 'running' : frame.status;
  useEffect(() => {
    const shown = card.current?.querySelector<HTMLElement>(`[data-card="${state}"]`);
    setHeight(shown?.offsetHeight ? shown.offsetHeight + 30 : undefined);
  }, [state, t]);
  const files = t('landing.demo.fileCount', { count: example.pr.files });
  const diff = <Diffstat added={example.pr.added} removed={example.pr.removed} />;
  return (
    <div
      ref={card}
      className={`${styles.float} ${styles.status}`}
      data-show={frame.status !== 'none'}
      style={{ height }}
    >
      <div className={styles.statusCard} data-card="running" data-on={state === 'running'}>
        <span className={`${styles.statusHead} ${styles.statusHeadRunning}`}>
          <span className={styles.dot} data-state="running" />
          {t('landing.demo.running')}
        </span>
        <b className={styles.statusTitle}>{example.title}</b>
        <span className={`${styles.statusMeta} ${styles.mono}`}>
          <span>{AGENT_NAMES[frame.agent]}</span>
          <Clock seconds={frame.clock} instant={instant} />
        </span>
        <span className={styles.track}>
          <i />
        </span>
        <span className={styles.statusHint}>{t('landing.demo.closeTab')}</span>
      </div>
      <div className={styles.statusCard} data-card="ready" data-on={state === 'ready'}>
        <span className={styles.statusHead}>
          <CheckIcon />
          {t('landing.demo.ready')}
        </span>
        <b className={styles.statusTitle}>{example.title}</b>
        <span className={`${styles.statusMeta} ${styles.mono}`}>
          {diff}
          <span>{files}</span>
          <span>{workedFor(example.seconds)}</span>
        </span>
      </div>
      <div className={styles.statusCard} data-card="pr" data-on={state === 'pr'}>
        <span className={styles.statusHead}>
          <PullRequestIcon />
          {t('landing.demo.draftPr', { number: example.pr.number })}
        </span>
        <b className={styles.statusTitle}>{example.title}</b>
        <span className={`${styles.statusMeta} ${styles.mono}`}>
          {diff}
          <span>{files}</span>
          <span className={styles.checks} data-passed={frame.checksPassed}>
            <span className={styles.checksRunning}>
              <i className={styles.checksSpin} />
              {t('landing.demo.checksRunning')}
            </span>
            <span className={styles.checksPassed}>✓ {t('landing.demo.checksPassed')}</span>
          </span>
        </span>
        <span className={styles.actions}>
          <span className={styles.button}>{t('landing.demo.review')}</span>
          <span className={`${styles.button} ${styles.buttonDark}`}>
            {t('landing.demo.watchMerge')}
          </span>
        </span>
      </div>
    </div>
  );
}

interface WindowProps {
  example: DemoExample;
  frame: DemoFrame;
  instant: boolean;
}

/** The app, drawn at one frame of the demo. Decorative: the page around it says the same. */
export function ProductWindow({ example, frame, instant }: WindowProps) {
  const t = useT();
  const props = { example, frame, instant };
  const title = frame.sent ? example.title : t('landing.demo.newTask');
  return (
    <>
      <div className={styles.shadow} />
      <div className={styles.app}>
        <div className={styles.top}>
          <div className={styles.crumb}>
            <span className={styles.crumbRepo}>your-org/web</span>
            <span className={styles.crumbSep}>/</span>
            <b className={styles.crumbTitle}>
              <Crossfade value={title} instant={instant} />
            </b>
          </div>
          <div className={styles.actions}>
            <span className={`${styles.button} ${styles.reviewButton}`}>
              {t('landing.demo.review')}
            </span>
            <span className={`${styles.button} ${styles.buttonDark}`}>
              <Crossfade
                value={
                  frame.row === 'opened' ? t('landing.demo.viewPr') : t('landing.demo.createPr')
                }
                instant={instant}
              />
            </span>
            <span className={styles.avatar}>Y</span>
          </div>
        </div>
        <div className={styles.grid}>
          <Sidebar {...props} />
          <section className={styles.conversation}>
            <div className={styles.body}>
              <div className={styles.empty} data-out={frame.sent}>
                <span className={styles.mono}>your-org/web · main</span>
                <strong>{t('landing.demo.emptyTitle')}</strong>
              </div>
              <Thread {...props} />
            </div>
            <Composer {...props} />
          </section>
          <Workspace {...props} />
        </div>
      </div>
      <Picker frame={frame} />
      <StatusCard {...props} />
    </>
  );
}

function PlusIcon() {
  return (
    <svg aria-hidden="true" width="12" height="12" viewBox="0 0 14 14" fill="none">
      <path d="M7 2v10M2 7h10" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  );
}

function SendIcon() {
  return (
    <svg aria-hidden="true" width="12" height="12" viewBox="0 0 12 12" fill="none">
      <path
        d="M6 10V2M2.5 5.5 6 2l3.5 3.5"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function ChevronIcon() {
  return (
    <svg aria-hidden="true" width="12" height="12" viewBox="0 0 12 12" fill="none">
      <path
        d="m3 4.5 3 3 3-3"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function CheckMark() {
  return (
    <svg aria-hidden="true" className={styles.check} width="12" height="12" viewBox="0 0 12 12">
      <path
        d="m2.5 6.2 2.2 2.2 4.8-4.8"
        stroke="currentColor"
        strokeWidth="1.6"
        fill="none"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function CheckIcon({ className = '' }: { className?: string }) {
  return (
    <svg aria-hidden="true" className={className} width="15" height="15" viewBox="0 0 16 16">
      <circle cx="8" cy="8" r="7" fill="currentColor" opacity="0.12" />
      <path
        d="m5 8.2 2 2 4-4.4"
        stroke="currentColor"
        strokeWidth="1.5"
        fill="none"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function PullRequestIcon() {
  return (
    <svg aria-hidden="true" width="14" height="14" viewBox="0 0 16 16" fill="none">
      <circle cx="4.5" cy="3.5" r="1.8" stroke="currentColor" strokeWidth="1.3" />
      <circle cx="4.5" cy="12.5" r="1.8" stroke="currentColor" strokeWidth="1.3" />
      <circle cx="11.5" cy="12.5" r="1.8" stroke="currentColor" strokeWidth="1.3" />
      <path
        d="M4.5 5.3v5.4M11.5 10.7V7.5a2 2 0 0 0-2-2H7"
        stroke="currentColor"
        strokeWidth="1.3"
        strokeLinecap="round"
      />
    </svg>
  );
}
