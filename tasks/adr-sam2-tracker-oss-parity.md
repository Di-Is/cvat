# ADR: SAM2 Tracker OSS 対応指針

- Status: Proposed
- Date: 2025-11-17
- Owner: OSS SAM2 チーム

## 背景

- Enterprise 版では SAM2 Tracker の Nuclio 実装と AI Agent 実装が提供されており、ポリゴン・マスク入力や複数オブジェクト同時実行といった UX が確立している（`site/content/en/docs/annotation/auto-annotation/segment-anything-2-tracker.md:211-269`）。
- OSS 版では `sam2-tracker-agent`（AI Agent）を compose で提供しているが、UI/CLI/API/ドキュメントの一部が Enterprise 仕様と乖離しており、Polygon を基点にした追跡や一括実行の保証が弱い。
- README / docs / compose / CLI の更新が部分的に進んでいるものの、どこまで揃えれば Enterprise と同等と言えるのか整理されていない。

## 方針

1. **機能互換の確保**
   - UI（Run Actions モーダル、AI Tools サイドバー）と backend（annotation actions API）がポリゴン／マスクをフルサポートし、`Convert polygon shapes to tracks` → Tracker 実行まで 1 リクエストで完結することを保証する。
   - `ai-models/tracker/sam2/func.py` は Nuclio 実装と同じ入力契約（shape_type、polygon 座標、複数オブジェクト）を受け取り、RLE/mask を同等に返却する。
   - Multi-object 実行（Ctrl+E、Run Actions モーダル）を OSS エージェントでもサポートし、UI 側で shape type フィルタやターゲットフレーム指定が Enterprise と一致するようにする。

2. **ドキュメント整備**
   - `README.md` と `site/content/en/docs/annotation/auto-annotation/segment-anything-2-tracker.md` を同期し、OSS 手順でも「ポリゴン／マスク入力」「複数オブジェクト」「Convert polygon shapes to tracks」オプションを明記する。
   - `site/content/en/docs/annotation/tools/ai-tools.md` の SAM2 Tracker セクションに AI Agent (OSS) が Enterprise と同じ入力パターンを持つことを追記し、`.env` サンプルや compose profile の説明を全ドキュメントで共通化する。

3. **Compose / CLI 標準化**
   - `docker-compose.yml` の `sam2-tracker-agent` へ Enterprise 同等の環境変数を揃え、`SAM2_TRACKER_*`（モデル/デバイス/関数/追加引数）を個別に制御できるよう整理する。
   - `cvat-cli function run-agent` が Polygon/Multi-object を受け入れる実装になっていることを確認し、必要であれば `--extra-args` や `SAM2_TRACKER_FUNCTION_FILE` などの引数を compose から渡せるようにする。
   - `ai-models/tracker/sam2/pyproject.toml` と `dev/sam2-agent/entrypoint.sh` を OSS 環境でも `uv sync --frozen` で再現できるように固め、`cvat-cli`/`cvat-sdk` 依存をバージョン管理する。

4. **テレメトリとテスト**
   - `cvat/apps/functions/telemetry.py` に SAM2 Tracker の入力形状タイプやオブジェクト数を追加し、OSS 環境でも Polygon/Multi-object の利用率を観測できるようにする。
   - `tests/python/cli/test_cli_misc.py` などに `function run-agent` の Polygon/Multi-object 経路をモックテストで追加し、入力検証や CLI 引数の整合性を担保する。
   - 可能であれば REST/E2E テストを追加し、ポリゴンから Tracker 実行→トラック生成までを自動で回帰確認する。

## 成功指標

- OSS 版 UI からポリゴン／マスクを選んで SAM2 Tracker（AI Agent）を実行した場合、Enterprise と同じ UX で完走する（単体 + 複数オブジェクト両対応）。
- README / docs / ADR が Enterprise 記述と矛盾せず、`.env`/compose/CLI の例が共通化されている。
- `docker compose --profile sam2-agent up` だけでエージェントが起動し、`function run-agent` が Polygon/Multi-object を扱える。
- CI で Polygon/Multi-object 経路を含む CLI/REST テストが走り、テレメトリにも入力形状情報が記録される。

## TODO

- [ ] **UI parity** — `cvat-ui/src/components/annotation-page/standard-workspace/controls-side-bar/tools-control.tsx` で `supportedShapeTypes` を活用し、矩形限定ロジック（`getSupportedTrackers`/`collectTrackerPortals`/`trackedRectangleMapper`/`canvasInstance.interact({ shapeType: 'rectangle' })`）を polygon/mask 対応に置き換える。
  - [x] `getSupportedTrackers`/トグル UI を `supportedShapeTypes` ベースに刷新し、矩形以外の shape でも追跡トグルと説明が表示されるよう更新。
  - [x] Track ボタンの rectangle 依存を解消し、polygon/mask を直接ドラフトできるキャンバス操作へ置き換える。
- [x] **Shared tracker logic** — Run Actions (`annotations-actions/native-function-action.ts`) とサイドバー間で tracker 入力フィルタ・ターゲットフレーム制御を共通 util 化し、`core.lambda.call` への payload も統一する。
- [x] **Telemetry & tests** — `cvat/apps/functions/tracking.py` 処理で `telemetry.traced` に shape 種別やオブジェクト数を記録し、`tests/python/cli/test_cli_misc.py` などに Polygon/Multi-object 経路の単体テストを追加。UI 側も `cvat-ui/tests/tools-control.spec.ts` で tracker 表示/挙動を検証する。
- [ ] **Compose/CLI hardening** — `docker-compose.yml` と `cvat-cli/src/cvat_cli/_internal/agent.py` の SAM2 tracker パスを再確認し、`SAM2_TRACKER_*` 変数の説明・CPU fallback 手順・`function run-agent` の shape validation をドキュメント化。必要なら追加ユニットテストを作成。
- [ ] **Docs alignment** — `README.md` と `site/content/en/docs/annotation/{auto-annotation/segment-anything-2-tracker,tools/ai-tools}.md` を更新し、OSS SAM2 Tracker がポリゴン/マスク/複数オブジェクトを扱えること、`.env`/compose 手順が Enterprise と共通であることを明記する。
