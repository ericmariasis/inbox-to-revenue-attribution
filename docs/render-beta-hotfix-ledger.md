# Render Beta Hotfix Ledger

Use this tracked ledger to record every beta-only or beta-promoted fix after the public beta branch is frozen.

Why this exists:

- `north-star/` is useful for local continuity but is gitignored locally
- the public beta branch should stay hotfix-only
- later `main` sync is safer when each beta fix has an explicit tracked record

Do not rely on merging the beta branch wholesale back into `main`.

Preferred rule:

1. record the beta fix here when it is approved
2. update the entry after local validation
3. update the entry after staging validation
4. update the entry after beta deployment
5. explicitly record whether the fix was cherry-picked or reapplied onto `main`

## Current Frozen Beta

- Beta branch: `beta-2026-03-17`
- Frozen base commit: `bcfde8d`
- Frozen base date: `2026-03-16`
- Public beta Render web service: `inbox-to-revenue-attribution-web`

## Entry Template

### YYYY-MM-DD - Short hotfix title

- Tracking id:
- Reason:
- Beta branch commit(s):
- Source branch:
- Source commit(s):
- Files touched:
- Local validation:
- Staging validation:
- Public beta deploy:
- Main sync status:
- Notes:

## Active Entries

### 2026-03-17 - Clickable magic-link email

- Tracking id: `beta-hotfix-magic-link-clickable`
- Reason: first beta feedback reported that the magic-link email was not clickable for a non-Gmail recipient; the SMTP email was plain-text-only and likely depended on client auto-linking behavior
- Beta branch commit(s): `2bbdb99`
- Source branch: `beta-hotfix-magic-link-clickable`
- Source commit(s): `ae17d19`
- Files touched:
  - `app/services/email_provider.py`
  - `tests/test_email_provider.py`
- Local validation:
  - `.venv\Scripts\python.exe -m py_compile app\services\email_provider.py tests\test_email_provider.py tests\test_auth_magic_link_start.py tests\test_auth_magic_link_verify.py`
  - `.venv\Scripts\python.exe -m pytest -q tests\test_email_provider.py` (`5 passed`)
  - `.venv\Scripts\python.exe -m pytest -q tests\test_auth_magic_link_start.py` (`13 passed`)
  - `.venv\Scripts\python.exe -m pytest -q tests\test_auth_magic_link_verify.py` (`8 passed`)
- Staging validation: pending on the separate Render staging web service and staging Postgres database created on 2026-03-17
- Public beta deploy: pending
- Main sync status: pending; if staging confirms the root cause, reapply or cherry-pick the same provider-format change onto `main`
- Notes:
  - intended code delta versus frozen beta is only the multipart magic-link email change plus focused provider test updates
  - accidental merge commit `224cb73` briefly pulled `main` Story 95 and Story 96 work onto `beta-2026-03-17`
  - beta was restored to the frozen baseline by revert commit `66fc926`
  - the standalone email hotfix was then applied cleanly as `2bbdb99`
