# ADR: SAM2 Tracker を Polygon / Mask で直接呼び出す

- Status: Proposed
- Date: 2025-11-16
- Owner: CVAT OSS チーム

## 背景
- 現行の Run Annotation Action では `supported_shape_types` を利用しつつも、`NativeFunctionTrackerAction` の `isApplicableForObject` が `ObjectType.TRACK` のみを許容しているため、Polygon/Maks shape を選択した状態で `Ctrl+E` を開いても SAM2 Tracker は候補に現れない。
- Enterprise 版 SAM2 Tracker では「Convert polygon shapes to tracks」等を意識せず、Polygon/Maks を入力にしてそのままマルチオブジェクト追跡が行える。OSS 版の UI/CLI/ドキュメントも同じ仕様を謳っているが、実装が追いついていない。
- 既に `Function.supported_shape_types` には `["mask", "polygon"]` を設定済みであり、サーバー側 `start_tracking_action` でも Polygon/Maks の検証・マスク生成までは行っている (`cvat/apps/functions/tracking.py:108-146`)。不足しているのは「shape を選択しても Run Annotation Action へ SAM2 を提示し、実行時にトラックへ変換する」UI 側の導線である。

## ゴール
1. Polygon/Maks shape を選択した状態で Run Annotation Action モーダルを開いても `AI Tracker: SAM2` が候補に表示される。
2. SAM2 Tracker 実行時に shape を自動的にトラックへ変換し、ユーザーが `Convert polygon shapes to tracks` を手動で実行する必要がなくなる。
3. ドキュメント / エンドツーエンド手順で「Polygon/Maks を直接トラッキングに渡せる」ことを明示し、Enterprise 版と同じ UX を保証する。

## 決定事項
### 1. Run Annotation Action の適用対象を拡張
- `NativeFunctionTrackerAction.isApplicableForObject` を更新し、`objectType === ObjectType.SHAPE` かつ `shapeType` が `supportedShapeTypes` に含まれる場合も `true` を返す。
- `applyFilter` で `collection.shapes` から Polygon/Maks を抽出し、実行時に新規トラックへ変換する `ShapePayload` を組み立てる。既存トラックと shape を同時に選択した場合でも、同一の `supportedShapeTypes` フィルタで扱えるよう共通化する。
- モーダルから `shape` を選んだ場合も `actions.filter(...isApplicableForObject...)` で SAM2 を残せるため、ユーザーはオブジェクト単位で `Ctrl+E` を開ける。

### 2. 変換モードをユーザーに明示し選択可能にする
- Online/Enterprise 版の「Convert shapes to tracks」トグルを OSS の Run Annotation Action にも追加し、デフォルトは OFF（＝shape のまま SAM2 へ渡す）とする。ユーザーが ON に切り替えた場合のみトラック化フローを実行する。
- クライアント側の処理:
  1. トグル OFF: Polygon/Maks shape の `points` をそのまま Tracker へ送る。サーバー側の `shape_payloads` 経由で一時トラックを生成し、戻り値のトラックを既存ワークスペースへ適用する。
  2. トグル ON: 既存の `core.annotations.convertShapesToTracks(shapeIds)` を利用してローカルでトラックIDを作成 → `trackIds` として SAM2 を呼び出す。処理後はトラックを残し、Undo/Redo で戻せるようにする。
- ユーザーが毎回トグルを再設定しなくて済むよう、設定値を `localStorage` に保持し、Run Annotation Action モーダル初期化時に適用する。
- トグルの状態は `start_tracking_action` の引数（例: `convert_shapes`) に含め、サーバーがどちらのモードで実行されたかを telemetry に記録できるようにする。

### 3. サーバー側の入力緩和
- `start_tracking_action` に `shapes` フィールドを追加し、`track_ids` と `shapes` の両方を受け取れるようにする。
  - トグル OFF の経路では `shapes` に Polygon/Maks を渡す。サーバーは `AnnotationRequest.parameters["shapes"]` を利用して一時トラックを作成し、SAM2 推論後に最終結果だけを保存する。
  - トグル ON の経路では従来通り `track_ids` のみを渡し、追加の変換を行わない。
- 一時トラックは `source=FunctionKind.TRACKER` などのメタデータで区別し、成功後に自動削除（もしくは `auto_created=True` フラグで UI が非表示にする）を検討する。

### 4. ドキュメントと Compose 例の更新
- `README.md` / `site/content/en/docs/annotation/auto-annotation/segment-anything-2-tracker.md` に「Polygon/Maks を選択した状態で Run Annotation Action を開くと SAM2 Tracker が表示され、トラック化は自動で行われる」旨を追記。
- CLI ハンドブック (`site/content/en/docs/api_sdk/cli/`) にも `function run-agent` の `supported_shape_types` をPolygon/Maksに設定する例と、Polygon/Maks shape を Batch 実行で渡した場合の挙動を加筆する。

## 詳細設計
### 全体シーケンス
1. ユーザーが Polygon/Mask shape もしくは既存トラックを選択して `Ctrl+E` を押すと、`NativeFunctionTrackerAction.applyFilter` が現在フレーム上の対象オブジェクトを抽出し、`collection` に `tracks` と `shapes` を同時に含めてモーダルへ渡す。
2. モーダルは選択済みの `ObjectState` から `TrackerSubject` を生成し、変換モード（inline / preconvert）とともに `NativeFunctionTrackerAction.run()` へ引き渡す。`TrackerSubject` には serverId / clientId / labelId / shapeType 等が保存される。
3. `run()` はターゲットフレーム検証後、`conversionMode` ごとに前処理を実行する。inline の場合は `instance.annotations.save()` で shape の serverId を確定し、preconvert の場合は `core.annotations.convertShapesToTracks()` を同期実行して shape を即トラック化する。
4. 前処理で得られた `trackIds` と `TrackerRunShapePayload[]` を `Job.runFunctionTrackerAction()` へ渡し、`conversionMode` も一緒に送る。payload の例:
   ```jsonc
   {
     "frame": 12,
     "target_frame": 120,
     "track_ids": [3, 5],
     "conversion_mode": "inline",
     "shapes": [
       {
         "id": 481,
         "client_id": 9001,
         "frame": 12,
         "label_id": 7,
         "shape_type": "polygon",
         "points": [100, 150, 220, 140, 210, 220],
         "z_order": 1,
         "rotation": 0,
         "group": null,
         "occluded": false,
         "outside": false,
         "source": "AUTO",
         "attributes": [{ "spec_id": 4, "value": "sedan" }]
       }
     ]
   }
   ```
5. サーバーの `start_tracking_action()` は `track_ids` と `shapes` を `TrackingSubject` dataclass に正規化してから `AnnotationRequest` を生成する。shape から来た subject には `kind='shape'`、`original_shape_id` や `initializer` が付与される。
6. SAM2 エージェントが `track` リクエスト列を完了すると `_apply_tracking_results()` が走り、`kind='shape'` の subject には新しい `LabeledTrack` を作成、`original_shape_id` を削除して change payload を通知する。これにより UI はリロード不要で差分を取り込める。

- `NativeFunctionTrackerAction.applyFilter` は、既存トラックに加えて Polygon / Mask shape を返せるよう `collection.shapes` をフィルタリングする。shape 側は「現在フレーム（`frameData.number`）と一致」「`supportedShapeTypes` を満たす」「`objectType === ObjectType.SHAPE`」の3条件を満たす必要があり、返却オブジェクトは `tracks`/`shapes` の双方を含む。選択ペインでは `kind` を付与してレンダリングを切り替える。
- `ObjectState` から `TrackerSubject` を構築するユーティリティ（`utils/tracker.ts`）を追加し、`NativeFunctionTrackerAction` は必ずこの構造体を経由して処理する。型は以下の通り：
  ```ts
  interface TrackerSubject {
      kind: 'track' | 'shape';
      serverId: number | null;
      clientId: number;
      frame: number;
      labelId: number;
      shapeType: ShapeType;
      points: number[];
      zOrder: number;
      rotation: number;
      group: number | null;
      occluded: boolean;
      outside: boolean;
      source: Source;
      attributes: ObjectStateAttribute[];
  }
  ```
  `run()` では `Set<number>` を使って trackId の重複を省き、shape からは `TrackerRunShapePayload` へ変換する。
- Run Annotation Action モーダルに `Convert shapes to tracks` スイッチを追加する。状態は `localStorage` の `cvat:tracker:sam2:convert_shapes_to_tracks` キー（`"inline"` / `"preconvert"`）に保存し、モーダル初期化時に読み出して `NativeFunctionTrackerAction` の内部フィールド `#conversionMode` と同期する。ユーザーが切り替えた瞬間に localStorage へ書き戻し、以降のセッションにも適用できるようにする。
- スイッチ別の処理フロー:
  | モード | 事前処理 | API へ渡す payload | Undo/Redo | 想定用途 |
  | --- | --- | --- | --- | --- |
  | inline (デフォルト) | `instance.annotations.save()` を実行して shape の `serverID` を確定 | `trackIds`（既存トラック分）+ `shapes[]`（Polygon/Mask 分）+ `conversionMode='inline'` | DB 反映まで Undo 不可。完了時にサーバーが shape 削除と track 作成をまとめて通知 | Polygon/Mask を迅速に追跡開始 |
  | preconvert | `core.annotations.convertShapesToTracks(shapeIds)` を呼んで shape を即時トラック化 → `trackIds` へ追加 | `trackIds` のみ + `conversionMode='preconvert'`（`shapes` は空） | 変換がクライアントで行われるため従来通り Undo/Redo 即時可 | 大量の shape をまとめてトラック化 |
- `NativeFunctionTrackerAction.run()` は inline モード時に `instance.annotations.save()` を await し、`serverId` がない shape subject を自動保存したのち `TrackerRunShapePayload` を構築する。preconvert モードでは `convertShapesToTracks()` のレスポンスから作成された trackId を既存 trackId set にマージしてから API を呼ぶ。何も送る対象がない場合は「Polygon/Mask もしくは Track を 1 つ以上選択してから実行してください」と警告する。
- `TrackerRunShapePayload` は以下の TS 型で `Job.runFunctionTrackerAction` に渡す：
  ```ts
  interface TrackerRunShapePayload {
      id: number | null; // serverID（auto-save 後に確定）
      clientId: number;
      frame: number;
      labelId: number;
      shapeType: ShapeType;
      points: number[];
      zOrder: number;
      rotation: number;
      group: number | null;
      occluded: boolean;
      outside: boolean;
      source: Source;
      attributes: { specId: number; value: AttributeValue; }[];
  }
  ```
- `annotations-actions-modal.tsx` では NativeFunctionAction から追加 UI を注入できる拡張ポイントを導入し、SAM2 用スイッチの state を React Hook Form で制御する。`ActionFormContext` に conversionMode を登録し、`onSubmit` で `NativeFunctionTrackerAction` の `setConversionMode()` を呼ぶ。shape が 50 件を超える場合は `ActionFooter` に警告トーストを表示し、preconvert モードへ誘導する。
- SAM2 実行後、`handle_annotations_change` が shape 削除・track 追加を放送するため、UI 側は `clientId` 一覧を保持して選択中オブジェクトを解除し、重複表示を避ける。`annotationsActionsModal` が閉じる際に `TrackerSubject` の clientId をクリアする処理も追加する。

### SDK / API 層
- `FunctionTrackerRunParams` を `trackIds?: number[]`, `shapes?: TrackerRunShapePayload[]`, `conversionMode: 'inline' | 'preconvert'` に拡張し、`type TrackerRunPayload = ({ trackIds: number[] } | { shapes: TrackerRunShapePayload[] }) & { conversionMode: ... };` のような union 型を導入して静的に「track か shape のどちらか一方は必須」を表現する。`plugin-api.d.ts` や `cvat-core-wrapper` の型生成も更新し、UI から最新定義を import できるようにする。
- `cvat-core/src/server-proxy.ts` は snake_case の `conversion_mode` / `track_ids` / `shapes` を POST し、空配列の場合はフィールドを省略する。`session-implementation.ts` では `ArgumentError` を用いたバリデーションを追加し、`conversionMode` が未指定のときは `inline` を補完する。`cvat-core/src/api-interfaces.ts` に `TrackerRunShapePayload` を export し、`cvat-ui` と CLI の両方で共有する。
- CLI では `function run-agent <function-id> --conversion-mode inline --track-ids 3,5 --shapes payload.json` のような併用を許可する。`--shapes` 引数は JSON ファイルパスか `@-`（stdin）を受け付け、`payload.json` には複数 shape を配列で記述できる。シナリオテストとして `tests/python/cli/test_cli_misc.py` に (1) Polygon のみ (2) Track と Polygon 混在 (3) 無効 conversion mode の各ケースを追加する。

### サーバー / バックエンド
- `TrackingActionRequestSerializer` に `conversion_mode = serializers.ChoiceField(['inline', 'preconvert'], default='inline')` と `shapes = TrackerShapeSerializer(many=True, required=False)` を追加し、`validate` で `if not attrs['track_ids'] and not attrs.get('shapes')` を禁止する。`TrackerShapeSerializer` は `id`, `client_id`, `label_id`, `frame`, `shape_type`, `points`, `z_order`, `rotation`, `group`, `occluded`, `outside`, `attributes` を持ち、`frame` と `request.frame` の一致、`label_id` が job.labels に含まれること、Polygon は 3 頂点以上（≧6ポイント）で偶数ポイントであることを検証する。
- `start_tracking_action` は以下の dataclass を導入して入力を正規化する：
  ```py
  @dataclass
  class TrackingSubject:
      kind: Literal["track", "shape"]
      label_id: int
      shape_type: str
      initializer: dict[str, Any]
      track_id: int | None = None
      original_shape_id: int | None = None
  ```
  `track_ids` から生成する subject には `kind='track'` を、`shapes` からは `kind='shape'` を設定し、`function.supported_shape_types` フィルタを両者に適用する。agent へ渡す `shape_payloads` は subject 由来で 1:1 に並べる。
- `AnnotationRequest.parameters` に `conversion_mode`, `shape_targets`（subject の initializer）を格納し、SAM2 agent には既存通り `shape_payloads`（`points`, `type`）だけを渡す。`tracker_supported_shapes` や `subject_counts` を telemetry スパンに含め、Grafana 側で可視化できるようにする。
- `_apply_tracking_results` を拡張し、`tracking_targets` の各要素に `kind` を持たせる：
  - `kind == 'track'` は従来通り既存トラックに追跡結果を追記。
  - `kind == 'shape'` の場合は `initializer` から `LabeledTrack` と初期 `TrackedShape` を新規作成し、`SourceType` は `conversion_mode` に応じて `AUTO`（inline）/`MANUAL`（preconvert）をセット。生成した `track_id` を使って残りフレームの結果を保存し、`change_payload['created']['tracks']` に Track diff を積む。
  - 変換元 shape は `LabeledShape.objects.filter(pk=original_shape_id)` で削除し、`change_payload['deleted']['shapes']` へ加える。削除対象が存在しない場合はログのみ残す。
- Telemetry (`functions.tracking.start/apply`) に `cvat.tracker.conversion_mode`, `cvat.tracker.subject_counts.track`, `cvat.tracker.subject_counts.shape` を記録して UX 解析に使う。`apply` 側では `cvat.tracker.created_tracks` や `cvat.tracker.deleted_shapes` も残してダッシュボードの指標にする。

### ドキュメント / テスト
- UI E2E テストでは、Polygon 選択時に SAM2 が候補へ残ること、inline / preconvert の両モードで結果が得られることを検証する。
- Python SDK / CLI テストで `conversion_mode` と `shapes` フィールドを持つ POST を追加し、`AnnotationRequest` が `shapes` のみでも受理されることを保証する。
- ドキュメントにはトグルのスクリーンショット、`cvat-cli function run-agent` で `shapes` JSON を渡す例、inline モードではサーバー完了まで Undo 不可であることを明記する。

## 影響範囲
- UI: `cvat-ui/src/components/annotation-page/annotations-actions/native-function-action.ts`, `cvat-ui/src/utils/tracker.ts`, `cvat-ui/src/components/annotation-page/annotations-actions/annotations-actions-modal.tsx`
- サーバー: `cvat/apps/functions/serializers.py`, `cvat/apps/functions/tracking.py`
- テスト: `cvat-ui/tests/tools-control.spec.ts`（shape 選択時のトラッカー表示）と `tests/python/cli/test_cli_misc.py`（Polygon/Maks 経由の Run Action）を追加/更新。
- ドキュメント: `README.md`, `site/content/en/docs/annotation/auto-annotation/segment-anything-2-tracker.md`, `site/content/en/docs/api_sdk/cli/`。

## リスクとフォローアップ
- inline モードは暗黙で `instance.annotations.save()` を走らせるため、大量 selection では UI がフリーズし得る。shape 数に応じて警告を出し、50 件超なら preconvert を推奨する。
- SAM2 リクエストが失敗した場合、`original_shape_id` を保持することで shape を自動削除しないが、ユーザーが別タブで編集したケースでは競合が発生する可能性がある。`LabeledShape` 削除失敗は単にログへ記録し、ユーザーに通知は行わない方針。
- CLI で `shapes` を送信する際、UI より厳格な検証が存在しない。`TrackerShapeSerializer` に shape_type ごとの最小頂点数・偶数点チェックを実装し、エージェント前に不正入力を検出する必要がある。
- Telemetry には conversion mode / subject counts を記録するが、現状ダッシュボードがないため OSS では raw log 解析のみ。運用で活用するには Grafana ダッシュボードを追加するフォローアップが必要。

## TODO
- [x] UI: `NativeFunctionTrackerAction` に shape 対応フィルタ・`TrackerSubject` 変換・inline/preconvert スイッチを実装し、localStorage 連携・最大 shape 警告を追加する。
- [x] SDK: `FunctionTrackerRunParams` 型／`server-proxy.ts`／`plugin-api` の `conversionMode`・`shapes` 対応と CLI `function run-agent` の新オプションを実装し、対応する CLI/E2E テストを追加する。
- [x] サーバー: `TrackingActionRequestSerializer`／`start_tracking_action`／`_apply_tracking_results` を拡張し、`TrackingSubject` の `kind` ハンドリングと `original_shape_id` 削除フロー、Telemetry の追加属性を実装する。
- [ ] ドキュメント: `README.md`・`site/...segment-anything-2-tracker.md`・`site/...api_sdk/cli` にトグル UI と CLI shape payload 例、inline モードの挙動制約を追記しスクリーンショットを差し替える。
- [ ] 監視: SAM2 Tracker 専用 Grafana パネルを作成し、`conversion_mode` と subject counts を可視化するダッシュボードを components/analytics に追加する。

## スコープ外タスク
- 型検証基盤: `tsc` が `cvat-canvas*`/`cvat-core` を巻き込まずに `cvat-ui` 単体で実行できるようにする取り組みは別イニシアティブで扱う。
- テスト基盤: `uv run pytest` で `cvat/apps/functions/tests/test_api.py` を直接流せる Python 環境整備は共通テスト基盤タスクへ移管する。

## Confirmed Facts
- `cvat-cli function run-agent`／SAM2 tracker agent は `AnnotationRequest.parameters['shapes']` をそのまま `TrackableShape` に変換できるため、サーバーが Polygon/Mask shape を渡せば追加改修なしに処理可能。
- CLI や SDK 側での SAM2 トラッカー登録・実行手順（`function create-native`/`run-agent`）は既に Polygon/Mask をサポートする実装であり、今回の課題は OSS UI/サーバーから適切なリクエストを発行する部分に限られる。
