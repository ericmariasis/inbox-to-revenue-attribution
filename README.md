# Inbox to Revenue Attribution

System that attributes paid revenue to content posts.

## Docs

Architecture and design docs are in /docs

- [Render public-beta bootstrap](./docs/render-public-beta-bootstrap.md)
- [Render public-beta operations](./docs/render-public-beta-operations.md)
- [Render public-beta migration and recovery](./docs/render-public-beta-migration-recovery.md)

## Development

Run locally:

uvicorn app.main:app --reload

## CI

Automated tests run in GitHub Actions:

- Workflow: [tests.yml](./.github/workflows/tests.yml)
