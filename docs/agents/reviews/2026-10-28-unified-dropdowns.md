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
| `src/components/ui/menuStyles.ts` | One surface (`MENU_SURFACE`) for menu panels, `MENU_ACTIONS` for row-only action panels that scroll as a whole, anchored/composer placement, headings, dividers, footers, search, empty/status, row density. Plain rows keep hover and focus backgrounds; selected rows own the accent background; destructive rows own the error tint; disabled rows dim and cannot be hovered. All values use `border`, `surface-raised`, `surface-muted`, `foreground`, `muted`, `accent`, `status-error`. |
| `src/components/ui/anchoredPopover.ts` | `useAnchoredPopover`: fixed placement below/above the trigger, viewport/visual-viewport clamping, max height plus internal scrolling, follows scroll and resize, observes content/trigger resize for async options, and corrects for a transformed/filtered containing block (such as a dialog that animates in) instead of assuming the viewport origin. `popoverContainer` keeps a popup inside its modal dialog; `keepInView` scrolls only the list. |
| `src/components/ui/usePopupMenu.ts` | Shared action-menu keyboard/dismissal contract: focuses the first/last/selected row only after placement makes the panel visible, arrow/Home/End wrapping, Escape closes the top menu and restores focus, Tab resumes the page order, outside press/focus dismissal, placement. |
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
- `RepositoryPicker`: shared `MENU_COMPOSER` flex containment (so the row list
  is the part that shrinks and scrolls), search/footer, selected row and
  checkmark together, plus its richer content (temporary workspace,
  owner/name split, Create/Configure/Refresh) and existing async
  refresh/configure/return-focus behavior.
- `ComposerPlusMenu`, `ComposerCommandMenu`, MCP and SSH submenus: shared rows,
  headings, dividers, search and footer styling.
- `AccountMenu` (account, appearance, language, help) uses the shared selected
  row plus its existing `aria-checked`; `SidebarViewMenu`, `ThreadActions`,
  `GoalActionsMenu`, archive project menu, environment row menu, skill row
  menu, transcript display menu, admin server actions, admin settings overflow
  action, integrations and SSH settings "Manage" menus use the scrolling
  action surface (`MENU_ACTIONS`) or an explicit bounded max height, so short
  viewports scroll instead of clipping the actions below the fold.
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
- `npm test` — 274 files / 4427 tests passed.
- `npm run test:browser -- e2e/menus.spec.ts` — 10 passed: composer
  model/agent pickers, repository menu, account menu, sidebar view menu, task
  actions menu and admin form `Select` in desktop light and mobile dark; plus
  real-browser first-row/End focus for action menus, a 50-repository mobile
  panel whose list scrolls while the footer stays visible, and a fixed picker
  opened under a transformed ancestor. Screenshots are written by Playwright
  to `test-results/<test>/…` as
  `composer-model-picker-{desktop-light,mobile-dark}.png`,
  `composer-agent-picker-…`, `repository-menu-…`, `account-menu-…`,
  `sidebar-view-menu-…`, `task-actions-menu-…`, `admin-select-…`.
- `npm run test:browser -- e2e/dropdowns.spec.ts e2e/phone-layout.spec.ts` —
  pass (existing database/select, modal Escape/Tab, suggestion and phone
  layout coverage).
- Unit coverage added: `src/components/ui/usePopupMenu.test.tsx` (focus order,
  Escape ownership, layered Escape, outside dismissal, plain/selected/
  destructive/disabled row tokens); `usePopupMenu.unplaced.test.tsx` (focus
  stays on the trigger while the panel is still unplaced) and
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
