// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import { LocaleProvider } from '@/components/providers/LocaleProvider';
import type { MeasuredPrefixApi } from '@/lib/api/agents';
import { PrefixRows } from './PrefixRows';

const PREFIX: MeasuredPrefixApi = {
  source: 'member_request_meter',
  token_basis: 'chars_cjk_v1',
  event_id: 7,
  protocol: 'anthropic_messages',
  mode: 'passthrough',
  system_chars: 48_000,
  system_tokens: 12_000,
  system_parts: 3,
  tool_count: 42,
  tool_chars: 80_000,
  tool_tokens: 20_000,
  tools: [
    { label: 'mcp__session__search', chars: 8_000, tokens: 2_000 },
    { label: 'Bash', chars: 4_000, tokens: 1_000 },
  ],
  tools_omitted: 40,
  other_prefixes: 1,
};

function rows(prefix: MeasuredPrefixApi | null) {
  return render(
    <LocaleProvider>
      <ul>
        <PrefixRows prefix={prefix} />
      </ul>
    </LocaleProvider>,
  );
}

describe('PrefixRows', () => {
  afterEach(cleanup);

  it('says each row was not recorded when nothing measured the request', () => {
    rows(null);
    expect(screen.getByText('System prompt')).toBeVisible();
    expect(screen.getByText('Tool schemas')).toBeVisible();
    expect(screen.getAllByText('not recorded')).toHaveLength(2);
  });

  it('shows what the meter measured, and the tools by size on request', () => {
    rows(PREFIX);
    expect(screen.queryByText('not recorded')).not.toBeInTheDocument();
    expect(screen.getByText('42 tools')).toBeVisible();
    expect(screen.getByText(/other prefixes/)).toBeVisible();
    const toggle = screen.getByRole('button', { name: /Tool schemas/ });
    expect(screen.queryByText('Bash')).not.toBeInTheDocument();
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByText('mcp__session__search')).toBeVisible();
    expect(screen.getByText('Bash')).toBeVisible();
    expect(screen.getByText('40 smaller tools are not listed.')).toBeVisible();
  });

  it('cannot open a tool list it does not have', () => {
    rows({ ...PREFIX, tools: [], tools_omitted: 42, other_prefixes: 0 });
    expect(screen.getByRole('button', { name: /Tool schemas/ })).toBeDisabled();
    expect(screen.queryByText(/other prefixes/)).not.toBeInTheDocument();
  });
});
