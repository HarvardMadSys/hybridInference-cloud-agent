'use client';

// The screenshots a turn's tools saved, shown under the turn. Browser tools
// save them under the workspace's artifacts directory (the Playwright preset
// does with --output-dir), so the turn shows the images written there while
// it ran, and the agent is not asked to say anything about them.

import { useEffect, useId, useRef, useState } from 'react';
import { useT } from '@/components/providers';
import { useModalDialog } from '@/components/ui/useModalDialog';
import {
  listAgentJobArtifactImages,
  type AgentWorkspaceImageApi,
  type AgentWorkspaceImagesApi,
} from '@/lib/api/agents';
import { basename, useWorkspaceImage } from '../workspace/WorkspaceImage';
import { hasSettledTurn } from './recordedTurnOutputs';
import type { AgentJob } from '../types';

/** How long one conversation's listing serves every turn that shows it. */
const LISTING_TTL_MS = 20_000;
/** Slack for the sandbox's file times against the turn's own clock. */
const BEFORE_MS = 2_000;
const AFTER_MS = 5_000;
/** At most this many thumbnails per turn: its latest. */
const SHOWN = 6;

const listings = new Map<string, { at: number; promise: Promise<AgentWorkspaceImagesApi> }>();

/** One listing per conversation, however many of its turns ask: it is one exec in the sandbox. */
function conversationImages(scope: string, jobId: string): Promise<AgentWorkspaceImagesApi> {
  const cached = listings.get(scope);
  if (cached && Date.now() - cached.at < LISTING_TTL_MS) return cached.promise;
  const promise = listAgentJobArtifactImages(jobId, 0);
  listings.set(scope, { at: Date.now(), promise });
  promise.catch(() => {
    if (listings.get(scope)?.promise === promise) listings.delete(scope);
  });
  return promise;
}

/** For tests: forget every conversation's listing. */
export function clearScreenshotListings(): void {
  listings.clear();
}

/** The images written while a turn ran, oldest first. */
export function turnImages(
  images: readonly AgentWorkspaceImageApi[],
  startedAt: string,
  finishedAt: string,
): AgentWorkspaceImageApi[] {
  const start = Date.parse(startedAt) - BEFORE_MS;
  const end = Date.parse(finishedAt) + AFTER_MS;
  if (!Number.isFinite(start) || !Number.isFinite(end)) return [];
  return images
    .filter((image) => image.modified_at * 1000 >= start && image.modified_at * 1000 <= end)
    .sort((a, b) => a.modified_at - b.modified_at);
}

type ScreenshotTurn = Pick<AgentJob, 'id' | 'threadId' | 'state' | 'startedAt' | 'finishedAt'>;

export function TurnScreenshots({ job }: { job: ScreenshotTurn }) {
  const t = useT();
  const { startedAt, finishedAt } = job;
  const key =
    hasSettledTurn(job.state) && startedAt && finishedAt
      ? `${job.id}\n${job.state}\n${finishedAt}`
      : null;
  const [found, setFound] = useState<{ key: string; images: AgentWorkspaceImageApi[] } | null>(
    null,
  );
  const [open, setOpen] = useState<{ image: AgentWorkspaceImageApi; url: string } | null>(null);

  useEffect(() => {
    if (key === null || !startedAt || !finishedAt) return;
    let cancelled = false;
    conversationImages(job.threadId ?? job.id, job.id)
      .then((listing) => {
        if (!cancelled)
          setFound({ key, images: turnImages(listing.images, startedAt, finishedAt) });
      })
      // No workspace (a cold or deleted sandbox) shows no screenshots, not an error.
      .catch(() => {
        if (!cancelled) setFound({ key, images: [] });
      });
    return () => {
      cancelled = true;
    };
  }, [key, job.id, job.threadId, startedAt, finishedAt]);

  const images = found?.key === key ? found.images : [];
  if (images.length === 0) return null;
  const shown = images.slice(-SHOWN);
  return (
    <section aria-label={t('turnScreenshots.title')} className="mt-3">
      <h3 className="text-[11px] font-semibold uppercase tracking-[0.08em] text-gray-400 dark:text-gray-500">
        {t('turnScreenshots.title')}
      </h3>
      {images.length > SHOWN ? (
        <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
          {t('turnScreenshots.earlier', { count: images.length - SHOWN })}
        </p>
      ) : null}
      <ul className="mt-2 flex flex-wrap gap-2">
        {shown.map((image) => (
          <li key={image.path}>
            <Thumbnail jobId={job.id} image={image} onOpen={(url) => setOpen({ image, url })} />
          </li>
        ))}
      </ul>
      {open ? (
        <ScreenshotDialog image={open.image} url={open.url} onClose={() => setOpen(null)} />
      ) : null}
    </section>
  );
}

function Thumbnail({
  jobId,
  image,
  onOpen,
}: {
  jobId: string;
  image: AgentWorkspaceImageApi;
  onOpen: (url: string) => void;
}) {
  const t = useT();
  const { url, failed } = useWorkspaceImage(jobId, image.path);
  return (
    <button
      type="button"
      disabled={!url}
      onClick={() => url && onOpen(url)}
      aria-label={t('turnScreenshots.open', { name: basename(image.path) })}
      className="block h-24 w-36 overflow-hidden rounded-md border border-gray-200 bg-gray-50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-500 disabled:cursor-default dark:border-white/10 dark:bg-white/5"
    >
      {url ? (
        // An authorized Blob from the workspace, not a remote image to optimize.
        // eslint-disable-next-line @next/next/no-img-element
        <img src={url} alt="" className="h-full w-full object-cover object-top" />
      ) : (
        <span className="flex h-full items-center justify-center px-2 text-center text-[11px] text-gray-400 dark:text-gray-500">
          {failed ? t('turnScreenshots.failed') : t('workspaceFiles.loadingImage')}
        </span>
      )}
    </button>
  );
}

function ScreenshotDialog({
  image,
  url,
  onClose,
}: {
  image: AgentWorkspaceImageApi;
  url: string;
  onClose: () => void;
}) {
  const t = useT();
  const panel = useRef<HTMLDivElement>(null);
  const titleId = useId();
  useModalDialog(panel, { onClose, initialFocus: 'button[data-close]' });
  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center bg-gray-900/60 p-4 dark:bg-black/70">
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className="ui-card flex max-h-full w-full max-w-5xl flex-col gap-3 p-4"
      >
        <div className="flex items-center gap-3">
          <h2 id={titleId} className="min-w-0 flex-1 truncate font-mono text-xs text-muted">
            {image.path}
          </h2>
          <button
            type="button"
            data-close
            onClick={onClose}
            className="ui-button ui-button-secondary"
          >
            {t('turnScreenshots.close')}
          </button>
        </div>
        <div className="flex min-h-0 flex-1 items-center justify-center overflow-auto">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src={url}
            alt={t('workspaceFiles.previewAlt', { name: basename(image.path) })}
            className="max-h-[80vh] max-w-full object-contain"
          />
        </div>
      </div>
    </div>
  );
}
