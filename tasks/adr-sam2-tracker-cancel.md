# ADR: SAM2 Tracker Run Cancellation とクリーンアップ

- Status: Proposed
- Date: 2025-11-17
- Owner: CVAT OSS チーム

## 背景
- OSS 版 SAM2 Tracker は Run Annotation Action で開始すると UI 側のポーリングしか停止できず、ユーザーがキャンセルしてもサーバー/RQ/エージェントは最後まで処理を続ける。GPU 時間を浪費し、追跡結果が不要でも `_apply_tracking_results()` が既存アノテーションへ適用される。
- `AnnotationRequestStatus` に `cancelled` が存在せず、`FunctionRunStatus` も `pending/running/done/failed` のみ。Run を ID でアドレス可能にしているにもかかわらず停止 API がないため、長尺トラッキングを意図通り中断できない。
- Inline モードでは shape をサーバー側で自動保存してから追跡するため、処理途中で不要になっても形状が残り続け、Undo/Redo も効かない。途中フレームの `_apply_tracking_results()` が実行されると片側だけ書き換わる恐れがある。

## ゴール
1. エンドユーザーが Run Annotation Action からキャンセル操作を行うと、SAM2 Tracker の未処理フレームと実行中ジョブが即座に停止する。
2. キャンセル後は追跡過程で生成された一時データ（キャッシュされた states・inline で保存した shapes・途中まで生成された tracks）がロールバックされ、開始前と同じ状態に戻る。
3. Run の状態遷移とクリーンアップ結果を API / Telemetry に記録し、UI とログに反映できる。

## 決定事項
### 1. AnnotationRequestStatus に `CANCELLED` を追加
- `AnnotationRequestStatus` enum と DB 値に `cancelled` を追加。`AnnotationRequest` の default は従来通り `pending`。
- `FunctionRunStatusView` では `cancelled` を `status='failed'` 相当として扱わず、新たに `status='cancelled'` を返す。`progress` は中断時点の割合を返す。
- Telemetry では `cvat.functions.annotation_request.status` に `cancelled` を送り、Grafana でユーザーキャンセル数を計測する。

### 2. Run 単位のキャンセル API を追加
- 新規エンドポイント `POST /api/functions/runs/<uuid:run_id>/cancel` を `FunctionRunCancelView` として実装。認証済みユーザーが自分の Run のみ停止できる。
- サーバー処理:
  1. Run に紐づく `AnnotationRequest` を `select_for_update()` で取得し、`status in {pending, running}` のものを `cancelled` に更新。`result = {"detail": "Cancelled by user", "at": "<timestamp>"}` を記録。
  2. `rq.job.Job.fetch_many` で `pending/running` リクエストの RQ Job を引き当て、`job.cancel()` + `job.delete()` でキューから除去。
  3. 実行中に `agent_id` が付与されている場合は Redis Pub/Sub (`notifications.queue_listener`) へ `{"event": "request_cancelled", "request_id": ...}` を送信し、agent 側がポーリング中に run-loop を抜けられるようにする。
- Run 停止後は `handle_annotations_change` を呼ばず、`_apply_tracking_results` の再入を防ぐため `AnnotationRequest` の結果集約をスキップする。

### 3. Inline/Preconvert 共通のロールバック仕様
- 既に `_apply_tracking_results()` が呼ばれていない Run は、`cancel` 処理の最後に `_rollback_tracking_run(run_id)` を呼んで未確定状態を破棄する。
- `_rollback_tracking_run` の挙動:
  1. Run の `tracking_targets` を走査し、`kind == "track"` の場合は追跡対象トラックの `TrackedShape` を `start_frame` より後で `source=SourceType.AUTO` かつ `function_run_id` タグが一致するものだけ削除。
  2. `kind == "shape"`（inline で server 側が shape を保存したケース）の場合は `original_shape_id` を復活させる。削除済みならバックアップ (`AnnotationRequest.parameters["shape_backups"]`) から `LabeledShape` を再作成する。
  3. Run のために自動作成した `LabeledTrack`（`conversion_mode='inline'` で `kind='shape'` を追跡した際のテンポラリ）は `job_id` + `function_run_id` をキーに削除する。
- `_apply_tracking_results` には `if any(request.status == CANCELLED for request in run_requests): return` を追加し、キャンセル後に別の worker が偶然完了しても反映しない。

### 4. UI/UX 変更
- `AnnotationsActionsModal` のキャンセルボタンは `core.functions.runs.cancel(runId)` を呼んだうえで `cancellationRef.current = true` にする。
- ポーリング (`NativeFunctionTrackerAction.waitForRun`) は `summary.status === 'cancelled'` を検知して `"SAM2 tracker run cancelled"` を通知し、`reject` ではなく `resolve` してモーダルを閉じる。再実行可能にするため、`resetAfterRun` 後に `Canvas.setup` や `fetchAnnotationsAsync` は呼ばない。
- `core.functions` に `runs.cancel(runId: string)` を追加し、`serverProxy.functions.runs.cancel` を `POST /api/functions/runs/<run_id>/cancel` へマッピングする。

### 5. エージェント / ワーカー側のキャンセル対応
- SAM2 agent 実装（`components/sam2-agent`）は新しい SSE/Redis イベント `request_cancelled` と REST polling の両方でキャンセルを検出する。
- Agent がキャンセルを把握したら GPU 推論を停止し、`PATCH /api/functions/queues/<queue_id>/requests/<request_id>/fail` を `exc_info = "Cancelled by user"` で送信する。これによりサーバー側は `_handle_tracking_*` を呼ばず `status=cancelled` のまま固定する。

### 6. ドキュメントと計測
- `site/content/en/docs/annotation/auto-annotation/segment-anything-2-tracker.md` に「Run 中に Cancel を押すと推論が停止し、途中結果は破棄される」旨とスクリーンショットを追加。
- 運用ガイドラインに「キャンセル後の再実行は Run 完了を待たずに可能」「Grafana ダッシュボードで `cvat.tracker.cancelled_runs` を監視できる」ことを追記。
- Playwright / pytest でキャンセル → Run status `cancelled` を確認する統合テストを追加し、`AnnotationRequest` が `cancelled` になっていることと GPU 呼び出しが止まる（モックで検証）ことを保証する。

## 詳細設計
### API フロー
1. フロントエンドが `NativeFunctionTrackerAction.run()` から Run ID を受け取った後、キャンセルボタンを押すと `core.functions.runs.cancel(runId)` を呼ぶ。
2. バックエンド `FunctionRunCancelView` は Run に紐づく全 `AnnotationRequest` を `cancelled` に更新し、RQ job に `cancel()` を送る。
3. Agent は Redis Pub/Sub で `request_cancelled` を受信するか、`AnnotationRequestProgress` の POST が `400`（キャンセル済み）で失敗した時点でループを抜ける。
4. `_rollback_tracking_run` が inline 保存済み shape / track を削除し、`handle_annotations_change` へ `delete` イベントを送る。UI は差分を受け取り、表示を元のままに保つ。

### API 契約
- Endpoint: `POST /api/functions/runs/<uuid:run_id>/cancel`
  - 認証: `IsAuthenticated`。`FunctionRun.project.organization` が存在する場合は org admin / maintainer もキャンセル可能。
  - リクエスト: デフォルトは空 JSON。将来的な理由コード用に `{"reason": "user_request"}` を受け入れ、未指定時は `"user_request"` をセット。
  - レスポンス: `202 Accepted` で `{"run_id": str, "status": "cancelled", "processed_frames": int, "pending_requests": [int], "cleanup_status": "pending"|"done"}`。
  - エラー: 
    - `403`（他ユーザー Run）
    - `404`（Run または Queue が存在しない）
    - `409`（`status in {"done","failed"}` の場合、`{"status": "finished"}` を返す）
  - Idempotency-Key: `FunctionRun.id` を `Idempotency-Key` ヘッダーへ返し、UI が同じ Run に複数回 POST しても結果は一定。

### バックエンド実装詳細
- `FunctionRunCancelView` では共通サービス `FunctionRunCanceller` を呼び出す。擬似コード:
```python
class FunctionRunCanceller:
    def cancel(self, run: FunctionRun, actor: User):
        with transaction.atomic():
            requests = (
                run.annotationrequest_set
                .select_for_update(skip_locked=True)
                .filter(status__in=[AnnotationRequestStatus.PENDING, AnnotationRequestStatus.RUNNING])
            )
            if not requests.exists():
                return CancelResult(already_finished=True)
            updated = requests.update(
                status=AnnotationRequestStatus.CANCELLED,
                result=CancelResultPayload(actor),
            )
        self._cancel_rq_jobs(run, requests)
        self._publish_cancel_event(run, requests, actor)
        self._schedule_cleanup(run.id)
        return CancelResult(updated_count=updated)
```
- `_cancel_rq_jobs` は `rq.job.Job.fetch_many(list(requests.values_list("rq_job_id", flat=True)))` を使い、`job.cancel()` → `job.delete()` の順で実行。例外が出た場合は `retry_on` で再試行。
- `_publish_cancel_event` は `notifications.queue_listener.publish("request_cancelled", payload)` を呼び、agent 側は Pub/Sub 経由で即時キャンセル。
- `_schedule_cleanup` は即時実行（RQ）と遅延実行（Celery beat）の2段階。即時実行が失敗した場合に備えて遅延ジョブが 1 分後に再実行。

### 状態遷移と排他
- `AnnotationRequestStatus` の状態図:
  - `pending -> running -> done|failed`
  - `pending -> cancelled`
  - `running -> cancelled`
  - `cancelled` からの遷移はなし（吸収状態）。
- 排他制御:
  - キャンセル時は `select_for_update` で対象 Run の Request をロック。
  - `_apply_tracking_results` / `_rollback_tracking_run` は `pg_advisory_lock(hash(run_id))` を取得し、両者が同時に実行されないようにする。
  - `FunctionRun.summary_cache`（Redis）の値も `cancelled` に即書き込みし、UI ポーリングが DB 反映を待たない。

### データ構造
- `AnnotationRequest.parameters` に `{"function_run_id": str, "cleanup_tokens": {...}}` を追加し、inline 保存時に `shape_backups`（`serializer.data` のスナップショット）や `created_track_ids` を記録する。
- `TrackedShape` に `function_run_id`（nullable UUIDField）を追加しておくと Rollback 対象を一括削除しやすい。
- `AnnotationRequestResult` に `{"cancelled": true}` をセットすることで UI が run status を即時更新できる。

### ロールバック手順の詳細
1. `cleanup_tokens` に `target_tracks`, `target_shapes`, `created_tracks` を保持し、`DELETE FROM trackedshape WHERE function_run_id = %s` のバルク削除を最初に実施。
2. Inline でサーバー保存済みの shape は `cleanup_tokens["shape_backups"]` を `LabeledShapeSerializer(data=backup, context={"task": ..., "job": ...})` へ差し戻して再作成。再作成後に `annotation.save()` を呼ばず `bulk_create` でまとめて挿入。
3. `handle_annotations_change` には `{"action": "cancel_rollback", "run_id": run_id, "deleted_shape_ids": [...], "deleted_track_ids": [...]}` を publish し、UI は同じイベントで Canvas を復元。
4. すでに `_apply_tracking_results` が完了している場合は rollback をスキップし、`FunctionRun.cancelled_cleanup_status = "skipped"` を記録して再適用を防止。

### テレメトリ
- `functions.tracking.start` span: `cvat.tracker.run_cancelable=true`。
- `functions.tracking.cancel` span（新規）で `cancelled_by=user_id`, `pending_frames_remaining`, `processed_frames`, `conversion_mode` を記録。
- Grafana アラート: `cancelled_runs / total_runs > 0.3` で UI 問題を検知。

### UI 実装詳細
- `AnnotationsActionsModal` ではキャンセル中を示す `Button` ラベルを `t("Cancelling…")` に切り替え、`useEffect` で `isCancelling` が `true` の時は他ボタンを disabled。
- `core.functions.runs.cancel` は fetch 失敗時に `NotificationDispatcher.warn("Failed to cancel run, please retry.")` を発火し、再送が必要な場合に備えて `AbortController` を expose。
- `native-function-action.ts` の `run()` は `cancellationRef.current` が `true` の場合に限り、`setCanvasBlocking(false)` を呼び出して操作を即時復帰させる。
- `cvat-ui` の `useFunctionRunPolling` フック（新規）で `status === "cancelled"` を検知し、`dispatch(updateRun({ id: runId, status: "cancelled" }))` を発行して他 UI でも状態を共有。

### Agent / Worker フロー
- Redis Pub/Sub メッセージ: `{"event": "request_cancelled", "queue_id": int, "request_id": int, "function_run_id": "<uuid>", "reason": "user"}`。agent は `asyncio.Queue` に投入し、推論タスクが `await cancellation_event.wait()` で中断。
- フォールバック HTTP: Pub/Sub が届かない場合でも `AnnotationRequestProgress` POST が `409` を返した時点でキャンセル扱いにし、agent 側は `_report_cancelled()` を呼んでサーバーへ最終状態を送る。
- Worker (`cvat/apps/functions/tracking.py`) は `CancelledError` を受けたら部分結果を `tempfile` から削除し、`rq.get_current_job().meta["cancelled"] = True` とマークして復帰。

### エラー・タイムアウト処理
- API レスポンスに `Retry-After: 2` を添付し、UI は 2 秒後に `functions.runs.getSummary` を再度呼ぶ。
- Rollback が 30 秒を超えた場合は `FunctionRun.cancelled_cleanup_started_at` / `finished_at` を比較して `cvat.tracker.rollback_timeout` を Prometheus へ送信。SLO: p95 < 20s。
- Agent が 60 秒以内に `PATCH .../fail` を返さない場合、サーバー側 watchdog が再度 `rq.job.Job.cancel()` を実行し、`exc_info="Agent timeout after cancel"` を `AnnotationRequest.result` に残す。

## 影響範囲
- UI: `cvat-ui/src/components/annotation-page/annotations-actions/annotations-actions-modal.tsx`, `native-function-action.ts`, `cvat-core/src/api.ts`, `cvat-core/src/server-proxy.ts`.
- バックエンド: `cvat/apps/functions/models.py`, `serializers.py`, `views.py`, `tracking.py`, `notifications`, `telemetry`.
- エージェント: `components/sam2-agent`（Redis イベント購読 / HTTP キャンセル処理）。
- DB: `AnnotationRequest.status` の新値追加、`TrackedShape.function_run_id` カラム（migration）。
- テスト: Django (`tests/python/functions/test_tracking_cancel.py`), UI (Playwright/ Cypress), agent のユニットテスト。
- ドキュメント: OSS ドキュメント / 変更履歴 / リリースノート。

## トレードオフ
- Run 単位のキャンセルにより DB へバックアップを保存する I/O が増えるが、長尺タスク停止時の利得の方が大きい。
- Agent が Redis Pub/Sub を購読する必要があり、コンポーネント間結合が増す。ただし cancel 要件に必須であり、将来の Pause/Resume 拡張に流用できる。
- Rollback 用の `function_run_id` メタデータを `TrackedShape` や `LabeledTrack` に追加することでストレージ利用は僅かに増えるが、キャンセル後のクリーンな状態保証と引き換えに許容する。

## 移行計画
1. Migration で `AnnotationRequest.status` に `cancelled` を追加し、既存レコードは変更なし。
2. `TrackedShape.function_run_id` とバックアップ記録を同じリリースで導入し、既存 Run は `function_run_id = NULL` のまま扱う（追加の feature flag は設けない）。
3. Agent / UI / API を同一リリースで更新し、キャンセル操作とイベント受信が必ず対応するよう順次実装 → 動作確認 → 本番反映の順に進める。
4. ドキュメント/リリースノートでキャンセル機能追加と既知の制限（キャンセル後も数秒は GPU を開放できない等）を伝える。

## Open Questions
- Agent が GPU 推論をキャンセルした際に使用中のフレームキャッシュをどこまで破棄するか。`SAM2_INTERACTOR_CACHE_FRAMES` と同様の設定が必要か再検討。
- Rollback 時にユーザーが同じ形状を別の Run で編集していた場合の競合処理方法（ロック or 再確認ダイアログ）を UI にどう表現するか。
- Multi-user 環境で別ユーザーが Run を共有している場合の権限境界（現在は Function owner のみ）。Org admin に停止権限を広げるか要議論。
