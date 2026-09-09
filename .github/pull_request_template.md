## What and why

<!-- What changes, and what problem it solves. Link the plan task ID (e.g. D2, E6). -->

## Migration checklist

Delete this section if the PR is not a migration task.

- [ ] **Move as-is** — imports and paths only. No renames, no file splitting, no
      reformatting, no dead-code removal, no drive-by fixes.
- [ ] Diff against a pristine copy from the source repo shows only import/path lines.
- [ ] Ported tests are unmodified except for imports and fixtures. **No assertion
      was weakened.** Any test that could not pass unmodified is reported in this
      PR description rather than adapted.
- [ ] No `serving.*` import anywhere under `cloud_agent/` or `cloud_agent_host/` (CI enforces this).
- [ ] No gateway secret (`API_KEY_SECRET`, `JWT_SECRET_KEY`) is read, stored, or
      referenced.

## General checklist

- [ ] `make check` is green.
- [ ] No secrets, tokens, keys, or internal hostnames in the diff. This repository
      is intended to become public.
- [ ] New environment variables are documented in `ENVIRONMENT.md`.
- [ ] Database changes ship as an Alembic migration, not startup DDL.

## Verification

<!-- What you actually ran, and what it printed. Not what you expect to happen. -->
