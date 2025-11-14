# Repository Guidelines

## Project Structure & Module Organization
CVAT is a monorepo: `cvat/` contains the Django backend and workers, `cvat-ui/` + `cvat-core/` + `cvat-canvas*` ship the React/TypeScript clients, and `cvat-data/` holds dataset helpers. `cvat-sdk/` and `cvat-cli/` provide the Python SDK and CLI with integration specs in `tests/python`. Cypress specs and fixtures live in `tests/`. Optional services (serverless, analytics, IAM extras) reside in `components/`, while deployment assets live next to `docker-compose*.yml`, `helm-chart/`, and `dev/`. Keep cross-module utilities inside `utils/` and user-facing docs in `site/`.

## Build, Test, and Development Commands
- `docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build` spins up the full stack for development.
- `docker compose -f docker-compose.yml -f docker-compose.dev.yml build cvat_server` rebuilds the backend image after Python changes.
- `yarn workspace cvat-ui run start` launches the React dev server on port 3000 with API proxying.
- `yarn workspace cvat-core run build` rebuilds shared TS packages; reuse for `cvat-canvas*` and `cvat-data`.

## Coding Style & Naming Conventions
Python targets 3.9+, 4-space indent, and must satisfy Black (100 chars) plus isort’s Black profile (`pyproject.toml`). Favor explicit typing (dataclasses or TypedDicts instead of raw dicts) and keep functions side-effect-light. Frontend code uses TypeScript 5, ESLint (Airbnb + @typescript-eslint), and Prettier-compatible formatting; components/classes use PascalCase, hooks camelCase, constants UPPER_SNAKE. SCSS follows Stylelint standard-scss with BEM-like selectors. Markdown under `site/` is linted by remark-lint, so keep headings short and links absolute.

## Testing Guidelines
- Cypress E2E: `docker compose -f docker-compose.yml -f docker-compose.dev.yml -f components/serverless/docker-compose.serverless.yml -f tests/docker-compose.minio.yml -f tests/docker-compose.file_share.yml up -d`, then `cd tests && yarn --immutable && yarn run cypress:run:chrome` (append `:canvas3d` for 3D). `yarn run coverage` instruments bundles.
- REST API/SDK/CLI: `pip install -e ./cvat-sdk -e ./cvat-cli -r tests/python/requirements.txt` then `pytest ./tests/python --start-services --rebuild --cov` to cycle containers and gather coverage.
- Server unit tests: `pip install -r cvat/requirements/testing.txt`, keep `cvat_opa` running, and execute `python manage.py test --settings cvat.settings.testing cvat/apps -v 2` or `coverage run manage.py test ...`.

## Commit & Pull Request Guidelines
Use the existing pattern: imperative subject plus scope and PR reference (`Fix auth tokens (#9981)`). Commits must build, include migrations when models move, and update docs/config when behavior changes. PRs should explain motivation, list validation commands, link issues, and attach UI screenshots or recordings; call out new env vars or compose overlays explicitly.

## Note
- EnterpriseとOnlineのプランは利用しません。私との回答はOSS番に焦点を絞ってください。
