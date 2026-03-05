# Inbox to Revenue Attribution

System that attributes paid revenue to content posts.

## Docs

Architecture and design docs are in /docs

## Development

Run locally:

uvicorn app.main:app --reload

## CI

Automated tests run in GitHub Actions:

- Workflow: [tests.yml](./.github/workflows/tests.yml)
