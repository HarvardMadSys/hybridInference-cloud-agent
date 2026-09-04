/**
 * The source dictionary.
 *
 * English is the key set: every other language is typed as a total map over
 * these keys, so adding a string here fails the type-check until it has been
 * translated everywhere. That is deliberate — a language that silently falls
 * back for half its screens is worse than one we never shipped, because the
 * reader cannot tell the difference between "untranslated" and "broken".
 *
 * Placeholders are `{name}` and are substituted by `translate()`.
 */
export const en = {
  // ── Shell chrome ────────────────────────────────────────────────────────
  'nav.aria': 'Product navigation',
  'nav.status': 'Status',
  'nav.docsAssistant': 'Docs Assistant',
  'nav.agents': 'Agents',
  'nav.admin': 'Admin',
  'nav.dashboard': 'Dashboard',
  'nav.logOut': 'Log out',
  'nav.expandTaskList': 'Expand task list',
  'nav.collapseTaskList': 'Collapse task list',

  'shell.internalRequired.title': 'Administrator access required',
  'shell.internalRequired.body':
    "The agent sandbox runs code under this deployment's own credentials, so it is limited to administrators.",
  'shell.internalRequired.back': 'Back to Dashboard',

  // ── Sidebar ─────────────────────────────────────────────────────────────
  'sidebar.newTask': 'New task',
  'sidebar.archived': 'Archived',
  'sidebar.integrations': 'Integrations',
  'sidebar.noJobs': 'No jobs yet.',
  'sidebar.resize': 'Resize task list',
  'sidebar.showMore': 'Show {count} more',
  'sidebar.newTaskIn': 'New task in {project}',
  'sidebar.runningOrQueued': '{count} running or queued',
  'sidebar.pin': 'Pin {title}',
  'sidebar.unpin': 'Unpin {title}',
  'sidebar.pinTask': 'Pin task',
  'sidebar.unpinTask': 'Unpin task',
  'sidebar.archive': 'Archive {title}',
  'sidebar.archiveTask': 'Archive task',
  'sidebar.archiveFailed': 'Could not archive the task.',
  'sidebar.pinFailed': 'Could not pin the task.',
  'sidebar.unpinFailed': 'Could not unpin the task.',
  'sidebar.openSettings': 'Open settings',

  // ── Settings ────────────────────────────────────────────────────────────
  'settings.title': 'Settings',
  'settings.close': 'Close settings',
  'settings.done': 'Done',
  'settings.signedInAs': 'Signed in as',

  'settings.appearance.title': 'Appearance',
  'settings.appearance.theme': 'Theme',
  'settings.appearance.themeHint': 'System follows your operating system setting.',
  'settings.appearance.light': 'Light',
  'settings.appearance.dark': 'Dark',
  'settings.appearance.system': 'System',

  'settings.language.title': 'Language',
  'settings.language.label': 'Display language',
  'settings.language.hint':
    'Changes the interface only. Agents keep answering in whatever language you write to them in.',

  // ── Composer ────────────────────────────────────────────────────────────
  'composer.heading': 'What should the agent do?',
  'composer.subheading':
    'Runs in an isolated sandbox. No credentials inside — the output is a draft PR.',
  'composer.placeholder':
    'Describe a task… e.g. Fix the SSE total-timeout regression on /v1/messages and add a unit test',
  'composer.placeholderDisconnected': 'Connect GitHub to describe a task',
  'composer.connectToRun': 'Connect GitHub to select a repository and run',
  'composer.repository': 'Repository',
  'composer.branch': 'Branch',
  'composer.agent': 'Agent',
  'composer.model': 'Model',
  'composer.reasoning': 'Reasoning',
  'composer.run': 'Run',
  'composer.starting': 'Starting…',
  'composer.runTitle': 'Run — ⏎ to send, ⇧⏎ for a new line',
  'composer.network': 'Network: setup {setup} · agent {agent}',
  'composer.loadConfigFailed': 'could not load config',
  'composer.startFailed': 'could not start the job',
  'connect.title': 'Connect GitHub to start',
  'connect.titleUnavailable': 'GitHub setup is not available yet',
  'connect.body':
    'Choose the repositories the agent can work on. Credentials never enter the sandbox.',
  'connect.bodyUnavailable':
    'Contact your administrator to enable repository access for cloud agents.',
  'connect.action': 'Connect GitHub',
  'connect.actionUnavailable': 'View integrations',

  // ── Shared ──────────────────────────────────────────────────────────────
  'common.loading': 'Loading…',
} as const;

export type TranslationKey = keyof typeof en;
