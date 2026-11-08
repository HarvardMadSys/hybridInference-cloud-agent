'use client';

import { useT } from '@/components/providers';
import type { ModelEffortOfferApi } from '@/lib/api/agents';
import { AUTO_EFFORT } from './effort';
import { Picker } from './Picker';

/**
 * The reasoning control: Auto, then the levels the model's provider declares
 * for this agent. Auto is the provider's default and is always there. When the
 * provider declares nothing, or nothing this agent can send, Auto is all that
 * is offered and a short note says which; a level the owner had picked that
 * the newly chosen model does not offer is named once (`dropped`). Nothing
 * renders while there is no runnable model to ask about.
 */
export function EffortPicker({
  label,
  value,
  offer,
  onChange,
  dropped = '',
  compact = true,
}: {
  label: string;
  /** `''` is Auto. */
  value: string;
  offer: ModelEffortOfferApi | null;
  onChange: (next: string) => void;
  dropped?: string;
  compact?: boolean;
}) {
  const t = useT();
  if (!offer) return null;
  const levels = offer.status === 'supported' ? offer.levels : [];
  const note =
    offer.status === 'unknown'
      ? t('effort.unknown')
      : offer.status === 'unsupported'
        ? t('effort.unsupported')
        : '';
  return (
    <span className="inline-flex min-w-0 flex-wrap items-center gap-1" title={note || undefined}>
      <Picker
        compact={compact}
        label={label}
        value={value || AUTO_EFFORT}
        options={[AUTO_EFFORT, ...levels]}
        optionLabels={{ [AUTO_EFFORT]: t('effort.auto') }}
        onChange={(next) => onChange(next === AUTO_EFFORT ? '' : next)}
      />
      {note ? <span className="text-xs text-muted">{t('effort.autoOnly')}</span> : null}
      {note ? <span className="sr-only">{note}</span> : null}
      {dropped ? (
        <span role="status" className="text-xs text-status-warning">
          {t('effort.dropped', { level: dropped })}
        </span>
      ) : null}
    </span>
  );
}
