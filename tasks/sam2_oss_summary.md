# SAM2 Tracker OSS対応 依頼サマリ

## 依頼内容・背景・ゴール
- **背景**: SAM2 トラッカーは公式ドキュメント上「CVAT Online / Enterprise」でのみ AI Agent として提供。docker compose で立ち上げる OSS 版では UI 上に「AI Tracker: SAM2」が表示されず、CLI からも関数登録/エージェント起動が行えない。
- **ユーザー依頼**: OSS 版で同じ機能を使いたいので、不足している機能を特定し、実装計画を立てたい。
- **ゴール**: OSS 版サーバ + フロントエンドを修正して、SAM2 トラッカーを UI から呼び出せる状態（CLI で `function create-native` / `run-agent` が動き、UI にアクションが表示され結果が反映される状態）を作る。

## 調査結果 (技術的ギャップ)

1. **REST API の欠落**
   - CLI は `/api/functions`（関数 CRUD）および `/api/functions/queues/**`（キュー監視・AR取得）のエンドポイントを使用するが、OSS サーバは `/api/lambda/**` のみ実装。`cvat.apps.lambda_manager` にも `FunctionViewSet` は存在するものの Nuclio（`/api/lambda/functions`) 専用で、ネイティブ関数と共有するデータモデルや URL は提供されていないため、CLI の native function ワークフローが成立しない。
   - 既存 `lambda_manager` は Nuclio 向けの同期呼び出しのみで、ネイティブ関数メタデータを保存するモデルも無い。

2. **Annotation Request (AR) キューとイベントの未実装**
   - CLI エージェントは `/api/functions/queues/{queue_id}/watch` の SSE と `/api/functions/queues/{queue_id}/requests/{acquire|complete|fail|update}` の HTTP API だけで AR を取得する設計（内部的な Redis 依存は無い）。サーバ側に該当データモデルや処理が無いため、エージェントが仕事を取得できない。
   - 進捗更新や失敗時の再割り当てなど、サーバ内で AR ライフサイクルを持つ処理が必要。

3. **UI / SDK の前提**
   - `server-proxy.ts` などは `/lambda` ベースで固定されており、ネイティブ関数一覧を取得して UI アクションに反映する仕組みが無い。
   - UI に組み込まれている唯一のトラッカーアクションは OpenCV TrackerMIL（完全クライアントサイド）で、リモート実行結果を待つコードパスや進捗 UI が SAM2 用には整備されていない。

4. **ドキュメント/設定の乖離**
   - 公式ドキュメントは AI Agent 版を CVAT Online / Enterprise 向けとしてのみ説明しており、docker compose ベースの OSS 手順は未記載。OSS へ拡張する場合は前提条件と導入手順、制約を追記する必要がある。

## 実装タスク案
1. **DB/モデル整備**
   - Django に `Function` / `FunctionLabel` / `AnnotationRequest` を追加し、`owner` と `kind`, `supported_shape_types` を `Function` 側で保持しつつ、ラベル定義は `FunctionLabel` モデルで管理する最小構成に絞る。
   - 共有・組織単位の概念や複数マイグレーションは設けず、1 本の migration でテーブル作成と index を完了させる。

2. **REST API 実装**
   - DRF ViewSet で `/api/functions` CRUD を提供し、簡易な `IsOwner` パーミッションのみを実装。
   - `/api/functions/queues/<queue_id>/watch` と `/api/functions/queues/<queue_id>/requests/{acquire|complete|fail|update}` を PostgreSQL ベースで構築し、`watch` は Server-Sent Events (SSE) で接続直後に `retry: 30000` を通知、以降は 2 秒おきに keep-alive コメントを送りつつ 30 秒でストリームを閉じてクライアントに再接続させる。

3. **サーバ実行フロー**
   - UI/CLI からの実行リクエストで `AnnotationRequest` を生成し、`pending → running → done/failed` の単純な状態遷移を Django サービス層にまとめる。
   - agent からの結果を `dm.task.patch_*` に連携するユーティリティを追加し、失敗時は `result.exc_info` に理由を格納。必要に応じて `manage.py reset_annotation_request <id>` でステータスを戻せるようにする。

4. **Agent / CLI / SDK**
   - Python SDK と CLI の `create-native` / `run-agent` を新 REST API に直結し、互換チェックや feature flag は実装しない。
   - `docker-compose.yml` に最小限の `sam2-agent` サービスを追加し、`CVAT_URL` と PAT を env で共有するだけの構成に抑える。

5. **フロントエンド**
   - `server-proxy.ts` へ `/api/functions` / queues API を紐付け、`annotations-actions-modal` に Pending/Running/Done の簡易ステータス表示と実行ボタンを追加する。
   - 進捗の再試行・キャンセルや複雑なトースト制御は入れず、ローディングスピナー＋結果反映のみ実装。

6. **テストとドキュメント**
   - DRF の正常系テストと SAM2 エージェントを使った手動動作手順を README へ記載。自動 E2E や Cypress は省略。
   - `site/content` の SAM2 記事へ OSS 手順と `sam2-agent` コンテナ追加方法を追記する。

## 第三者調査から取り込んだ着眼点

- **提供形態と UI 体験**: 公式ドキュメントにある通り、Nuclio 版は Enterprise 限定、AI Agent 版は CVAT Online/Enterprise 向けで、Run annotation action から **AI Tracker: SAM2** を選択する流れが標準とされている（`site/content/en/docs/annotation/auto-annotation/segment-anything-2-tracker.md:13-210`）。OSS で同等の体験を再現する際もこの UX を踏襲する。
- **AI Agent 制約**: 1 関数 1 エージェント、state をメモリ保持、agent 経由のみ利用といった制約が Limitations セクションに明記されている（`site/content/en/docs/annotation/auto-annotation/segment-anything-2-tracker.md:221-235`）。設計時に同等制約を前提にし、キャンセルやエラー表示を織り込む。
- **CLI/SDK 依存 API**: `function create-native` / `run-agent` は Enterprise/Cloud 限定で `/api/functions` を呼び出す設計になっており（`site/content/en/docs/api_sdk/cli/_index.md:380-417`, `cvat-cli/src/cvat_cli/_internal/commands_functions.py:98-135`）、OSS 側がこの REST 面を欠いていることがギャップの根本。
- **現状サーバ差分**: Django ルータは `/api/lambda/functions|requests` までしか公開しておらず `/api/functions/**` や `/api/functions/queues/**` は未実装。一方で `FunctionViewSet` は Nuclio 連携向けに存在するため、OSS でのネイティブ関数 API は新規追加になる（`cvat/apps/lambda_manager/urls.py:10-27`, `cvat/apps/lambda_manager/views.py:1234-1342`）。
- **Annotation Request 実装欠如**: CLI エージェントは `queues/{queue_id}/watch` や `requests/*` を利用するが、サーバには対応エンドポイントやモデルが無い（`cvat-cli/src/cvat_cli/_internal/agent.py:574-934` と `cvat/apps/lambda_manager/urls.py:10-27` の比較）。OSS 側でキュー＆SSE 管理を実装する必要がある。
- **UI/アクション登録の不足**: `core.actions.register` で登録されているのは OpenCV TrackerMIL のみで、ネイティブ関数を自動検出して進捗待機する構造が無い（`cvat-ui/src/utils/opencv-wrapper/opencv-wrapper.ts:317-334`）。SAM2 を UI へ出すには actions modal 連携を拡張する。
- **SAM2 実装資産**: `ai-models/tracker/sam2/func.py` には `SAM2VideoPredictor` ベースの OSS 実装が既に含まれているので、サーバ／UI 側が整えば Community でも推論自体は利用可能（`ai-models/tracker/sam2/func.py:16-200`）。
- **ドキュメント記述の整理**: 公式ドキュメントや AI ツール一覧は AI Agent SAM2 を CVAT Online/Enterprise 向けとしており、OSS については触れていない（`site/content/en/docs/annotation/auto-annotation/segment-anything-2-tracker.md:13-37`, `site/content/en/docs/annotation/tools/ai-tools.md:272-280`）。Community で提供を始める際はこの差分を doc 更新で明示する。

## 計画への反映
- **Backend**: `/api/functions` と `/api/functions/queues|requests` を Django + PostgreSQL のみで完結させ、Redis や feature flag を排除した最短ルートで SAM2 に必要な API 群を実装する。
- **Agent contract**: SDK/CLI/agent は新 API のみを前提にし、型定義も最小限（Pydantic/TypedDict）で揃える。互換性メッセージや capability チェックは行わず、失敗時は HTTP エラーをそのまま表示する。
- **UI/UX**: `server-proxy.ts` と `annotations-actions-modal` を中心に、SAM2 を選択→実行→結果反映までの直線的な UX を作る。進捗表示以外の高度な UI は後回し。
- **Docs & ops**: docker compose でのセットアップ手順と `sam2-agent` サービス定義を README / `site/content/.../segment-anything-2-tracker.md` に記述し、個人開発者がコピペで再現できる形にする。

## ローカル RTX 4080 前提での対応計画
### 前提整理
- 実行主体は 1 名、動作環境は docker compose + ローカル RTX 4080 のみ。
- SAM2 agent は GPU 常駐サービスとして compose に追加し、`facebook/sam2.1-hiera-small` を CUDA 実行する。
- サーバーは OSS docker compose を常時起動し、DB/queue は Postgres のみで完結させる。

### 進め方
1. **環境検証**: 既存 OSS compose が正常に立ち上がること、`ai-models/tracker/sam2/func.py` が単体で推論できることを確認する。
2. **Backend 実装**: `Function`/`AnnotationRequest` モデルと `/api/functions` + queues API を実装し、Postgres を SSE 通知のデータソースにする。追加の queue や Redis は導入しない。
3. **Agent / CLI / SDK**: 新 API と合致するよう CLI/SDK コマンドを修正し、`sam2-agent` コンテナ（`CVAT_URL`/`CVAT_TOKEN`）を compose へ足して実機で動作確認する。
4. **フロントエンド**: `server-proxy.ts` と `annotations-actions-modal` を更新し、SAM2 アクションを UI から選んで結果が反映されるまでの最小 UX を整える。
5. **動作確認・ドキュメント**: docker compose + agent の手動動作確認を実施し、再現手順を README / docs にまとめる。自動テストは DRF の最小ケースに限定する。

### ローカル運用補足
- `sam2-agent` は compose サービスとして追加し、`ai-models/tracker/sam2` ディレクトリを bind mount。GPU 有効化は `deploy.resources.reservations.devices`（`driver: nvidia`, `capabilities: [gpu]`, `SAM2_AGENT_GPU_COUNT`）を使い、追加の `runtime: nvidia` 記載は不要。
- 認証は PAT 1 つを `.env` に記載し、UI/CLI/agent で共通利用する。自動ローテーションや複雑な権限管理は行わない。
- ログは `cvat-cli function run-agent` の INFO/ERROR をそのまま標準出力へ流し、必要に応じて `docker compose logs sam2-agent` を確認する（追加の JSON 出力は行わない）。

### リスクと緩和
- **GPU 資源の単一性**: 4080 を UI/agent 共有で使うと GUI がカクつく可能性があるため、`CUDA_VISIBLE_DEVICES=0` で agent を固定し、必要に応じて `nice` や `nvidia-smi` で手動制御する。
- **State 喪失**: agent が途中で落ちても `annotation_requests` テーブルに状態が残るので再実行は可能だが、再開は手動 `manage.py reset_annotation_request` で行う方針を周知する。
- **API 追加の影響**: `/api/functions` が唯一の経路となるため、Enterprise との差分は README に明記し、旧 `/api/lambda/**` 利用者に影響が出る場合は個人環境でのみ使うことを前提にする。

## 個人利用向け実装詳細
- **AnnotationRequest 永続化**: Postgres の `annotation_requests` テーブルに `function` / `owner` / `task` / `job` / `category` / `type` / `status` / `parameters` / `result` / `progress` / `agent_id` と `created_at` / `updated_at` を保存し、サーバー再起動時は DB の値のみ参照する。Redis や再同期コマンドは作らず、未完了レコードは UI から再実行。
- **エージェント認証**: `.env` に `CVAT_AGENT_TOKEN=<token>` を記載し、UI/CLI/agent で共通利用。トークン期限切れ時は手動で更新し、コード側の自動更新は行わない。
- **キュー監視**: `/api/functions/queues/<queue_id>/watch` は SSE ストリーム API とし、クライアントは接続直後の `retry: 30000` と 2 秒ごとの keep-alive コメントを頼りに 30 秒ごとに再接続する。複雑なバックオフは導入しない。
- **ジョブデータの扱い**: `dm.task.put_job_data` / `delete_job_data` をそのまま呼び、必要であれば `/tmp/cvat-sam2-cache` 配下を `docker compose exec sam2-agent rm -rf /tmp/cvat-sam2-cache/*` のような手動削除で掃除し、専用管理コマンドは作らない。
- **UI テスト**: 進捗更新の happy path を Jest 1 ケースで担保し、Cypress 等の統合テストは作成しない。
- **ログ**: agent stdout には CLI の INFO/ERROR ログのみが出力される。必要時のみ `docker compose logs sam2-agent` を目視確認する。

### TODO
- [x] `Function` / `FunctionLabel` / `AnnotationRequest` の migration を追加し、owner ベースで参照できるようにする。
- [x] `/api/functions` CRUD と `/api/functions/queues|requests` の SSE ベース API を DRF で実装する。
- [x] agent からの結果を `dm.task.patch_*` に反映し、`manage.py reset_annotation_request` を追加する。
- [x] アクション実行時に `AnnotationRequest` を生成し、SAM2 トラッカー向けの init/track フローをサーバーで連携する処理を実装する。
- [x] Python SDK / core (`server-proxy.ts`, `cvat-core`) を新 API に合わせて拡張し、ジョブから SAM2 トラッカー実行・ステータス取得ができるようにした。
- [x] CLI コマンド (`function create-native` / `run-agent`) を新 API と同期し、`docker-compose.yml` へ `sam2-agent` サービスを追加する。
- [x] `server-proxy.ts` / `annotations-actions-modal` にネイティブ関数 UI を実装し、Pending/Running/Done 表示を追加する。
- [x] DRF 正常系テストと README / `site/content/.../segment-anything-2-tracker.md` の OSS 手順更新を行う。
- [x] `README.md` / `site/content/en/docs/contributing/development-environment.md` などのローカル開発向けドキュメントへ `sam2-agent` プロファイルの利用方法（`.env` サンプル含む）を反映する。
- [x] CLI 側で `function create-native` / `run-agent` の新しいエラー分岐をカバーする単体テストを追加し、`raise_if_functions_api_missing` の回帰を防ぐ。
- [x] `sam2-agent` イメージの CI ビルド（最低限 `docker build Dockerfile.sam2-agent`）を追加し、依存パッケージ更新時に壊れないことを確認する。

### 進捗メモ (2025-11-10)
- `cvat.apps.functions` に REST API を追加し、`Function` CRUD / queues / requests を `/api/functions` 配下で公開。`FunctionViewSet` + SSE ベースの `watch` エンドポイントを実装し、エージェントは 30 秒間のロングポーリングで通知を受け取れる。
- PostgreSQL をキューソースとする `AnnotationRequest` 取得・完了・失敗 API (`acquire`/`complete`/`fail`/`update`) を追加し、エージェントとのコントラクト（`agent_id`, `request_category`, 進捗更新）を OSS 版でも再現。
- `cvat/apps/functions/tests/test_api.py` を新設し、Function CRUD とキュー API のハッピーパス（取得→完了、進捗更新、失敗、watch ストリーム）をカバーする API テストを追加。
- agent 完了時に `result.annotations` を `dm.task.patch_*` へ適用する処理を実装し、`AnnotationRequest` が Done になると同時にタスク/ジョブへ反映されるようにした。`result_handlers.py` でデータ検証と書き込みを行い、API テストでは patch 呼び出しをモックして検証済み。
- `manage.py reset_annotation_request <uuid>` を追加し、`status/agent_id/progress/result` をリセットして再割り当てできるようにした。失敗時のリカバリー手順に利用予定。

### 進捗メモ (2025-11-11)
- `/api/functions/requests/<uuid>` と `/api/functions/runs/<uuid>` を追加し、UI から AnnotationRequest 単体および run 単位でステータス/進捗を取得できるようにした。失敗/進行状況の把握が API で完結。
- `cvat-core` にネイティブ関数 API を組み込み、`server-proxy.ts`・`api.ts`・`session.ts` で関数リスト取得とジョブからの tracker 実行 (`runFunctionTrackerAction`) を追加。リクエスト/ラン状態を SDK から参照可能にし、型定義も補完。
- `annotations-actions-modal` に SAM2 トラッカー用アクションを注入し、ネイティブ関数（`AI Tracker: <name>`）を自動登録。新クラス `NativeFunctionTrackerAction` で tracker 実行→ラン完了までポーリングし、進捗 UI (Pending/Running/Done) を反映。失敗時は `functions.requests` 詳細からエラーを表示。

### 進捗メモ (2025-11-11 / CLI & Compose)
- `cvat-cli` の `function create-native` / `function run-agent` で `/api/functions` が無いサーバーに接続した場合は `CriticalError` を投げ、ユーザーへ「CVAT 2.42+ へ更新 or `cvat.apps.functions` を有効化」が必要である旨を明示するようにした。
- `Dockerfile.sam2-agent`（`pytorch/pytorch:2.4.1-cuda12.1-cudnn9-runtime` ベース）と `dev/sam2-agent/entrypoint.sh` を追加し、`docker-compose.yml` に `profiles: [sam2-agent]` な `sam2-agent` サービスを組み込んだ。`SAM2_MODEL_ID` / `SAM2_FUNCTION_ID` / `SAM2_DEVICE` / `SAM2_AGENT_CVAT_URL` / `SAM2_EXTRA_AGENT_ARGS` など環境変数で CLI コマンドをパラメータ化し、Hugging Face キャッシュ用の `sam2_agent_cache` ボリュームも追加済み。
- `.env` に `CVAT_AGENT_TOKEN`（PAT）を置けば CLI / コンテナ両方が共有し、GPU 非搭載環境では `SAM2_DEVICE=cpu` にした上で `deploy.resources.reservations.devices` ブロックをコメントアウト（または `SAM2_AGENT_GPU_COUNT=0` を指定する）ことをドキュメントに記載。
- `site/content/en/docs/annotation/auto-annotation/segment-anything-2-tracker.md` に OSS 手順（ PAT 発行・`sam2-agent` プロファイル起動・環境変数サンプル）を追加し、`site/content/en/docs/api_sdk/cli/_index.md` の注意書きを「CVAT Online/Enterprise + OSS 2.42+」向けに更新した。

### 進捗メモ (2025-11-14 / Docs, CLI, CI)
- README と `site/content/en/docs/contributing/development-environment.md` に `sam2-agent` プロファイル用の `.env` サンプル、PAT 取得、`docker compose --profile sam2-agent` コマンドの最小手順を追記し、ローカル開発者が OSS で SAM2 トラッカーを起動できるようにした。
- `tests/python/cli/test_cli_misc.py` へ `function create-native` / `function run-agent` の `/api/functions` 欠落時シナリオを追加し、`ApiClient.call_api` が 404/405 を返した場合に `raise_if_functions_api_missing` が `CriticalError` を出すことを回帰テストで担保。
- `.github/workflows/main.yml` の CI `build` ジョブに `Dockerfile.sam2-agent` をビルドするステップを追加し、依存バージョン更新時にも agent イメージが壊れていないことを検証。

## SAM2.1 Interactor E2E 手動検証ログ (WIP)
- 詳細手順とログテンプレは `tasks/sam2_interactor_e2e.md` に切り出し。GPU 環境での再現が必要。
- 2025-11-15 時点で実施できた項目
  - `uv run --with cvat-cli` で Tracker/Interactor 関数を作成し、ID=1/2 を `.env` にセット。
  - compose で `sam2-*-agent` を起動すると `cvat-cli: not found` で再起動し続ける（pyproject に CLI を含める必要あり）。現状はホスト側で未リリース CLI/SKD を `PYTHONPATH` で読み込ませ、`function run-agent 2` を `nohup` で常駐させて代替。
  - REST `POST /api/jobs/{job}/functions/{function}/interactions` は `filters.py`（OrderingFilter の None ガード）と `permissions.py`（`function_interactions` → `UPDATE_ANNOTATIONS` スコープ）のホットパッチ後に 200 応答。`ai-models/interactor/sam2/func.py` では `torch.Tensor`→NumPy bool 変換を追加して `mask_rle` が戻ることを確認。
  - エージェントログ `/tmp/sam2_interactor_agent.log` に `AR '... completed'` が残り、server log もマスク応答を返した。UI (AI Tools) の手動確認とスクリーンキャプチャは未実施。
- 前提: docker compose で `sam2-agent` プロファイルを有効化し、`SAM2_TRACKER_*` / `SAM2_INTERACTOR_*` の Function ID を `.env` へ記録済み。GPU 共有や CPU fallback は `SAM2_*_DEVICE` を `cpu` にするほか、`docker-compose.yml` 側の `deploy.resources.reservations.devices` ブロックをコメントアウトして調整する。
- CLI / agent 操作は `uv tool install cvat-cli` で CLI を取得し、PAT (`CVAT_AGENT_TOKEN`) を `~/.config/cvat-cli` か `.env` に保存して使い回す。ローカルで repo ルートから実行する際は `UV_PROJECT_ENV=.venv` をセットして `uv run` を呼ぶ。

| Status | 手順 | コマンド / 操作 | ログ / 備考 |
| --- | --- | --- | --- |
| TODO | Function 登録 (tracker) | `UV_PROJECT_ENV=.venv uv run --with cvat-cli python -m cvat_cli --server-host http://localhost --auth <USER>:<PASS> function create-native "AI Tracker: SAM2" --function-file ai-models/tracker/sam2/func.py -p model_id=str:facebook/sam2.1-hiera-small -p device=str:cuda` | 返却 ID を `SAM2_TRACKER_FUNCTION_ID` へ追記。`--auth` の代わりに `--auth-token "$CVAT_AGENT_TOKEN"` でも可。 |
| TODO | Function 登録 (interactor) | `UV_PROJECT_ENV=.venv uv run --with cvat-cli python -m cvat_cli --server-host http://localhost --auth <USER>:<PASS> function create-native "AI Interactor: SAM2" --function-file ai-models/interactor/sam2/func.py -p model_id=str:facebook/sam2.1-hiera-small -p device=str:cuda` | ID を `SAM2_INTERACTOR_FUNCTION_ID` に記録。`ai-models/interactor/sam2/README.md` の入力制約 (正点2個以上 etc.) を CLI からも確認。 |
| BLOCKED (GPU 不足) | agent サービス起動 | `docker compose --profile sam2-agent build sam2-tracker-agent sam2-interactor-agent && docker compose --profile sam2-agent up -d sam2-tracker-agent sam2-interactor-agent` | 現在の開発端末には NVIDIA GPU が無く、`nvidia-container-cli: initialization error` で停止するため未実施。GPU ノード確保後に着手。 |
| PENDING | agent ログ確認 | `docker compose logs -f sam2-interactor-agent` | `SAM2_FUNCTION_ID` 未設定や PAT 不備時の失敗ログを収集予定。 |
| TODO | CLI -> agent -> REST 動作確認 | `UV_PROJECT_ENV=.venv uv run --with cvat-cli python -m cvat_cli --auth-token "$CVAT_AGENT_TOKEN" function run-agent $SAM2_INTERACTOR_FUNCTION_ID --function-file ai-models/interactor/sam2/func.py -p model_id=str:facebook/sam2.1-hiera-small` | SSE で `REQUEST_CATEGORY_INTERACTIVE` が最優先で割当されること、`mask_rle` が返送されることを Jenkins GPU ノードで確認予定。 |
| TODO | UI からの操作 | 任意ジョブを開き、AI Tools -> Interactors で `SAM2.1 Interactor (Native)` を選択。正/負点と box を入力して応答マスクが可視化されるか、Undo/Redo で履歴が残るかを画面録画。 | 録画 + CLI ログを次回更新時に本ログへ添付。 |

備考: ローカル (CPU) での compose 実行は GPU ブロックを無効化すれば起動までは可能だが、SAM2.1 モデルの初回ダウンロード&推論に 10GB 超の VRAM と長時間を要する。OSS 検証向けには GPU ノード上の `sam2-agent` プロファイルで上記手順を完了させ、SSE ログと UI キャプチャをこのテーブルに記載する。
