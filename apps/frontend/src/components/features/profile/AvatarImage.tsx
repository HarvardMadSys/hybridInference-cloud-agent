'use client';

import { useT } from '@/components/providers';

export function AvatarImage({
  imageDataUrl,
  companionDataUrl,
  className = 'h-full w-full',
}: {
  imageDataUrl?: string | null;
  companionDataUrl?: string;
  className?: string;
}) {
  const t = useT();
  const source = imageDataUrl || companionDataUrl;
  if (!source) return <PokeBall className={className} />;
  return (
    // Profile uploads are sanitized raster data URLs; companions are server-generated SVGs.
    // eslint-disable-next-line @next/next/no-img-element
    <img
      src={source}
      alt={t(imageDataUrl ? 'settings.profile.imageAlt' : 'composer.avatarAlt')}
      title={t(imageDataUrl ? 'settings.profile.imageAlt' : 'composer.avatarDescription')}
      className={`${className} ${imageDataUrl ? 'object-cover' : 'object-contain'}`}
    />
  );
}

export function PokeBall({ className }: { className: string }) {
  return (
    <svg className={className} viewBox="0 0 32 32" fill="none" aria-hidden="true">
      <circle cx="16" cy="16" r="14" fill="#FFF9EB" />
      <path d="M2 16a14 14 0 0 1 28 0H2Z" fill="#E46965" />
      <circle cx="16" cy="16" r="14" stroke="#3D3946" strokeWidth="2" />
      <path d="M2 16h28" stroke="#3D3946" strokeWidth="2" />
      <circle cx="16" cy="16" r="4.5" fill="#FFF9EB" stroke="#3D3946" strokeWidth="2" />
      <circle cx="16" cy="16" r="1.5" fill="#3D3946" />
      <path d="M8 10a10 10 0 0 1 4-3" stroke="white" strokeWidth="2" strokeLinecap="round" />
    </svg>
  );
}
