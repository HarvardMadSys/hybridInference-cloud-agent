// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import type { ComponentProps } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { LocaleProvider } from '@/components/providers/LocaleProvider';
import type { UserMemoryView } from '@/lib/api/userMemory';
import { LOCALE_STORAGE_KEY } from '@/lib/i18n/locales';
import { UserHomeInsights } from './UserHomeInsights';

const view: UserMemoryView = {
  enabled: true,
  revision: 1,
  updated_at: null,
  avatar: { data_url: 'data:image/svg+xml,<svg></svg>', style: 'illustrated' },
  portrait: {
    summary: '',
    projects: [],
    focus_areas: [],
    preferred_runtime: null,
    preferred_model: null,
  },
  memories: [],
  suggestions: [
    {
      id: 'validation',
      title: 'Include a validation command',
      reason: 'Recent requests often need test fixes.',
      action: 'Add the expected test command to your next request.',
    },
  ],
};

function panel(overrides: Partial<ComponentProps<typeof UserHomeInsights>> = {}) {
  return (
    <LocaleProvider>
      <UserHomeInsights
        view={view}
        loading={false}
        error={false}
        onRetry={vi.fn()}
        {...overrides}
      />
    </LocaleProvider>
  );
}

beforeEach(() => {
  localStorage.clear();
  localStorage.setItem(LOCALE_STORAGE_KEY, 'en');
});
afterEach(cleanup);

describe('homepage user insights', () => {
  it('presents each suggestion with its reason and a concrete next action alongside memory settings', () => {
    render(panel());

    const region = screen.getByRole('region', { name: 'Suggestions for your next request' });
    const suggestion = within(region)
      .getByRole('heading', { name: view.suggestions[0].title })
      .closest('li')!;
    expect(
      within(suggestion).getByRole('heading', { name: view.suggestions[0].title }),
    ).toBeVisible();
    expect(within(suggestion).getByText(view.suggestions[0].reason)).toBeVisible();
    expect(within(suggestion).getByText(view.suggestions[0].action)).toBeVisible();
    expect(within(region).getByRole('link', { name: 'Manage user memory' })).toHaveAttribute(
      'href',
      '/integrations#settings-user-memory',
    );
    expect(screen.queryByRole('button', { name: 'Retry suggestions' })).not.toBeInTheDocument();
  });

  it('explains when suggestions become available when memory has no suggestions', () => {
    render(panel({ view: { ...view, suggestions: [] } }));

    expect(
      screen.getByText('Suggestions will appear when your activity provides enough context.'),
    ).toBeVisible();
    expect(
      screen.queryByRole('heading', { name: view.suggestions[0].title }),
    ).not.toBeInTheDocument();
    expect(screen.getByText('How your avatar is generated')).toBeVisible();
    expect(screen.queryByText(/Automatic user memory is paused/)).not.toBeInTheDocument();
  });

  it('keeps existing suggestions available while explaining paused updates and how to manage them', () => {
    render(panel({ view: { ...view, enabled: false } }));

    expect(screen.getByText(view.suggestions[0].action)).toBeVisible();
    expect(screen.getByText(/Automatic user memory is paused/)).toHaveTextContent(
      'Existing suggestions and your avatar theme are kept until you enable updates or clear memory in settings.',
    );
    expect(screen.getByRole('link', { name: 'Manage user memory' })).toHaveAttribute(
      'href',
      '/integrations#settings-user-memory',
    );
  });

  it('announces loading and offers a retry after failure before displaying recovered suggestions', () => {
    const onRetry = vi.fn();
    const rendered = render(panel({ view: null, loading: true, onRetry }));

    expect(screen.getByRole('status')).toHaveTextContent('Loading your suggestions…');
    expect(screen.queryByText(/Suggestions will appear/)).not.toBeInTheDocument();
    expect(screen.getByText('How your avatar is generated')).toBeVisible();

    rendered.rerender(panel({ view: null, error: true, onRetry }));
    expect(screen.getByRole('status')).toHaveTextContent(
      'Could not load your suggestions. You can still start a task.',
    );
    fireEvent.click(screen.getByRole('button', { name: 'Retry suggestions' }));
    expect(onRetry).toHaveBeenCalledTimes(1);

    rendered.rerender(panel({ onRetry }));
    expect(screen.getByText(view.suggestions[0].action)).toBeVisible();
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Retry suggestions' })).not.toBeInTheDocument();
  });

  it('opens and closes the avatar guide with its native disclosure and explains all seven work areas', () => {
    render(panel());

    const summary = screen.getByText('How your avatar is generated');
    const guide = summary.closest('details')!;
    const generation = within(guide).getByText(
      /Pokémon avatar is assembled from layered illustrated shapes/,
    );
    expect(summary.tagName).toBe('SUMMARY');
    expect(guide).not.toHaveAttribute('open');
    expect(generation).not.toBeVisible();

    fireEvent.click(summary);
    expect(guide).toHaveAttribute('open');
    expect(generation).toBeVisible();
    expect(generation).toHaveTextContent(
      'English keywords in up to your 50 most recent eligible requests across projects.',
    );
    expect(within(guide).getByText('Your Pokémon companion')).toBeVisible();
    expect(
      within(guide).getByText(
        /Pikachu, Bulbasaur, Charmander, Squirtle, Eevee, Gengar, or Jigglypuff/,
      ),
    ).toHaveTextContent(
      'Silhouette, ears, tail, and face are layered together; familiar Pokémon colors stay the same.',
    );
    expect(within(guide).getByText('Habitat palette')).toBeVisible();
    expect(within(guide).getByText(/backdrop, halo, and platform take their colors/)).toBeVisible();
    expect(within(guide).getByText('Work orbit')).toBeVisible();
    expect(within(guide).getByText(/The largest badge at the top right/)).toHaveTextContent(
      'They describe topics, not skill levels or achievements.',
    );
    expect(within(guide).getByText(/The largest badge at the top right/)).toHaveTextContent(
      'the left badge shows the second. Smaller badges below show the rest of your recent mix.',
    );
    expect(
      within(guide).getByText(/account-specific default habitat with decorative details/),
    ).toBeVisible();
    expect(
      within(guide).getByText(/Clearing user memory restores the default habitat/),
    ).toHaveTextContent('Both keep the same Pokémon, pose, and expression.');

    const legend = within(guide).getByRole('list');
    expect(
      within(legend)
        .getAllByRole('listitem')
        .map((item) => item.textContent),
    ).toEqual([
      'Code brackets · Blue · Backend and APIs',
      'Window · Rose · Frontend and UI',
      'Checkmark · Green · Testing',
      'Bug · Gold · Reliability and debugging',
      'Shield · Violet · Security',
      'Database · Teal · Data and storage',
      'Cloud · Peach · Deployment and infrastructure',
    ]);
    expect(legend).toBeVisible();

    fireEvent.click(summary);
    expect(guide).not.toHaveAttribute('open');
    expect(generation).not.toBeVisible();
  });

  it('translates the suggestion context, paused state, and avatar guide into Chinese', () => {
    localStorage.setItem(LOCALE_STORAGE_KEY, 'zh-CN');
    render(panel({ view: { ...view, enabled: false } }));

    expect(screen.getByRole('region', { name: '下一次请求的改进建议' })).toBeVisible();
    expect(screen.getByText(/已暂停自动建立用户记忆/)).toBeVisible();
    expect(screen.getByRole('link', { name: '管理用户记忆' })).toHaveAttribute(
      'href',
      '/integrations#settings-user-memory',
    );

    const summary = screen.getByText('头像是如何生成的');
    fireEvent.click(summary);
    const guide = summary.closest('details')!;
    expect(guide).toHaveAttribute('open');
    expect(within(guide).getByText(/最近最多 50 条符合条件的请求进行英文关键词匹配/)).toBeVisible();
    expect(within(guide).getByText('你的宝可梦伙伴')).toBeVisible();
    expect(
      within(guide).getByText(/皮卡丘、妙蛙种子、小火龙、杰尼龟、伊布、耿鬼或胖丁/),
    ).toHaveTextContent('轮廓、耳朵、尾巴与面容逐层组合，保留宝可梦熟悉的配色。');
    expect(within(guide).getByText('栖息地配色')).toBeVisible();
    expect(within(guide).getByText('工作徽章轨道')).toBeVisible();
    expect(within(guide).getByText('默认栖息地')).toBeVisible();
    expect(within(guide).getByText(/清除用户记忆会恢复默认栖息地/)).toHaveTextContent(
      '两项操作都会保留同一只宝可梦及其姿态与表情',
    );
    expect(within(guide).getByRole('heading', { name: '工作领域的配色与徽章' })).toBeVisible();
    expect(
      within(guide)
        .getAllByRole('listitem')
        .map((item) => item.textContent),
    ).toEqual([
      '代码括号 · 蓝色 · 后端与 API',
      '窗口 · 玫瑰色 · 前端与界面',
      '对勾 · 绿色 · 测试',
      '虫子 · 金色 · 可靠性与调试',
      '盾牌 · 紫色 · 安全',
      '数据库 · 青色 · 数据与存储',
      '云朵 · 桃色 · 部署与基础设施',
    ]);
  });

  it('provides translated loading, retry, and empty guidance for Chinese readers', () => {
    localStorage.setItem(LOCALE_STORAGE_KEY, 'zh-CN');
    const onRetry = vi.fn();
    const rendered = render(panel({ view: null, loading: true, onRetry }));
    expect(screen.getByRole('status')).toHaveTextContent('正在加载建议…');

    rendered.rerender(panel({ view: null, error: true, onRetry }));
    expect(screen.getByRole('status')).toHaveTextContent('无法加载建议，你仍然可以开始任务。');
    fireEvent.click(screen.getByRole('button', { name: '重试建议' }));
    expect(onRetry).toHaveBeenCalledTimes(1);

    rendered.rerender(panel({ view: { ...view, suggestions: [] }, onRetry }));
    expect(screen.getByText('当任务活动提供足够背景时，系统会生成建议。')).toBeVisible();
  });
});
