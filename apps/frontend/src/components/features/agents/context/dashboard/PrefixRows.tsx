'use client';

// The Context Browser's system prompt and tool schemas rows: what the request
// sent ahead of the conversation, as the member's request meter measured it
// (`composition.prefix`). Sizes only, estimated from characters; the platform
// stores neither text. Without a measurement (a turn from before the meter, or
// a CLI on its vendor's own host) each row says it was not recorded.

import { useState } from 'react';
import { useLocale } from '@/components/providers';
import type { MeasuredPrefixApi } from '@/lib/api/agents';
import { compactCount } from '@/components/features/settings/usagePresent';
import { labelOf, swatchOf } from './palette';

function NotRecordedRow({ category }: { category: string }) {
  const { t } = useLocale();
  return (
    <li
      className="flex items-center justify-between gap-2 px-2 py-1.5 opacity-70"
      title={t('dashboard.notRecordedTip')}
    >
      <span className="flex items-center gap-1.5">
        <span
          aria-hidden="true"
          className="h-2 w-2 shrink-0 rounded-full border border-dashed border-muted"
        />
        <span className="text-foreground">{t(labelOf(category))}</span>
      </span>
      <span className="text-muted">{t('dashboard.notRecorded')}</span>
    </li>
  );
}

function Label({ category, count }: { category: string; count?: string }) {
  const { t } = useLocale();
  return (
    <span className="flex min-w-0 items-center gap-1.5">
      <span
        aria-hidden="true"
        className={`h-2 w-2 shrink-0 rounded-full ${swatchOf(category).bg}`}
      />
      <span className="truncate text-foreground">{t(labelOf(category))}</span>
      {count ? <span className="tabular-nums text-muted">{count}</span> : null}
    </span>
  );
}

function ToolSchemasRow({ prefix }: { prefix: MeasuredPrefixApi }) {
  const { t, locale } = useLocale();
  const [open, setOpen] = useState(false);
  const count = t('dashboard.browser.prefix.tools', {
    count: prefix.tool_count.toLocaleString(locale),
  });
  return (
    <li title={t('dashboard.browser.prefix.measuredTip')}>
      <button
        type="button"
        aria-expanded={open}
        disabled={prefix.tools.length === 0}
        onClick={() => setOpen(!open)}
        className="ui-focus-ring flex w-full items-center justify-between gap-2 px-2 py-1.5 text-left [@media(pointer:coarse)]:min-h-11"
      >
        <span className="flex min-w-0 items-center gap-1.5">
          <span aria-hidden="true" className="text-muted">
            {open ? '▾' : '▸'}
          </span>
          <Label category="tool_schemas" count={count} />
        </span>
        <span className="shrink-0 tabular-nums text-foreground">
          ≈{compactCount(prefix.tool_tokens)}
        </span>
      </button>
      {open ? (
        <div className="border-t border-border px-2 pb-2">
          <p className="mt-1 text-[11px] text-muted">{t('dashboard.browser.prefix.toolList')}</p>
          <ul className="mt-1 space-y-0.5">
            {prefix.tools.map((tool, index) => (
              <li key={`${tool.label}-${index}`} className="flex justify-between gap-2">
                <span className="min-w-0 truncate font-mono text-[11px] text-foreground">
                  {tool.label}
                </span>
                <span className="shrink-0 tabular-nums text-muted">
                  ≈{compactCount(tool.tokens)}
                </span>
              </li>
            ))}
          </ul>
          {prefix.tools_omitted > 0 ? (
            <p className="mt-1 text-[11px] text-muted">
              {t('dashboard.browser.prefix.omitted', { count: prefix.tools_omitted })}
            </p>
          ) : null}
        </div>
      ) : null}
    </li>
  );
}

/** The two rows every request carries beside its conversation. */
export function PrefixRows({ prefix }: { prefix: MeasuredPrefixApi | null | undefined }) {
  const { t } = useLocale();
  if (!prefix) {
    return (
      <>
        <NotRecordedRow category="system_prompt" />
        <NotRecordedRow category="tool_schemas" />
      </>
    );
  }
  return (
    <>
      <li
        className="flex items-center justify-between gap-2 px-2 py-1.5"
        title={t('dashboard.browser.prefix.measuredTip')}
      >
        <span className="flex min-w-0 items-center gap-1.5">
          <span aria-hidden="true" className="invisible text-muted">
            ▸
          </span>
          <Label category="system_prompt" />
        </span>
        <span className="shrink-0 tabular-nums text-foreground">
          ≈{compactCount(prefix.system_tokens)}
        </span>
      </li>
      <ToolSchemasRow prefix={prefix} />
      {prefix.other_prefixes > 0 ? (
        <li className="px-2 py-1 text-[11px] text-muted">
          {t('dashboard.browser.prefix.others', { count: prefix.other_prefixes })}
        </li>
      ) : null}
    </>
  );
}
