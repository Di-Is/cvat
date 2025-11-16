# SAM2 Tracker (OSS) Performance Measurements

## Environment
- Server: `http://192.168.10.190:8080` (OSS stack)
- User: `admin`
- Test data:
  - Job `2` (3 frames) for API sanity checks
  - Job `8` (20 synthetic PNG frames, label `obj`, track id `27`) for timing
- Functions: `AI Tracker: SAM2` (`id=6`)
- Tools: `curl`, `uv run python` scripts that called public REST APIs only

## Measurement Methods
| Phase | Method |
| --- | --- |
| UI pre-processing | Triggered `NativeFunctionTrackerAction` logic indirectly by calling annotation save/load endpoints and timing individual HTTP requests. |
| Tracker submission | Measured `POST /api/jobs/{job}/functions/{function}/tracker-actions` wall clock time via Python `requests` (basic auth). |
| Agent workloads | After submission, polled `GET /api/functions/runs/{run_id}` every 0.25–0.5s; once complete, fetched `/api/functions/requests/{request_id}` to parse `created_at` / `updated_at` for each `AnnotationRequest` (init + track). |
| Result apply | Observed difference between run completion and annotation payload updates via repeated `GET /api/jobs/{job}/annotations`. |
| Run status overhead | Timed individual `GET /api/functions/runs/{run_id}` calls immediately after completion to isolate DB/query time. |

## Timing Results (Job 8, 20 frames)
| Phase | Observation |
| --- | --- |
| Annotation save + reload | `POST /annotations` + `GET /annotations` ≈ 0.26–0.30 s per tracker invocation (frame density dependent). |
| Tracker POST | ≈ 0.13 s per call. |
| `init_tracking` AR | Duration from `created_at` to `updated_at`: **1.06 s** (includes first frame load + SAM2 precompute). |
| `track` AR (per frame) | ≈ **0.27 s**/frame (19 frames → ~5.1 s total). Cost dominated by per-frame dataset fetch + SAM2 `track_step`. |
| Result apply | After last `track` completes, `_apply_tracking_results` deletes/reinserts shapes for all frames (see `cvat/apps/functions/tracking.py:520-640`). Observed additional ~4 s before annotations endpoint reflects updates. |
| Run polling latency | Even after completion, single `GET /api/functions/runs/{run_id}` takes **≈4.7 s** (due to full-scan JSON lookups inside `FunctionRunStatusView`). |

## Findings & Prioritized Improvements
### 大規模動画（数万フレーム）を前提とした優先順位
1. **トラッキングARの逐次化**（最優先）  
   - 現状はフレームごとに `AnnotationRequest` を生成し、各ARで単一フレームをSAM2に投げる。数万フレームではAR件数とREST/DBオーバーヘッドが線形に膨れ上がり、推論以外だけで数十分〜数時間かかる。  
   - 対応例: 連続フレームを1件のARにまとめて処理する、エージェント側で `TaskDataset` をプリロードしながらループ実行する等。

2. **結果適用時の全フレーム再生成**  
   - `_apply_tracking_results` が対象区間の全フレームを削除→再挿入しており、フレーム数に比例してDB書き込み時間も肥大化する。  
   - 差分アップデート（変更があるフレームのみ更新＋outsideキーフレーム追記）、もしくは区間分割applyを導入して処理時間とイベント発火量を抑える。

3. **Run status endpoint（ポーリングのDBコスト）**  
   - `GET /api/functions/runs/{run_id}` が1回 ≈4.7 s かかり、UIの進捗更新が鈍い。バッチ化で全体の所要時間を短縮した後も、インデックス追加や集計テーブル化でレスポンスを改善する必要がある。

4. **UI前処理の往復**  
   - `annotations.save()`→`annotations.get()` の往復は1回あたり0.3 s程度で固定コスト。フレーム数が増えても相対比は下がるが、UX向上のため余力があれば形状ペイロード生成をフロントキャッシュから行うように見直す。

（従来の小規模ジョブ中心の優先度では Run status/UI が上位だったが、長尺動画では 1・2 を先に解消しないと根本的にスループットが出ない。）

These data points should guide future optimization work; repeating the measurement after each change will confirm regressions or gains.

## 追加調査メモ (2024-xx-xx)
### Agent 側データ取得のボトルネック
- `_calculate_result_for_tracking_ar` は `init_tracking` / `track` のたびに `_get_sample_from_ar_params` を呼び、都度 `TaskDataset` を生成してから `sample.media.load_image()` でフレームをダウンロードする（`cvat-cli/src/cvat_cli/_internal/agent.py:1036-1084,1120-1139`）。`TaskDataset` は全フレームの `Sample` を `range(task.size)` から組み立てるため（`cvat-sdk/cvat_sdk/datasets/task_dataset.py:28-127`）、フレーム数 N のジョブを M 回リクエストすると O(N·M) ではなく O(N²) 近い CPU/IO を消費する。
- `MediaDownloadPolicy.FETCH_FRAMES_ON_DEMAND` が固定で使われ、`sample.media.load_image()` は毎回 `GET /api/tasks/{id}/data?type=frame` 相当の HTTP を張り直す（`cvat-sdk/cvat_sdk/datasets/task_dataset.py:205-208`）。GPU 推論 60–80 ms に対してフレーム取得だけで ~150 ms 以上を占め、測定した 0.27 s/フレームの大半が I/O 待ちであると推測される。
- インタラクタでは `_InteractorDatasetRepository` で `TaskDataset` を task 単位でキャッシュしている一方、トラッカーは `with_chunks=False` のキャッシュ制限だけで実体を共有していない。結果として 1 ラン中に同じ task_id の `data_meta.json` / `annotations.json` を何十回も読み直し、`PIL` デコードも繰り返している。

### Run status API の全表スキャン
- `GET /api/functions/runs/{run_id}` は `AnnotationRequest.objects.filter(parameters__function_run_id=...)` を multiple query で繰り返し呼ぶ実装になっており（`cvat/apps/functions/views.py:211-420`）、`parameters__function_run_id` に索引が無い（`cvat/apps/functions/models.py:86-115`）。JSON フィールドの `contains` は PostgreSQL が seq scan → `jsonb_extract_path_text` を都度評価するため、1 ランで数百 AR があると 4–5 s/リクエストの待ちが発生する。
- さらに `_summarize_run_status` は同じ QuerySet に対して `.count()` `.filter(...).count()` `.exists()` を個別に投げるため、1 回のステータス更新で 6 回以上クエリが走る。UI ポーリング間隔（3 s）より遅く、キャンセル検知も鈍い。

### `_apply_tracking_results` の全削除/再挿入
- 追跡終了後は `TrackedShape.objects.filter(frame>=start_frame)` をまとめて削除→`bulk_create` で該当区間を再構築する実装になっている（`cvat/apps/functions/tracking.py:516-639`）。トラック数 T・フレーム数 F に比例して DB 書込みと `handle_annotations_change` が発火するため、測定した「結果適用 4 s」の大半はここで発生している。
- 対象フレームに変更が無い場合でも `last_points` をそのまま書き戻すので、差分適用が一切行われない。結果的に `function_run_id` を付けた `TrackedShape` を後からまとめてロールバックする実装とも整合せず、ストレージ量と WAL も増える。

## 高速化アイデア / 次アクション案
1. **TaskDataset の再利用 / 単フレームアクセス API 追加**  
   - `TrackerDatasetRepository`（仮）を `_InteractorDatasetRepository` と同様に導入し、`task_id` ごとに `TaskDataset(media_download_policy=FETCH_FRAMES_ON_DEMAND)` を共有。agent 起動時に `max_cache_tasks_without_chunks` 上限を守りつつ LRU で破棄する。  
   - 併せて `TaskDataset` に「特定 frame_index の Sample だけ構築するモード」や `get_sample(frame_index)` を追加し、N=100k のジョブでも初期化 O(N) を避ける。

2. **フレームプリフェッチと chunk cache**  
   - Tracker 用にも `with_chunks=True` パスを許可し、画像（imageset）ジョブは zip chunk を先読み、動画ジョブは `TaskDataset` が `data_chunk_type=="video"` でも ffmpeg で連番化できるよう拡張する。  
   - `_calculate_result_for_track_ar` で次フレームを別スレッドで `load_image()` しておき、GPU 推論の待ち時間を隠蔽する。

3. **Run summary の正規化**  
   - `AnnotationRequest` に `run_id` 専用の `UUIDField` + B-Tree index を追加、もしくは `FunctionRun` テーブルを新設して `total_requests`, `completed_requests`, `status`, `failed_request_id` を非正規化。  
   - `_summarize_run_status` は単一 `annotate()` で `COUNT FILTER (WHERE ...)` を取るよう書き換え、UI ポーリングの 4.7 s → <200 ms を目指す。

4. **結果適用の差分化**  
   - `_apply_tracking_results` を「agent から返ったフレームだけ更新」「outside を付け足すだけのフレームは `UPDATE ... SET outside=true`」に分岐。  
   - `handle_annotations_change` 呼び出しも create/update/delete それぞれ 1 回ずつにまとめ、自動イベント量を抑える。

5. **AR バッチ化の具体化**  
   - `_enqueue_track_request` の `remaining_frames` を固定サイズのチャンク（例: 8 フレーム）で消費し、1 AR が複数フレームの `states` と `shapes[]` を返すよう SAM2 agent を拡張。  
   - エージェント側は `track()` 内で `for frame in chunk:` を回して結果配列を構築、`AnnotationRequest` は chunk 単位で DB を更新する。REST/DB のオーバーヘッドが 1/N になり、長尺動画でも現実的な待ち時間にできる。

測定を更新する際は (1) dataset 再利用の有無、(2) 1 AR あたりの `TaskDataset` init 回数、(3) `GET /functions/runs` のレスポンス時間 をログに仕込んでおくと、次の高速化フェーズで回帰を検知しやすい。
