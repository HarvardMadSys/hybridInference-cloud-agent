import type { TranslationKey } from './en';

/**
 * Simplified Chinese.
 *
 * Typed as a *total* map over `TranslationKey`, so this file cannot fall
 * behind: a new English string breaks the build here until it is translated.
 *
 * Product nouns that this team says in English — "Agent", the section name
 * "Agents" — stay in English rather than being rendered 智能体. Translating the
 * word people actually use in standup makes the UI harder to talk about, not
 * easier to read.
 */
export const zhCN: Record<TranslationKey, string> = {
  // ── Shell chrome ────────────────────────────────────────────────────────
  'nav.aria': '产品导航',
  'nav.status': '服务状态',
  'nav.docsAssistant': '文档助手',
  'nav.agents': 'Agents',
  'nav.admin': '管理后台',
  'nav.dashboard': '控制台',
  'nav.logOut': '退出登录',
  'nav.expandTaskList': '展开任务列表',
  'nav.collapseTaskList': '收起任务列表',

  'shell.internalRequired.title': '需要管理员权限',
  'shell.internalRequired.body': 'Agent 沙箱会用本部署自己的凭据运行代码,因此仅限管理员使用。',
  'shell.internalRequired.back': '返回控制台',

  // ── Sidebar ─────────────────────────────────────────────────────────────
  'sidebar.newTask': '新建任务',
  'sidebar.archived': '已归档',
  'sidebar.integrations': '集成',
  'sidebar.noJobs': '还没有任务。',
  'sidebar.resize': '调整任务列表宽度',
  'sidebar.showMore': '显示另外 {count} 条',
  'sidebar.newTaskIn': '在 {project} 中新建任务',
  'sidebar.runningOrQueued': '{count} 个运行中或排队中',
  'sidebar.pin': '置顶 {title}',
  'sidebar.unpin': '取消置顶 {title}',
  'sidebar.pinTask': '置顶任务',
  'sidebar.unpinTask': '取消置顶',
  'sidebar.archive': '归档 {title}',
  'sidebar.archiveTask': '归档任务',
  'sidebar.archiveFailed': '归档失败。',
  'sidebar.pinFailed': '置顶失败。',
  'sidebar.unpinFailed': '取消置顶失败。',
  'sidebar.openSettings': '打开设置',

  // ── Settings ────────────────────────────────────────────────────────────
  'settings.title': '设置',
  'settings.close': '关闭设置',
  'settings.done': '完成',
  'settings.signedInAs': '当前登录',

  'settings.appearance.title': '外观',
  'settings.appearance.theme': '主题',
  'settings.appearance.themeHint': '「跟随系统」会使用你操作系统的深浅色设置。',
  'settings.appearance.light': '浅色',
  'settings.appearance.dark': '深色',
  'settings.appearance.system': '跟随系统',

  'settings.language.title': '语言',
  'settings.language.label': '界面语言',
  'settings.language.hint': '只影响界面显示。你用什么语言跟 Agent 说话,它就用什么语言回答。',

  // ── Composer ────────────────────────────────────────────────────────────
  'composer.heading': '让 Agent 做什么?',
  'composer.subheading': '在隔离沙箱里运行,内部不含任何凭据 —— 产出是一个 draft PR。',
  'composer.placeholder':
    '描述一个任务…… 例如:修复 /v1/messages 上的 SSE 总超时回归,并补一个单元测试',
  'composer.placeholderDisconnected': '连接 GitHub 后才能描述任务',
  'composer.connectToRun': '连接 GitHub 后才能选仓库并运行',
  'composer.repository': '仓库',
  'composer.branch': '分支',
  'composer.agent': 'Agent',
  'composer.model': '模型',
  'composer.reasoning': '推理强度',
  'composer.run': '运行',
  'composer.starting': '启动中…',
  'composer.runTitle': '运行 —— ⏎ 发送,⇧⏎ 换行',
  'composer.network': '网络:准备阶段 {setup} · Agent 阶段 {agent}',
  'composer.loadConfigFailed': '无法加载配置',
  'composer.startFailed': '无法启动任务',
  'connect.title': '先连接 GitHub',
  'connect.titleUnavailable': 'GitHub 尚未开放配置',
  'connect.body': '选择 Agent 可以工作的仓库。凭据不会进入沙箱。',
  'connect.bodyUnavailable': '请联系管理员为 cloud agent 开通仓库访问。',
  'connect.action': '连接 GitHub',
  'connect.actionUnavailable': '查看集成',

  // ── Shared ──────────────────────────────────────────────────────────────
  'common.loading': '加载中…',
};
