'use client';

import { useT } from '@/components/providers';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  conversationNavigationScope,
  describeCurrentTurn,
  describeHistoryTurns,
  sameNavigationEntries,
  toNavigationEntry,
  type ConversationTurnNavigationEntry,
} from '../conversationTurnNavigation';
import type { AgentJob } from '../types';
import { shouldHoldReading, useConversationScroll } from '../useConversationScroll';
import type { JobDetailPanels } from './useJobDetailPanels';

/** Shared turn descriptions, navigation announcements, and reader-controlled scrolling. */
export function useJobConversation({
  job,
  panels,
  attemptNo,
  liveAttempt,
  setAttemptNo,
}: {
  job: AgentJob;
  panels: JobDetailPanels;
  attemptNo: number;
  liveAttempt: number;
  setAttemptNo: (attemptNo: number) => void;
}) {
  const t = useT();
  const { tab, wideLayout, workspaceOpen, inspector } = panels;
  // The transcript and rail share descriptions. Composer edits cannot change
  // them; streaming updates rebuild only the description whose source changed.
  const currentAttemptNo = job.currentAttemptNo;
  const historicalRows = job.historyTurns;
  const historyTurns = useMemo(
    () => describeHistoryTurns({ historyTurns: historicalRows }),
    [historicalRows],
  );
  const { rewindAnchors, currentRewindAnchor } = useMemo(() => {
    let previous: string | undefined;
    const rewindAnchors = historyTurns.map((turn) => {
      const anchor = previous;
      if (turn.forkable) previous = turn.jobId;
      return anchor;
    });
    return { rewindAnchors, currentRewindAnchor: previous };
  }, [historyTurns]);
  const currentTurn = useMemo(
    () => describeCurrentTurn(job, { attemptNo, currentAttemptNo }),
    [job, attemptNo, currentAttemptNo],
  );
  const visibleEvents = currentTurn.events;
  // The rail renders two host elements per turn while `toDisplayJob()` hands
  // the page a rebuilt job per streamed event. Handing it the entries it was
  // already showing keeps those marks from being rebuilt for a delta that only
  // reaches the newest turn's body.
  const previousEntriesRef = useRef<ConversationTurnNavigationEntry[]>([]);
  const navigationEntries = useMemo(() => {
    const fresh = [
      ...historyTurns.map((turn) => toNavigationEntry(turn)),
      toNavigationEntry(currentTurn, { attemptNo, currentAttemptNo }),
    ];
    const previous = previousEntriesRef.current;
    if (sameNavigationEntries(previous, fresh)) return previous;
    previousEntriesRef.current = fresh;
    return fresh;
  }, [attemptNo, currentAttemptNo, currentTurn, historyTurns]);
  const turnKeys = useMemo(() => navigationEntries.map((entry) => entry.key), [navigationEntries]);
  const conversationKey = conversationNavigationScope(job).conversationKey;
  const scroll = useConversationScroll({
    turnKeys,
    conversationKey,
    active: tab === 'chat' && (wideLayout || !(workspaceOpen || inspector !== null)),
  });
  // Announced only for a jump the reader asked for, never for a streamed
  // event: a transcript that talks on every token is one nobody can listen to.
  const [announcedTurn, setAnnouncedTurn] = useState<string | null>(null);
  const jumpToTurn = useCallback(
    (key: string) => {
      const entry = navigationEntries.find((candidate) => candidate.key === key);
      setAnnouncedTurn(
        entry === undefined ? null : t('turnNavigation.turn', { turn: entry.displayNo }),
      );
      scroll.jumpToTurn(key);
    },
    [navigationEntries, scroll, t],
  );
  // What the live region says: where the reader's last jump landed, or that it
  // could not land. Never a token, and never both at once.
  const navigationNotice =
    scroll.unavailableKey === null ? (announcedTurn ?? '') : t('turnNavigation.unavailable');
  // Switching to an older run changes what the body shows, so it is a reading
  // position, not a live one: the reader settled on a fixed snapshot. The
  // selection also moves by itself — a queued turn starts, a retry arrives —
  // and that must not stop the newest output from being followed.
  const attemptSelection = useRef({ jobId: job.id, attemptNo });
  const chosenAttemptRef = useRef<number | null>(null);
  useEffect(() => {
    const previous = attemptSelection.current;
    attemptSelection.current = { jobId: job.id, attemptNo };
    if (
      !shouldHoldReading(previous, attemptSelection.current, chosenAttemptRef.current, liveAttempt)
    )
      return;
    chosenAttemptRef.current = null;
    scroll.holdReading();
    // `scroll` changes identity on every measurement; the judgement above is
    // what decides whether this is the reader's choice at all.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [attemptNo, job.id, liveAttempt]);

  const selectAttempt = (next: number) => {
    chosenAttemptRef.current = next;
    setAttemptNo(next);
  };
  return {
    historyTurns,
    currentTurn,
    rewindAnchors,
    currentRewindAnchor,
    visibleEvents,
    navigationEntries,
    scroll,
    jumpToTurn,
    navigationNotice,
    selectAttempt,
  };
}
