# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Architecture Overview

CVAT (Computer Vision Annotation Tool) is a monorepo with these main components:

- **Backend (Django)**: `cvat/` - Python REST API with apps in `cvat/apps/` (engine, dataset_manager, iam, functions, lambda_manager)
- **Frontend (React/TypeScript)**: `cvat-ui/` (main SPA), `cvat-core/` (client library), `cvat-canvas/` (annotation canvas), `cvat-data/` (media decoding)
- **Python Packages**: `cvat-sdk/` (SDK), `cvat-cli/` (CLI tool)
- **AI Models**: `ai-models/` - SAM2 tracker/interactor agents
- **Tests**: `tests/python/` (REST API/SDK/CLI), `tests/cypress/` (E2E)

## Essential Commands

### Frontend Development
```bash
corepack enable yarn
yarn --immutable                    # Install dependencies
yarn start:cvat-ui                  # Dev server on localhost:3000
yarn build:cvat-ui                  # Production build
yarn workspace cvat-ui run lint     # ESLint
```

### Backend Development
```bash
python manage.py runserver          # Dev server
python manage.py migrate            # Apply migrations
python manage.py makemigrations     # Create migrations
```

### Testing
```bash
# REST API/SDK/CLI tests
pytest ./tests/python                              # All tests
pytest ./tests/python/rest_api/test_tasks.py      # Single file
pytest ./tests/python -k test_name                 # Single test by name

# Backend unit tests
python manage.py test --settings cvat.settings.testing cvat/apps -v 2

# E2E tests
cd tests && yarn run cypress:run:chrome
```

### Linting
```bash
black --check --diff .              # Python formatting
isort --check --diff .              # Import sorting
pylint cvat                         # Python linting
yarn workspace cvat-ui run lint     # TypeScript linting
```

### Docker Services
```bash
# Start dev services
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build \
  cvat_opa cvat_db cvat_redis_inmem cvat_redis_ondisk cvat_server

# Stop services
docker compose -f docker-compose.yml -f docker-compose.dev.yml down -v
```

## Technology Stack

- **Backend**: Django 4.2+, DRF, PostgreSQL, Redis, RQ, OPA
- **Frontend**: React 18, Redux, TypeScript 5, Ant Design 5, Fabric.js
- **Testing**: pytest, Cypress, Vitest

## Development Setup

1. Start Docker services (PostgreSQL, Redis, OPA)
2. Install Python deps: `pip install -r cvat/requirements/development.txt`
3. Install frontend deps: `yarn --immutable`
4. Initialize DB: `python manage.py migrate && python manage.py createsuperuser`
5. Run UI: `yarn start:cvat-ui` (localhost:3000)
6. API available at localhost:8080

## Key Directories

- `cvat/apps/engine/` - Core annotation engine
- `cvat/apps/functions/` - AI functions/trackers
- `cvat-ui/src/components/` - React components
- `cvat-ui/src/actions/` and `reducers/` - Redux state
- `tests/python/rest_api/` - API integration tests
