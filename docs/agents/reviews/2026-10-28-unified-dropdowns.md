# Unified dropdowns — coverage inventory and validation record

PR: #1033 (`codex/ui/dropdown-consistency`), separate from personal-model
connection PR #1029. Reference principles were read from the current
HybridInference snapshots (`Select.tsx`, `anchoredPopover.ts`,
`ModelPicker.tsx`) and adapted to Cloud Agent's semantic theme tokens. No
backend or gateway code was imported.

## What changed

### Shared primitives

| Primitive | Purpose |
|---|---|
| `src/components/ui/menuStyles.ts` | One surface (`MENU_SURFACE`), anchored/composer placement, heading, divider, footer, search, empty/status, row density, selected/disabled/destructive row treatments, bounded scrolling. All values use `border`, `surface-raised`, `surface-muted`, `foreground`, `muted`, `accent`, `status-error`. |
| `src/components/ui/anchoredPopover.ts` | `useAnchoredPopover`: fixed placement below/above the trigger, viewport/visual-viewport clamping, max height plus internal scrolling, follows scroll and resize, measures the content once. `popoverContainer` keeps a popup inside its modal dialog; `keepInView` scrolls only the list. |
| `src/components/ui/usePopupMenu.ts` | Shared action-menu keyboard/dismissal contract: first/last focus, arrow/Home/End wrapping, Escape closes the top menu and restores focus, Tab resumes the page order, outside press/focus dismissal, placement. |
| `src/components/ui/useTopLayerEscape.ts` | The innermost popup owns Escape, in the capture phase, even when Chromium leaves focus on the trigger after a pointer press. A nested layer can consume the key first. |
| `src/components/ui/Select.tsx`, `SuggestionInput.tsx` | Migrated their panel to the shared surface/row/search/placement; retained form validation, refs, grouped options, typeahead and dialog containment. |
| `src/components/features/agents/composerMenuStyles.ts` | Now derives from the shared tokens instead of a second composer palette. |

### Selection surfaces migrated or reused

- Shared `Select`: admin database (table/column/operator/sort/rows), admin
  conversations, usage, sandboxes filters and batch actions, measurements,
  host/compute forms, admin settings choices, archived filters, environment
  dialog/manager/variable editor, MCP add/import/catalog, repository memory,
  Git panel, trajectory agent filter. These reuse the shared control and menu;
  only their trigger frame (compact toolbar vs. form field) is intentional.
- `SuggestionInput`: default-model/admin string suggestions. Same shared
  surface, free text and native field semantics preserved.
- Composer `Picker` (agent, model, reasoning, branch, follow-up): shared surface,
  search, rows, placement and dialog container. Compact and form (`field`)
  triggers remain intentionally different sizes.
- `RepositoryPicker`: shared surface/rows/search/footer, plus its own richer
  content (temporary workspace, owner/name split, Create/Configure/Refresh),
  bounded scrolling and existing async refresh/configure/return-focus behavior.
- `ComposerPlusMenu`, `ComposerCommandMenu`, MCP and SSH submenus: shared rows,
  headings, dividers, search and footer styling.
- `AccountMenu` (account, appearance, language, help), `SidebarViewMenu`,
  `ThreadActions`, `GoalActionsMenu`, archive project menu, environment row
  menu, skill row menu, transcript display menu, admin server actions, admin
  settings overflow action, integrations and SSH settings "Manage" menus:
  shared surface and row treatments.
- Sidebar search/filter chips and archived search/filter controls moved off the
  ad-hoc gray/white palette onto semantic tokens.

### True dialog exceptions (kept as dialogs)

These are task flows, not value pickers; they keep their richer content,
forms, focus containment and confirmation steps, and only their containing
panel style was left to the existing dialog conventions:

- `EnvironmentPicker` / `WorkspaceEnvironment`: choose from radio cards with
  variable counts, badges, a details disclosure, create/edit/test actions.
- `SkillSelectionDialog` / `PluginSelectionDialog`: multi-select with switches,
  byte/limit validation and apply/cancel.
- `McpAddDialog`, `SkillDialog`, `SshServerDialog`/`SshCheckDialog`,
  `CreateRepositoryDialog`, `NativeQuestionPrompt`, rename/archive delete
  dialogs, `ApplicationSettingGroup` review dialog.
- `AddWorkspace` remains a rich searchable popup, now on the shared surface.

## Verification

Run on Node 22 in the frontend directory unless noted.

- `npm run lint` — pass.
- `npx prettier --check "src/**/*.{ts,tsx}" "e2e/**/*.ts"` — pass.
- `npx tsc --noEmit` — pass.
- `npm test` — 272 files / 4421 tests passed.
- `npm run test:browser -- e2e/menus.spec.ts` — 6 passed (desktop light and
  mobile dark): composer model/agent pickers, repository menu, account menu,
  sidebar view menu, task actions menu, admin form `Select`. Screenshots are
  written by Playwright to `test-results/<test>/…` as
  `composer-model-picker-{desktop-light,mobile-dark}.png`,
  `composer-agent-picker-…`, `repository-menu-…`, `account-menu-…`,
  `sidebar-view-menu-…`, `task-actions-menu-…`, `admin-select-…`.
- `npm run test:browser -- e2e/dropdowns.spec.ts e2e/phone-layout.spec.ts` —
  pass (existing database/select, modal Escape/Tab, suggestion and phone
  layout coverage).
- Unit coverage added: `src/components/ui/usePopupMenu.test.tsx` (focus order,
  Escape ownership, layered Escape, outside dismissal, shared row tokens) and
  `src/components/features/agents/RepositoryPicker.test.tsx` (sorting,
  deduplication, search, Enter/Escape, footer actions, disconnected state).

No production demo routes or provider calls are used: the Playwright fixture
serves every API response and asserts the fixture saw no unknown routes or
task creation.

## Limits

- The screenshot paths above are Playwright output artifacts, not committed
  binary fixtures.
- Menus built on native `<details>` (Integrations and SSH "Manage") share the
  surface, rows and destructive treatment but keep the browser's own
  disclosure keyboard behavior rather than the `usePopupMenu` contract; they
  are not selection dropdowns.
- The legacy `.ui-popover` fade was removed (the class is no longer used), so
  menus render in their final state and review snapshots are deterministic.
