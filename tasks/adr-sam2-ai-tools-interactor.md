# ADR: OSSでSAM2.1をAI ToolsのInteractorとして提供する

- Status: Proposed
- Date: 2025-11-14
- Owner: CVAT OSS チーム
- Note: 本ADRは私的な検討用途としてまとめており、社内公式合意事項ではない。

## 背景 / コンテキスト
- 既存のSAM2対応は「AI Tracker: SAM2」として注釈アクションモーダルに統合されており、AI Toolsサイドバーからは利用できない。実装は `NativeFunctionTrackerAction`（`cvat-ui/src/components/annotation-page/annotations-actions/native-function-action.ts`）と関数API (`cvat/apps/functions/`) で完結している。
- AI ToolsのInteractor/Tracker一覧はNuclioベースの`/api/lambda/functions`の結果のみを読み込み、`core.lambda.call` を同期呼び出ししている (`cvat-ui/src/actions/models-actions.ts`, `cvat-ui/src/components/.../tools-control.tsx`)。OSS環境ではNuclioを同梱していないため、SAM2.1を同じUXで提供できない。
- ドキュメント上も「AI Toolsダイアログでは矩形に限定されるためSAM2 trackerは選択できない」と明記されており (site/content/en/docs/annotation/auto-annotation/segment-anything-2-tracker.md:286-289)、Enterprise/OnlineのSAM2 UXとギャップが残っている。
- 既にAIエージェント基盤 (SAM2 tracker向け) と `AnnotationRequest` キュー / SSE 監視が整備済みであり、これをInteractorにも拡張できればNuclio依存なしで同等UXを再現できる。

## ゴール
1. OSS版でもAI Toolsサイドバー > Interactorsタブに「SAM2.1 Interactor (AI Agent)」が表示され、Enterprise版SAM2と同じ操作手順でマスクを生成できる。
2. バックエンドは既存のネイティブ関数 (`/api/functions`) + AIエージェントの枠組みを再利用し、追加の外部サービス (Nuclio) に依存しない。
3. CLI / docker compose でトラッカーと同様に `function create-native` / `function run-agent` を実行するだけでSAM2.1インタラクタを常駐させられる。

## 決定事項
### 1. データモデル拡張
- `Function` モデルにInteractor専用メタデータを追加する：`min_pos_points`, `min_neg_points`, `startswith_box`, `startswith_box_optional`, `help_message`, `animated_gif`, `version`。これにより、Nuclio由来の`MLModel`シリアライザと同じ情報をUIへ返せる。
- `FunctionSerializer` で `kind == interactor` の場合に上記フィールドをrequireし、`labels_v2`は任意 (SAM2はクラスレス) のままとする。

### 2. API / サービス層
- 新規エンドポイント `POST /api/jobs/{job_id}/functions/{function_id}/interactions` を定義。
  - リクエスト: `frame`, `pos_points`, `neg_points`, `obj_bbox`, `label_id`, `start_with_box` 等、既存の`core.lambda.call`に倣ったペイロード。
  - サーバは `AnnotationRequest(category=interactive, type="interact")` を生成し、エージェントに処理させる。
  - サーバ側で最大60秒 (設定化) ポーリングし、`DONE`/`FAILED` でレスポンスを返す。結果には `mask`, `bounds`, `points` を含め、UIが従来通り`InteractorResults`として扱えるようにする。
  - タイムアウト時は `504` を返し、UIはエラートーストを表示。
- SSEウォッチや`/requests/acquire`は既にカテゴリを指定できるため、`request_category=interactive` を使って同キューを流用する。`apply_annotation_result` は `type="interact"` ではスキップする。

### 3. AIエージェント / CLI 拡張
- `cvat_sdk.auto_annotation` に `InteractorFunctionSpec` と `InteractorFunction` インターフェースを追加し、`pos/neg points` + オプションの `bounding box` を受けて `MaskPrediction` を返す仕様を定義。
- `cvat-cli function create-native` が `InteractorFunctionSpec` を読み取り `kind=interactor` を送信できるようにする。
- `run_agent` に `interact` リクエストの処理器を追加：
  - 指定フレームを `TaskDataset` から取得し、SAM2.1推論へ渡す。
  - 結果として `mask` (2D list) と `bounds`, `points` をJSON化して`complete` APIへ返す。
  - 処理時間短縮のため tracker と同じ `ProcessPoolExecutor` を再利用しつつ、`REQUEST_CATEGORY_INTERACTIVE` を常に最優先で消化する既存ロジックを活かす。
- `ai-models` に `interactor/sam2` (仮) を追加し、SAM2.1の promptable segmentation API (Meta公式 `SAM2ImagePredictor`/`SAM2PromptPredictor`) をwrapする。`pyproject.toml`/`uv.lock`・`func.py`・READMEをtracker版にならって用意。
- docker compose の `sam2-agent` サービスを二系統に分割 (例: `sam2-tracker-agent`, `sam2-interactor-agent`) し、それぞれに `SAM2_FUNCTION_ID_*` / `SAM2_MODEL_ID_*` を設定できるよう `.env` テンプレートを更新する。単一GPU環境では `deploy.resources` でデバイス共有するか、片方をCPU fallbackにする手順をドキュメント化。

### 4. UI / SDK 改修
- `core.functions.list` の応答を `SerializedModel` にマッピングするアダプタを追加し、`modelsActions.getModelsAsync` でNuclio + Native関数を統合して `models.interactors` / `models.detectors` / `models.trackers` を構築する。Providerを区別できるよう `MLModel.provider` を利用。
- AI Tools側 (`tools-control.tsx`) で `activeInteractor.provider === 'native'` の場合は新API (`jobInstance.runNativeInteractor(..)` など `core.functions` ラッパー) を呼び出す実装を追加し、従来の `core.lambda.call` をフォールバックとして保持。
- `Canvas.interact` から送られるパラメータはこれまでと同一なので、UI/UX変更は最小限 (ローディング表示やエラー文言のみ差分) に留める。
- `cvat-core` に `Job.runFunctionInteractor` を追加し、server-proxy経由で前述の新RESTを叩けるようにする。

### 5. ドキュメント / サンプル
- `site/content/en/docs/annotation/tools/ai-tools.md` と `segment-anything-2-tracker.md` に「SAM2.1 Interactor (AI Agent) は OSS でもAI Toolsから利用可能になった」旨と `.env` 設定例を追記。
- README / 開発者ガイドに `sam2-interactor-agent` プロファイルの起動コマンド、PAT流用手順、GPU要件を記載。

## 代替案 (却下)
1. **NuclioベースのSAM2を同梱する**: 既存Enterprise資産を流用できるが、OSSバンドルにNuclioやRedisを含める運用コストが大きく、軽量なdocker compose目標に反するため採用しない。
2. **ブラウザ内推論 (WebGPU/WebAssembly)**: 追加サーバレス成分無しで済むが、SAM2.1はVRAM 8GB以上を前提としており現実的ではない。
3. **UIを大きく書き換えて非同期Polling UIにする**: OAuth/tokenまわりの差分が減る一方で、既存Enterprise UXとズレるため採用しない。サーバ側で同期化する方針を選んだ。

## リスクと対応
- **サーバ同期待ちによるワーカー消費**: 60秒ブロックはGunicorn workerを占有するため、`timeout`と`MAX_CONCURRENT_INTERACTION_WAIT`を設定し、一定以上の待機は即時`429`を返してUIにリトライさせる。
- **結果ペイロード肥大化**: `mask` (H×W) をJSONで返すと帯域を圧迫する。既存アクション同様にRLE圧縮し、`bounds`を付けて伝送量を抑える。
- **マルチ関数エージェントの運用**: tracker/interactorで別Function IDになるため、`sam2-agent`イメージを共通化しつつ、複数エージェントが同一ホストGPUを取り合わないよう compose プロファイルとドキュメントで明示する。

## 未解決事項
- SAM2.1の最適パラメータ (point weighting, mask threshold) は今後の検証が必要。初期版は Meta 推奨値を設定し、フィードバックを待つ。
- 1 Jobにつき複数同時インタラクションを受け付けるか否か (現状は直列)。パフォーマンス次第でWebSocket通知やサーバpushの導入を検討。

## 作業洗い出し (2025-11-14 私用メモ)
### バックエンド / API
- `cvat/apps/functions/models.py` にInteractor向けカラム（`min_pos_points`, `min_neg_points`, `startswith_box`, `startswith_box_optional`, `help_message`, `animated_gif`, `version`）を追加するタスク。マイグレーション生成と既存データのデフォルト値設定、`FunctionSerializer` での必須チェック/シリアライズ拡張が必要。
- 新REST `POST /api/jobs/{job_id}/functions/{function_id}/interactions` を `cvat/apps/functions/views.py`・`services.py` に実装し、`AnnotationRequest(category=interactive, type='interact')` の生成/監視を行う。`JobPermission` やジョブ範囲検証、`MAX_CONCURRENT_INTERACT_WAIT`・`CVAT_FUNCTION_INTERACT_TIMEOUT` 設定の扱いを `settings/base.py` で整理。
- `result_handlers.py` にInteractor用 `mask_rle` 展開/適用処理を追加し、`apply_annotation_result` から `type='interact'` を除外 or no-opにする明示的なハンドリングを加える。`views.FunctionQueueWatchView` のSSE通知にも `request_category=interactive` を含める。
- `cvat/apps/functions/tests/test_api.py` へinteractiveカテゴリのユニットテストを追加（正常系 / タイムアウト504 / 同時実行制限429 / 権限検証）。既存テストデータ作成ヘルパーに `kind=FunctionKind.INTERACTOR` 分岐を増やす。

### AIエージェント / CLI / SDK
- `cvat_sdk/auto_annotation` に `InteractorFunctionSpec`・`InteractorFunction` を追加し、`pos_points`/`neg_points`/`obj_bbox` 入力と `mask_rle` 出力の型定義を整備。`mypy` 対応と既存tracker用テストの拡張が必要。
- `cvat-cli/src/cvat_cli/_internal/commands_functions.py`・`agent.py` に `kind=interactor` 取扱いと `_worker_job_interact` 実装を追加。既存trackerコードの共通ヘルパー抽出、`tests/python/cli/test_cli_misc.py` へのケース増設が必要。
- `ai-models/interactor/sam2/` ディレクトリ（`pyproject.toml`/`uv.lock`, `func.py`, README）を追加し、SAM2.1推論ラッパーと `TaskDataset` ローダーの共用化を図る。GPU1枚構成でtrackerとinteractorをどう並列化するか（ProcessPool共有 or サービス分割）をcompose設計に落とし込む。
- `docker-compose.yml`, `docker-compose.dev.yml`, `.env.example` に `sam2-interactor-agent` サービス・環境変数 (`SAM2_INTERACTOR_FUNCTION_ID`, `SAM2_INTERACTOR_MODEL_ID`, GPU割り当て) を追記し、`Dockerfile.sam2-agent` を共通利用するか新Dockerfileを分けるか決める。

### UI / cvat-core
- `cvat-core/src/server-proxy.ts`・`cvat-core/src/api.ts` に `runJobFunctionInteractor` (ネイティブREST呼び出し) を追加し、`cvat-core/src/ml-model.ts` で `provider` やInteractorメタ（min/max points, startsWithBoxなど）を保持できるよう型を拡張。
- `cvat-ui/src/actions/models-actions.ts` と reducer へ `core.functions.list` を組み込むアダプタを実装し、`ModelProviders` に `native` を追加。`tools-control.tsx` の `runInteractionRequest` では `activeInteractor.provider === 'native'` 時に `jobInstance.runNativeInteractor` を呼ぶよう分岐を加える。
- `cvat-ui/src/cvat-core-wrapper.ts` へ `Job.runNativeInteractor` を追加し、`core.functions.jobs.interact` RESTを叩く。`mask_rle` の `core.utils.rle2Mask` 復号やエラートースト表示を統合する。
- `NativeFunctionTrackerAction` との共通UI部品（ローディング、エラーハンドリング）を見直し、`yarn test` 対象に provider 分岐の単体テスト (例: `tests/components/tools-control.spec.tsx`) を追加。

### テスト / 運用検証
- `pytest ./tests/python/cli/test_cli_misc.py -k native_interactor` を追加して CLI → agent → API の往復を検証。`tests/python` で SAM2 ダミー推論をモックするfixtureが必要。
- docker compose (sam2-tracker-agent + sam2-interactor-agent) を用いた私的E2E検証手順を整備し、AI ToolsからSAM2.1マスク生成→アノテーション保存→Undo/Redoの動作を確認。公開ドキュメントは不要だが操作ログを個人メモに残す。

> FAQ整備・外部向けドキュメント/リリースノート更新は私用スコープ外として除外。

### TODOチェックリスト (私用)
- [x] Functionモデル/Serializer拡張と`makemigrations`実施（Interactorメタ＋`mask_rle`受信定義）
- [x] `/api/jobs/{job_id}/functions/{function_id}/interactions` REST ＋ timeout/同時実行ガード設定
- [x] `result_handlers.py`・SSE通知のinteractor対応、および`test_api.py` でinteractiveテスト追加
- [x] SDK/CLI (`InteractorFunctionSpec`, `_worker_job_interact`) 実装と `tests/python/cli` 拡張
- [x] `ai-models/interactor/sam2` 作成＆compose/env (`sam2-interactor-agent`) 更新
- [x] `cvat-core` にネイティブInteractor APIを追加し、`models-actions`/`tools-control` でprovider分岐
- [x] `Job.runNativeInteractor` / `mask_rle` 復号処理
- [x] UI単体テスト整備（provider分岐 + mask_rle 復号のケース追加、`cvat-ui/tests/tools-control.spec.ts` のVitestで検証）
- [x] CLI→agent→REST→UIのE2E検証（docker compose + 手動操作ログ。テンプレ: `tasks/sam2_interactor_e2e.md`）

## 作業計画 (概計画)
1. **フェーズ0: 要件確定とスコープロック**
   - ADRのステータスを`Proposed`→`Accepted`へ進めるためのレビュー収集、SAM2.1モデル仕様とOSSリリース方針の最終確認。
   - データ移行の影響範囲 (migrations / serializer互換性) と compose テンプレート変更のステークホルダー合意を得る。
2. **フェーズ1: バックエンド基盤拡張**
   - `Function` モデルとシリアライザの拡張、`/api/jobs/.../interactions` REST、AnnotationRequest待受処理を実装し、単体テストとマイグレーション検証を完了させる。
   - ガードレール (timeout, 同時実行制限, RLEシリアライズ) を設定ファイルとモニタリング観点まで含めて定義する。
3. **フェーズ2: AIエージェント / CLI / SDK整備**
   - `InteractorFunctionSpec` をSDK/CLIへ追加し、`run-agent` の `interact` リクエスト処理と `ai-models/interactor/sam2` の実装・サンプルデータを提供。
   - docker compose, `.env` テンプレート、CIジョブ (lint/test) を更新し、trackerとの共存シナリオを検証する。
4. **フェーズ3: UI / cvat-core 統合**
   - モデル一覧アダプタとAI Toolsサイドバーの provider 分岐を追加し、`Job.runFunctionInteractor` 経由で同期呼び出しできることを確認。
   - フロント単体テストと手動E2E (SAM2.1マスク生成) を実施し、OSS/Enterprise双方で既存機能が退行しないことをチェックする。
5. **フェーズ4: ドキュメント / リリース対応**
   - ユーザドキュメント、`tasks/sam2_oss_summary.md`, README, compose プロファイル解説を更新し、デプロイ手順・GPU要件・CLIコマンド例を整備。
   - 変更点をCHANGELOGに追加し、SAM2.1 Interactor提供をアナウンスするためのブログ/リリースノート下書きを準備する。

## 作業計画 (詳細)
### フェーズ0: 要件確定とスコープロック
- **Entry Criteria**: SAM2.1 OSS対応の課題がレビュー依頼済みで、UI/Backend/Docsの窓口が決まっている。  
- **Exit Criteria**: ADRが `Accepted` へ移行できるレビュー承認と、各担当のリソース割り当てが明文化される。  
- **主要タスク**:
  1. 本ADRを用いたアーキレビューを開催し、質疑はFAQとして `site/content/en/docs/contributing/` 配下に転記する計画を合意。
  2. SAM2.1モデルのチェックポイント/ライセンスを再確認し、`ai-models` への同梱可否を法務へエスカレーション。
  3. Migration・compose変更の互換ポリシーと切り戻し手順を `tasks/sam2_oss_summary.md` ドラフトに記載。
  4. Backend/UI/SDK/Docs担当を割り当て、フェーズ1以降の完了条件へ同意した証跡をSlack等に残す。

### フェーズ1: バックエンド基盤拡張
- **Entry Criteria**: 既存の `cvat/apps/functions` 実装 (Function CRUD / ARキュー) がdevelopブランチと同期しており、Interactor向け差分の設計がレビュー合意済み。  
- **Exit Criteria**: `Function` モデル/シリアライザ/サービスへInteractorメタが追加され、`/api/jobs/{job_id}/functions/{function_id}/interactions` エンドポイントがテスト付きで実装される。CIでは従来テストに加え新しいinteractorケースが走る。  
- **主要タスク**:
  1. `cvat/apps/functions/models.py` にInteractor専用のメタ列 (points要件/startsWithBox等) を追加し、`makemigrations`→`migrate` で互換性を確認 (既存データにデフォルト値が入るようにする)。
  2. 既存 `serializers.py` に `kind=interactor` バリデーションとレスポンスフィールドを追加し、`views.py`/`services.py` と整合させる。
  3. `services.py`/`views.py` に `/api/jobs/{job_id}/functions/{function_id}/interactions` ハンドラを追加し、現在のAnnotationRequest処理を流用しつつ同期レスポンスを返すロジックを実装する。`settings/base.py` へ `CVAT_FUNCTION_INTERACT_TIMEOUT` / `MAX_CONCURRENT_INTERACT_WAIT` を追加。
  4. `result_handlers.py` にInteractor用のRLE圧縮ユーティリティを追加し、既存Tracker処理と共通化 (重複ロジックはリファクタリング)。  
  5. 既存 `cvat/apps/functions/tests/test_api.py` に interactor 正常系/タイムアウト(504)/同時実行上限(429) のテストを追加し、`.github/workflows/main.yml` で対象テストが必ず走るよう更新。

### フェーズ2: AIエージェント / CLI / SDK整備
- **Entry Criteria**: 既存のSAM2トラッカー向け CLI/agent 実装 (現ブランチに存在) が把握できており、Interactor追加に伴う差分設計とAPI仕様が確定している。  
- **Exit Criteria**: `cvat-cli` / `cvat-sdk` / `ai-models` / `docker-compose` が Interactor 対応を取り込み、Tracker 実装と共存する状態で `tests/python/cli` が通る。  
- **主要タスク**:
  1. 既存 `cvat_sdk/auto_annotation` (SAM2トラッカー対応の延長) に `InteractorFunctionSpec` / `InteractorFunction` を追加し、mypy/lint と単体テストを更新。
  2. `cvat-cli/src/cvat_cli/_internal/commands_functions.py` と `agent.py` を拡張し、既存の tracker コードパスを再利用しつつ `kind=interactor` と `_worker_job_interact` を実装。`tests/python/cli/test_cli_misc.py` に Native Interactor フローのケースを追加。
  3. 既存の `Dockerfile.sam2-agent` / `dev/sam2-agent/entrypoint.sh` を基に、`ai-models/interactor/sam2` ディレクトリを新設 or Tracker との共通モジュールとして実装し、requirements/README/サンプルを用意。
  4. `docker-compose.yml`, `docker-compose.dev.yml`, `.env.example` に `sam2-interactor-agent` サービスを追加し、既存の `sam2-agent` (tracker) とリソース競合しないようコメント・env説明を追記。
  5. CLI/Agent変更をCIに組み込み、`pip install -e cvat-cli` → `pytest tests/python/cli/test_cli_misc.py -k native_interactor` が実行されるよう `.github/workflows/main.yml` を更新。

### フェーズ3: UI / cvat-core統合
- **Entry Criteria**: Tracker対応で進行中の `cvat-core` / `cvat-ui` 変更との差分が確認でき、Interactor向け仕様がサーバ・CLIと整合している。  
- **Exit Criteria**: `provider === 'native'` のInteractorがAI Toolsサイドバーで表示・実行でき、既存Tracker/Detector UIと競合しない状態で `yarn test` / `yarn lint` が通る。  
- **主要タスク**:
  1. Tracker対応で更新済みの `cvat-core/src/server-proxy.ts`, `api.ts`, `server-response-types.ts` を参照し、`runJobFunctionInteractor` や `Job.runNativeInteractor()` を追加。既存Native Tracker呼び出しとコードパスを共通化する。
  2. `cvat-ui/src/actions/models-actions.ts` と `modelsReducer` を拡張し、`core.functions.list` (native) と `core.lambda.list` (nuclio) を統合するロジックを既存分岐と整合させる。`ModelProviders` に `native` を追加し、UI表示に反映。
  3. `cvat-ui/src/components/annotation-page/annotations-actions/annotations-actions-modal.tsx` や `tools-control.tsx` で provider 分岐を追加し、Native Interactor選択時に `jobInstance.runNativeInteractor()` を利用。既存 `NativeFunctionTrackerAction` から共通化できるコードを抽出。
  4. `cvat-ui/src/cvat-core-wrapper.ts` などで `mask_rle` 復元、ローディング、エラートースト処理を実装し、`yarn test tools-control.spec.tsx` 等でカバレッジを確保。
  5. docker compose (SAM2 tracker + interactor agent) を使った手動E2Eを実施し、OSS UIでの操作ログ/スクリーンショットをDocs用に取得。

### フェーズ4: ドキュメント / リリース対応
- **Entry Criteria**: 実装が `develop` へマージ予定で、UIキャプチャとCLIログが揃っている。  
- **Exit Criteria**: Docs/README/CHANGELOG/ブログ草案が更新され、`tasks/sam2_oss_summary.md` に最終手順がまとまる。  
- **主要タスク**:
  1. `site/content/en/docs/annotation/tools/ai-tools.md`・`segment-anything-2-tracker.md` にAI Tools Interactor手順と `.env` サンプルを追記。
  2. `site/content/en/docs/api_sdk/cli/_index.md`・`site/content/en/docs/contributing/development-environment.md` にCLI/agent手順を更新。
  3. `README.md`, `tasks/sam2_oss_summary.md`, `CHANGELOG.md` を更新し、リリースノート/ブログ草案を `changelog.d/` へ追加。
  4. Support FAQ・Issue Templateに既知制約 (GPU 8GB+, 60s sync, fallback) を記載。
  5. 公開計画 (日時/チャネル) を決定し、SNS or ブログ担当へ引き継ぎ。

## 次のアクション (ハイレベル)
1. モデル/シリアライザ/REST追加 + migration を実装。
2. `cvat-cli` + `ai-models` にInteractor specを追加し、単体テストを整備。
3. `cvat-core` / `cvat-ui` にネイティブInteractorパスを実装し、E2Eハーネス (手動) でSAM2.1との往復を確認。
4. ドキュメントとdocker composeプロファイルを更新し、動作手順を `tasks/sam2_oss_summary.md` に追記。

## 調査メモ (2025-11-14)
- **データモデル**: `Function` にインタラクタ用メタ (min/max points, startswith_box, help_message, animated_gif, version) を追加し、`FunctionSerializer` が kind=interactor で必須チェックを行う。RLE 受信のため結果ペイロードへ `mask_rle` を定義する。
- **ジョブREST**: `POST /api/jobs/{job_id}/functions/{function_id}/interactions` を追加し、`AnnotationRequest(category=interactive,type='interact')` を発行して最大60秒同期待ち。Gunicorn占有防止に `CVAT_FUNCTION_INTERACT_TIMEOUT` と `MAX_CONCURRENT_INTERACT_WAIT` を設定で制御し、タイムアウトは504/上限は429で応答。
- **SDK/CLI/Agent**: `InteractorFunctionSpec`/`InteractorFunction` を `cvat_sdk` に追加し、`cvat-cli function create-native` が kind=interactor＋メタ値を送信、`run-agent` は `_worker_job_interact` で `REQUEST_CATEGORY_INTERACTIVE` を最優先処理。`ai-models/interactor/sam2` を新設し tracker 実装から共通化、`docker-compose.yml` に `sam2-interactor-agent` サービス (独立 Function ID) を追加して GPU 共有手順を文書化。
- **UI/cvat-core**: `core.lambda.list` と `core.functions.list` を統合するアダプタを `modelsActions.getModelsAsync` に追加し、`ModelProviders` に `native` を導入。`Job.runFunctionInteractor` と `serverProxy.jobs.runInteractorAction` を実装し、AI Tools 側 (`tools-control.tsx`) は `provider === native` 時に新RESTを呼ぶ。レスポンス `mask_rle` は `core.utils.rle2Mask` で復元。
- **テスト/ドキュメント**: `cvat/apps/functions/tests` に interactor API の happy/timeout/429 を追加し、`cvat-ui` では `tools-control` 単体テストで provider 分岐と RLE 復元を検証。README や `site/content/en/docs/annotation/tools/ai-tools.md`、`segment-anything-2-tracker.md` へ OSS でも AI Tools から SAM2.1 が使える旨と 2 エージェント構成 (.env サンプル) を追記。
