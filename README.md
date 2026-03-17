# Inbox to Revenue Attribution

System that attributes paid revenue to content posts.

## Docs

Architecture and design docs are in /docs

- [Render public-beta master QA plan](./docs/render-public-beta-master-qa-plan.md)
- [Render public-beta QA result - 2026-03-16](./docs/render-public-beta-qa-result-2026-03-16.md)
- [Render beta branch promotion workflow](./docs/render-beta-branch-promotion-workflow.md)
- [Render public-beta bootstrap](./docs/render-public-beta-bootstrap.md)
- [Render public-beta operations](./docs/render-public-beta-operations.md)
- [Render public-beta migration and recovery](./docs/render-public-beta-migration-recovery.md)
- [Render public-beta launch checklist](./docs/render-public-beta-launch-checklist.md)
- [Render public-beta UX QA packet](./docs/render-public-beta-ux-qa-packet.md)

## Development

Run locally:

uvicorn app.main:app --reload

## CI

Automated tests run in GitHub Actions:

- Workflow: [tests.yml](./.github/workflows/tests.yml)
