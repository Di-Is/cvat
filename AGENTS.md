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
- SDK 単体の pytest をローカルで実行する際は、既存の開発スタックを停止したうえで
  `docker compose -f docker-compose.yml -f docker-compose.dev.yml -f tests/docker-compose.file_share.yml -f tests/docker-compose.minio.yml -f tests/docker-compose.test_servers.yml up -d`
  でテスト用付随サービスを起動し、`UV_HTTP_TIMEOUT=120 uv sync --group dev --group test --python 3.10` → `PYTHONPATH=$PWD/cvat-sdk UV_HTTP_TIMEOUT=120 uv run --python 3.10 python -m pytest tests/python/sdk/test_datasets.py -k basic` の順で実行する。fixtures が `docker` を直接操作するため、既存の `cvat_server/cvat_db` が動いているとテストは開始前に終了する点に注意。
- Server unit tests: `pip install -r cvat/requirements/testing.txt`, keep `cvat_opa` running, and execute `python manage.py test --settings cvat.settings.testing cvat/apps -v 2` or `coverage run manage.py test ...`.

## Commit & Pull Request Guidelines
Use the existing pattern: imperative subject plus scope and PR reference (`Fix auth tokens (#9981)`). Commits must build, include migrations when models move, and update docs/config when behavior changes. PRs should explain motivation, list validation commands, link issues, and attach UI screenshots or recordings; call out new env vars or compose overlays explicitly.

## Note
- Meta社のSAM2モデルをCVATに取り込んでいきます。CVAT謹製のEnterpriseとOnlineのプランで利用可能のSAM2に相当する機能を実装したい。
- pythonの環境構築はuvを使用してください。可能な限り`uv pip`は使用せず、`uv add/remove/lock/sync/run`のプロジェクト管理用のAPIを使用してください。pyproject.tomlやuv.lockが存在しない状態のpython環境を編集する際は最初にuvで再現可能な環境を構築してください。
- Pythonテストを動かす手順:
  1. `source ~/.cargo/env` で Rust PATH を有効化します。
  2. ルートで `UV_HTTP_TIMEOUT=120 uv sync --group dev --group test --python 3.10` を実行し、`.venv` を構築します。
  3. `uv run python manage.py test --keepdb <test-label>` で Django テストを実行します。既存の `test_cvat` DB が残っている場合は `--keepdb` を指定するか、外部ツールで drop してください。
  4. SAM2 tracker の局所検証は `uv run python manage.py test --keepdb cvat.apps.functions.tests.test_api.FunctionsApiTests.test_tracker_action_appends_outside_keyframe_after_target cvat.apps.functions.tests.test_api.FunctionsApiTests.test_tracker_action_removes_existing_shapes_beyond_target` を推奨します。
- サーバーのエンドポイント 192.168.10.190:8080 です。アクセス時にはヘッダー'Host: 192.168.10.190'の付与してください。
- ルートユーザーの資格情報は admin:admin です。
- SAM2はInteractorとTrackerに導入しようとしています。
- TrackerはPolygonかMaskオブジェクトを指定し、Run annotation Actionから呼び出します。
- Interactor/TrackerはGPU実行を仮定し、RTX 4080(VRAM 16GB)を使用する仮定で作業を行う。
- SAM2 tracker function を `cvat-cli function create-native` で登録する際は `--supports-batched-tracker`（デフォルト有効）を使い、batch tracking capability を忘れず設定してください。独自 tracker で無効化する場合は `--no-supports-batched-tracker` を明示します。
- SAM2 trackerのパフォーマンスでは1フレームあたりの処理時間に注目します。warm upなどの1度きりの操作は相対的に重視しません。
