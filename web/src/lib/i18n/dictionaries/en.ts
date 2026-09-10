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
  'sidebar.projects': 'Projects',
  'sidebar.viewOptions': 'View options',
  'sidebar.groupBy': 'Group by',
  'sidebar.orderBy': 'Order by',
  'sidebar.byProject': 'Project',
  'sidebar.inOneList': 'In one list',
  'sidebar.manual': 'Manual',
  'sidebar.lastUpdated': 'Last updated',
  'sidebar.searchScope': 'Search scope',
  'sidebar.searchScopeUnavailable': 'Open a project to limit search to it',
  'sidebar.searchAllProjects': 'Search all projects',
  'sidebar.searchCurrentProject': 'Search only {repo}',
  'sidebar.search': 'Search tasks',
  'sidebar.searchPlaceholder': 'Search titles or repositories…',
  'sidebar.allProjects': 'All projects',
  'sidebar.currentProject': 'Current project',
  'sidebar.includeArchived': 'Include archived',
  'sidebar.noMatches': 'No matching tasks. Try another title or repository.',
  'sidebar.loadMore': 'Show more',
  'sidebar.retry': 'Retry',
  'sidebar.closeSearch': 'Close search',
  'sidebar.reorderHint': 'Drag to reorder, or press Alt + Arrow Up / Down',
  'sidebar.moved': 'Order updated',
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
  'settings.backToWorkspace': 'Back to workspace',
  'settings.preferences': 'Preferences',
  'settings.conversations': 'Conversations',
  'settings.archived': 'Archived sessions',

  'sessionArchive.badge': 'Archived',
  'sessionArchive.back': 'Archived sessions',
  'sessionArchive.title': 'This session is archived',
  'sessionArchive.body':
    'You can read the conversation and browse files. Unarchive to continue working.',
  'sessionArchive.running':
    'This run is still active. Archiving does not stop it. Unarchive to send another message.',
  'sessionArchive.contextLost':
    'You can unarchive this session, but its native context is unavailable. Start a new session to continue working.',
  'sessionArchive.restore': 'Unarchive',
  'sessionArchive.continue': 'Unarchive and continue',
  'sessionArchive.restoring': 'Unarchiving…',
  'sessionArchive.restoreFailed': 'Could not unarchive this session.',
  'sessionArchive.terminal':
    'Unarchive this session to use its terminal. Git and files remain available for viewing.',

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

  // ── Archived sessions ───────────────────────────────────────────────────
  'archive.title': 'Archived sessions',
  'archive.subtitle': 'Revisit past work or organize sessions you no longer need.',
  'archive.search': 'Search archived sessions',
  'archive.statusFilter': 'Filter by status',
  'archive.projectFilter': 'Filter by project',
  'archive.allStates': 'All statuses',
  'archive.allProjects': 'All projects',
  'archive.state.running': 'Running',
  'archive.state.queued': 'Queued',
  'archive.state.needsReview': 'Needs review',
  'archive.state.done': 'Done',
  'archive.state.failed': 'Failed',
  'archive.state.cancelled': 'Cancelled',
  'archive.countOne': '1 session',
  'archive.count': '{count} sessions',
  'archive.archivedOn': 'Archived {date}',
  'archive.dateUnknown': 'Archive date unavailable',
  'archive.runningHint': 'Archiving does not stop execution.',
  'archive.unarchive': 'Unarchive',
  'archive.unarchiveSession': 'Unarchive {title}',
  'archive.unarchiving': 'Unarchiving…',
  'archive.unarchivedOne': '1 session returned to its original project',
  'archive.unarchived': '{count} sessions returned to their original projects',
  'archive.unarchiveFailed': 'Could not unarchive these sessions. Please try again.',
  'archive.undo': 'Undo',
  'archive.undone': 'Sessions archived again',
  'archive.undoFailed': 'Could not archive these sessions again. Please try again.',
  'archive.view': 'View',
  'archive.dismiss': 'Dismiss notification',
  'archive.projectActions': 'Actions for {project}',
  'archive.unarchiveGroup': 'Unarchive group ({count})',
  'archive.deleteGroup': 'Delete group ({count})',
  'archive.deleteSession': 'Delete {title}',
  'archive.deleteAll': 'Delete all',
  'archive.deleteFiltered': 'Delete filtered',
  'archive.deleteBusy':
    'This session is still active, preparing a fork, or publishing. Try again after it finishes.',
  'archive.preparing': 'Collecting matching sessions…',
  'archive.prepareFailed':
    'Could not collect all matching sessions. No changes were made. Please try again.',
  'archive.deleteTitleOne': 'Delete this session?',
  'archive.deleteTitle': 'Delete {count} archived sessions?',
  'archive.deleteBody':
    'Permanently remove these sessions from your session history. This cannot be undone. Shared workspaces and other sessions are unaffected.',
  'archive.deleteSkipped':
    '{count} active sessions are excluded, including sessions preparing a fork or publishing.',
  'archive.noDeletableTitle': 'No sessions can be deleted yet',
  'archive.noDeletable':
    'All matching sessions are still active. Try again after execution, fork preparation, and publication finish.',
  'archive.bulkLimit':
    'Select at most {count} sessions at a time. Narrow your search or project and status filters, then try again.',
  'archive.scopeSession': 'Session: {title}',
  'archive.scopeAll': 'Scope: all archived sessions',
  'archive.scopeFiltered': 'Scope: current search and filters',
  'archive.scopeProject': 'Scope: archived sessions in {project}',
  'archive.scopeProjectFiltered':
    'Scope: matching archived sessions in {project}, with the current search and status filters',
  'archive.andMore': 'And {count} more sessions',
  'archive.deleteCountOne': 'Delete 1 session',
  'archive.deleteCount': 'Delete {count} sessions',
  'archive.deleting': 'Deleting…',
  'archive.deleteFailed': 'Could not delete these sessions. Check your connection and try again.',
  'archive.sessionActive': 'A selected session is still active. Wait for its work to finish.',
  'archive.forkInProgress': 'A selected session is being forked. Wait for the fork to finish.',
  'archive.publicationPending':
    'A selected session has unfinished publication. Wait for it to finish.',
  'archive.sessionNotArchived': 'A selected session was unarchived. Archive it before deleting.',
  'archive.sessionNotFound': 'A selected session is no longer available.',
  'archive.selectionChanged': 'The selection has changed. Review the refreshed list and try again.',
  'archive.deletedOne': '1 session deleted',
  'archive.deleted': '{count} sessions deleted',
  'archive.cancel': 'Cancel',
  'archive.close': 'Close',
  'archive.loading': 'Loading archived sessions…',
  'archive.loadFailed': 'Could not load archived sessions.',
  'archive.refreshFailed': 'Could not refresh the archive. Displayed counts may be out of date.',
  'archive.retry': 'Try again',
  'archive.noResults': 'No matching sessions',
  'archive.noResultsHint': 'Try a different keyword or adjust your project and status filters.',
  'archive.empty': 'All caught up',
  'archive.emptyHint': 'Sessions you archive will appear here, ready to revisit.',
  'archive.groupEmpty': 'No archived sessions remain in this project.',
  'archive.clearFilters': 'Clear filters',
  'archive.moreSessions': 'Show more sessions ({count} remaining)',
  'archive.moreProjects': 'Show more projects ({count} remaining)',
  'archive.showing': 'Showing {count} of {total} sessions',
  'archive.order': 'Recently archived first',

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
