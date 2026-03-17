# Render Beta Branch Promotion Workflow

Use this workflow when the public beta should stay on one stable Git branch while normal development continues on `main`.

This keeps the current Render beta environment stable without blocking planned work on `main`.

## Recommended Branch Roles

- `main`
  - ongoing planned development
  - new stories and normal integration work land here first
- `beta-YYYY-MM-DD`
  - the branch currently deployed to the public beta Render service
  - only receives approved beta fixes
- short-lived feature branches
  - branch from `main`
  - merge to `main` first
  - only promote to beta after the fix is validated and explicitly approved

## Core Rule

Do not treat the public beta branch as the main place to build new work.

The safe default is:

1. implement on a feature branch from `main`
2. merge to `main`
3. cherry-pick the exact approved fix commit(s) onto the beta branch
4. deploy the beta branch to the existing Render beta service

Tracked record requirement:

- record every promoted or direct beta hotfix in [Render beta hotfix ledger](./render-beta-hotfix-ledger.md)
- keep the `Main sync status` field current until the same fix is safely mirrored onto `main` or explicitly rejected

## One-Time Cutover To A Beta Branch

Run this once when you freeze the current public beta build.

### 1. Create The Beta Branch From Clean `main`

```bash
git checkout main
git pull --ff-only origin main
git checkout -b beta-2026-03-17
git push -u origin beta-2026-03-17
git tag beta-launch-candidate-2026-03-17
git push origin beta-launch-candidate-2026-03-17
```

### 2. Point Render At The Beta Branch

In the existing public beta Render web service:

1. open the service settings
2. change the tracked Git branch from `main` to `beta-2026-03-17`
3. prefer manual deploys for this beta service
4. save the settings

Recommended setting:

- auto deploy: off or manual

Reason:

- `main` should keep moving without automatically changing the public beta environment

### 3. Keep The Existing Beta Database

For the current single public beta service, keep the existing beta database attached to the beta service.

Do not point a second hosted environment at the same beta database unless you are intentionally sharing state.

If you later want a hosted `main` or staging environment too, give it:

- a separate Render web service
- a separate Postgres database
- separate webhook/base-URL config

### 4. Record The Frozen Beta Start Point

Capture this somewhere durable when you switch:

- beta branch name
- initial beta tag
- Render service name
- current deployed commit
- date the service stopped tracking `main`

## Day-To-Day Development After The Cutover

Normal work continues on `main`:

```bash
git checkout main
git pull --ff-only origin main
git checkout -b story-or-fix-branch
```

Default rule:

- new stories merge to `main`
- beta only receives selective cherry-picked fixes

## Per-Fix Promotion Workflow

Use this every time you want to promote a fix from `main` to the public beta branch.

### 1. Land The Fix On `main` First

Finish the work on a normal feature branch and merge it to `main`.

Do not build the fix only on beta unless it is an emergency hotfix.

### 2. Identify The Exact Commit(s) To Promote

Choose the smallest complete commit set that fixes the beta issue.

Prefer:

- one fix commit
- or one small contiguous set of fix commits

Avoid promoting:

- unrelated story work
- follow-up refactors that are not required for the beta fix
- broad roadmap changes that happen to be nearby on `main`

### 3. Cherry-Pick Onto The Beta Branch

```bash
git checkout beta-2026-03-17
git pull --ff-only origin beta-2026-03-17
git cherry-pick <commit-sha>
```

If multiple commits are required:

```bash
git cherry-pick <sha1> <sha2> <sha3>
```

If there is a conflict:

1. resolve it on the beta branch
2. run the targeted validation
3. complete the cherry-pick

### 4. Run Targeted Validation Before Deploy

Always rerun the smallest relevant validation for the promoted fix.

Examples:

- UI copy or route change:
  - targeted `tests/test_ui_auth_shell.py`
- reporting or billing behavior:
  - focused reports or phase validation tests
- auth or support-request fix:
  - focused auth/support UI tests
- deploy/bootstrap fix:
  - startup smoke or deploy-specific tests

If the fix touches migrations, add:

- migration workflow checks from [Render public-beta migration and recovery](./render-public-beta-migration-recovery.md)

### 5. Push The Beta Branch

```bash
git push origin beta-2026-03-17
```

### 6. Verify Render Is Still Tracking The Beta Branch

Before deploying, confirm in Render:

- service branch is still `beta-2026-03-17`
- you are not about to deploy `main` accidentally
- the service still points at the intended beta database

### 7. Deploy The Beta Branch To Render

Use a manual deploy of the latest commit on `beta-2026-03-17`.

Record:

- beta branch name
- deployed commit SHA
- deploy timestamp

### 8. If The Fix Includes Migrations, Follow The Migration Checklist

Do not rely on a normal code deploy alone.

For migration-bearing fixes:

1. take the logical backup
2. deploy the beta branch revision
3. run:

```bash
alembic upgrade head
python scripts/render_startup_smoke.py --require-schema
```

4. then continue with browser/operator checks

### 9. Run Minimum Post-Deploy Smoke Checks

At minimum:

1. `GET /health`
2. `python scripts/render_startup_smoke.py --require-schema`
3. one browser check for the affected surface
4. if relevant, authenticated `GET /reports/health`

For higher-risk fixes, rerun the appropriate subset of:

- [Render public-beta launch checklist](./render-public-beta-launch-checklist.md)
- [Render public-beta operations](./render-public-beta-operations.md)
- [Render public-beta UX QA packet](./render-public-beta-ux-qa-packet.md)
- [Render public-beta master QA plan](./render-public-beta-master-qa-plan.md)

### 10. Record The Promotion

Capture:

- promoted commit SHA(s)
- reason for promotion
- tests rerun locally
- staging validation result
- Render smoke results
- whether a migration was involved
- any follow-up to also apply back on `main`

Preferred tracked location:

- [Render beta hotfix ledger](./render-beta-hotfix-ledger.md)

## Emergency Hotfix Exception

If you must patch the beta branch directly first:

1. make the smallest possible direct beta fix
2. validate it
3. deploy it
4. immediately cherry-pick that same commit back onto `main`

Do not leave beta-only code drifting away from `main`.

When using this exception, also record:

- the direct beta commit SHA
- the matching `main` sync plan or resulting `main` commit SHA
- whether the beta fix was first staging-validated before the public beta deploy

## Render Change Checklist

Use this checklist every time you promote a fix.

### Branch And Deploy Mode

- Render service branch is the beta branch, not `main`
- deploy mode is still manual or controlled
- the commit being deployed matches the intended beta branch commit

### Environment Integrity

- `TRACKED_LINK_BASE_URL` still matches the public beta host
- `MAGIC_LINK_BASE_URL` still matches the public beta host
- `STRIPE_CONNECT_REDIRECT_URI` still matches the public beta host plus `/stripe/connect/callback`
- webhook secrets and email settings are unchanged unless the fix explicitly requires them

### Database Safety

- the beta service still points to the intended beta Postgres database
- no separate hosted `main` or preview app is writing into the same beta database by accident

### Post-Deploy Checks

- `GET /health` passes
- schema-required smoke passes
- affected browser surface works
- any affected operator/JSON surface works

## When To Recut The Beta Branch

Do not keep the beta branch forever if it accumulates many cherry-picks.

Recut a fresh beta branch from `main` when:

- several approved beta fixes have accumulated
- the cherry-pick history is getting messy
- you are preparing the next broader beta wave

Example:

```bash
git checkout main
git pull --ff-only origin main
git checkout -b beta-2026-04-01
git push -u origin beta-2026-04-01
```

Then switch Render to the new beta branch and retire the old one after the deploy is stable.

## Recommendation For The Current Repo

For the current public beta:

- cut one branch such as `beta-2026-03-17`
- point the existing Render beta service at that branch
- keep `main` moving normally
- promote only narrow approved fixes by cherry-pick
- do not create a second hosted environment against the same beta database

You do not need to change `render.yaml` just to freeze the beta onto a different Git branch.

Change `render.yaml` only if you later want tracked infrastructure for multiple hosted environments rather than one current public beta service.
