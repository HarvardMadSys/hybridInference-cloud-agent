'use client';

import { useEffect, useState, type ReactNode } from 'react';
import { useT } from '@/components/providers';
import { getAgentJobImage } from '@/lib/api/agents';

const IMAGE_NAME = /\.(png|jpe?g|gif|webp)$/i;

/** Whether a path's name says image. The server decides from the bytes. */
export function looksLikeImage(path: string): boolean {
  return IMAGE_NAME.test(path);
}

export function basename(path: string): string {
  return path.split('/').pop() || path;
}

/**
 * One workspace image as an object URL, read through the image route, whose
 * bound is larger than a file preview's. The URL is released when the image is
 * no longer shown, and `path` null reads nothing.
 */
export function useWorkspaceImage(
  jobId: string,
  path: string | null,
): { url: string | null; failed: boolean } {
  const key = path === null ? null : `${jobId}\n${path}`;
  const [state, setState] = useState<{ key: string; url: string | null; failed: boolean } | null>(
    null,
  );

  useEffect(() => {
    if (key === null || path === null) return;
    if (typeof URL.createObjectURL !== 'function') {
      setState({ key, url: null, failed: true });
      return;
    }
    const controller = new AbortController();
    let url: string | null = null;
    getAgentJobImage(jobId, path, controller.signal)
      .then((blob) => {
        if (controller.signal.aborted) return;
        url = URL.createObjectURL(blob);
        setState({ key, url, failed: false });
      })
      .catch(() => {
        if (!controller.signal.aborted) setState({ key, url: null, failed: true });
      });
    return () => {
      controller.abort();
      if (url) URL.revokeObjectURL(url);
    };
  }, [key, jobId, path]);

  const current = state?.key === key ? state : null;
  return { url: current?.url ?? null, failed: current?.failed ?? false };
}

/**
 * A file past the preview bound. An image (a full-page screenshot usually is
 * one) loads through the image route; anything else, or an image that cannot
 * be read either, shows `fallback`.
 */
export function LargeFilePreview({
  jobId,
  path,
  fallback,
}: {
  jobId: string;
  path: string;
  fallback: ReactNode;
}) {
  const t = useT();
  const image = looksLikeImage(path);
  const { url, failed } = useWorkspaceImage(jobId, image ? path : null);
  if (!image || failed) return <>{fallback}</>;
  if (!url) {
    return (
      <p role="status" className="p-4 text-sm text-muted">
        {t('workspaceFiles.loadingImage')}
      </p>
    );
  }
  return (
    <div className="flex min-h-0 items-center justify-center p-4">
      {/* The source is a bounded authorized Blob, not a remote image to optimize. */}
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        src={url}
        alt={t('workspaceFiles.previewAlt', { name: basename(path) })}
        className="max-h-[28rem] max-w-full rounded object-contain"
      />
    </div>
  );
}
