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

## 計測ログ (2025-11-16)
- ローカル docker-compose（`SAM2_TRACKER_DEVICE=cpu`、`job=8` `track_id=27`、20フレーム）で `POST /api/jobs/8/functions/6/tracker-actions` を実行し、完了後に `psql` で `functions_annotationrequest` を参照して `updated_at - created_at` を算出。
- API 経由の呼び出しでは `Host: 192.168.10.190` ヘッダーを強制しないと Traefik が 502 を返す点に注意。

| 状態 | run_id | init_tracking avg[s] | track avg[s] | 備考 |
| --- | --- | --- | --- | --- |
| 変更前（TaskDatasetを毎リクエスト生成） | `7e0489c0-3b4c-4bcf-8d0b-c6154ea0b7d3` | 1.65 | **1.39** (min 1.13 / max 2.18) | `cvat-cli/_internal/agent.py` を HEAD に戻してビルド。 |
| 変更後（`_TrackerDatasetRepository` で共有） | `68311f30-b0a3-4891-9258-3790875cb2e1` | 2.37 | **1.29** (min 1.15 / max 1.36) | フレーム #1 でのみ `Prepared tracker dataset for task 8 (0.10s)` ログが出ており、その後の track AR では再生成されないことを確認。 |

- CPU 実行のため 1 フレームあたり 1.3 s 前後と推論時間が支配的になったが、TaskDataset 再利用だけで track AR の平均所要時間を **約 6.8%** 削減できた。GPU 実行時（従来 0.27 s/フレーム）であればデータセット構築コストの占める割合が高いため、さらに大きな改善が見込める。
- `psql` で使用したクエリ例:

```sql
SELECT type,
       COUNT(*) AS count,
       AVG(EXTRACT(EPOCH FROM (updated_at - created_at))) AS avg_duration
FROM functions_annotationrequest
WHERE parameters->>'function_run_id' = '<RUN_ID>'
GROUP BY type;
```

### GPU (RTX 4080, CUDA) での比較
- `SAM2_TRACKER_DEVICE=cuda` で `docker compose --profile sam2-agent up -d` を立ち上げ、同じ `job=8` / `track_id=27` で測定。
- 変更後（TaskDataset共有）の run_id `fa218fa0-f9ff-433c-a3f9-09b539ceed2e` は **init=1.26 s / track=0.0859 s**、run全体 4.89 s。  
- 変更前（毎回 TaskDataset 構築）の run_id `d8fe08bb-b280-48b1-89ff-f89f3f7d7d75` は **init=0.34 s / track=0.176 s**、run全体 11.25 s。  
- track AR 平均は **約2.0×** 短縮され、1リクエスト 80 ms 台で GPU 推論コストにほぼ近い値へ収束。初期化の 0.34→1.26 s は、共有バージョンが初回のみ `TaskDataset` を構築してキャッシュするため。長尺ジョブほど初期化コストを amortize でき、GPU占有時間を効率化できる。

## 次フェーズ: 実装/実験計画 (2025-11-17)
### 1. `_TrackerDatasetRepository` を正式機能へ昇格
- `cvat-cli/_internal/agent.py` と `cvat-sdk` の依存関係を洗い出し、`TaskDataset` の生成を `TrackerDatasetRepository.get(task_id, allow_chunks)` に一本化する。Interactor 実装とのコード共有を図り、LRU エビクションと `with_chunks=True` の切替ポリシーをオプション化する。
- 共有オブジェクトのライフサイクルを `FunctionRun` 単位ではなく `task_id` 単位に揃え、agent 再起動時にウォームアップする CLI オプション（例: `--preload-task 8`）を追加して大規模ジョブでも初回遅延を吸収する。
- 計測: `uv run python scripts/sam2/benchmark_tracker.py --job 8 --frames 20 --device cuda` で before/after の TaskDataset init 回数と所要時間をログに残す。CI や自動テストへの組み込みは不要だが、手元検証の手順を README に記す。

### 2. AnnotationRequest バッチ化 PoC
- `_enqueue_track_request` で `remaining_frames` をチャンク化する実装を feature flag で導入し、SAM2 agent へ `frames: [idx...]` を渡す REST schema を拡張。戻り値も frame ごとの shape 配列に変え、POC では 4 フレーム単位から開始する。
- Agent 側では `tracker.track()` の while ループ内に `for frame in chunk` を挿入し、`SAM2Tracker.track_step()` を連続呼び出しして結果をまとめて返す。Python 側での画像ロードを chunk 内で再利用し、`load_image()` 呼び出し回数を 1/chunk に減らす。
- 計画中の実験: `chunk_size ∈ {1,4,8,16}` で job 8 を GPU/CPU の両方で測定し、`AR count`, `total DB writes`, `run duration` をダッシュボード化。chunk サイズごとの最適点を決める。

### 3. Run status API/DB 最適化
- `functions_annotationrequest` に `(parameters->>'function_run_id')` を抽出した仮想列 + B-Tree index を追加する migration を作成し、`FunctionRunStatusView` に `values('type').annotate(count=Count('id'), avg_duration=Avg(...))` を導入してクエリ数を 1 回に集約する。
- その後、`FunctionRunSummary` (materialized) テーブルを検討し、Agent から run progress を PUT する設計と比較する RFC をまとめる。UI polling interval を 1 s に短縮しても 200 ms 未満で応答できるかを SLIs に設定。
- ベンチマーク: `ab -n 10 -c 1 http://localhost:8080/api/functions/runs/<id>` で median latency を計測し、4.7 s → 0.2 s をターゲットに進捗を記録。

### 4. `_apply_tracking_results` の差分更新
- `cvat/apps/functions/tracking.py` に `DiffBuilder` ヘルパーを追加し、agent から返ってきた shapes をフレーム毎に比較。変更フレームだけ delete/insert し、outside 付与は `TrackedShape` の `outside=True` update で済ませる。
- 現状の「全削除→bulk_create」を feature flag で残しつつ、diff モードで `handle_annotations_change` の呼び出しを 1 回にまとめる。`FunctionRun` の undo/rollback ロジックとも整合するよう、delete/insert の `function_run_id` 追跡を維持する。
- 実験: job 8（20 frames）と job 15（200 frames 仮定）で適用時間を測定し、`diff_mode` が何フレームで既存より優位になるかを算出する。

### 5. 計測・監視整備
- `SAM2_TRACKER_VERBOSE=1` で `TaskDataset init`, `frame fetch ms`, `track_step ms`, `apply_diff ms` を構造化ログに吐くフラグを追加し、`docker compose --profile sam2-agent logs -f sam2-tracker-agent | jq` で即時可視化できるよう整える。
- `tests/python` に軽量な regression テストを追加し、`pytest tests/python/tracker --run-slow` 実行時に `functions_annotationrequest` の件数と `updated_at - created_at` 平均が 300 ms 以下であることを確認する（chunk サイズ 4 で計測）。
- Grafana/Prometheus の exporter を用意し、`function_run_duration_seconds` `tracker_frame_fetch_seconds` などのメトリクスを収集。長期的に改善効果を監視する。

### 6. リスクとフォローアップ
- `TaskDataset` を共有するとピークメモリが増えるため、`SAM2_TRACKER_MAX_DATASET_MB` を導入して OOM を防ぐ。エビクション時は run を中断しないよう fallback で再ロードするパスを確保する。
- AR バッチ化は API スキーマ互換性リスクがあるので、`functions/6` を v2 として登録し UI 側から capability を見て呼び分ける案も検討する。
- Run status の索引追加で DB ロックが必要になるため、OSS インストールガイドに `python manage.py migrate` だけで適用できることを明記し、migration 失敗時のロールバック手順をまとめてから PR を出す。

## 実装ログ (2025-11-17)
- `_DatasetRepositoryBase` を導入し、Interactor/Tracker のデータセット共有ロジックを共通化。Tracker 側は `allow_chunks=True` で `TaskDataset(media_download_policy=PRELOAD_ALL)` を試み、`UnsupportedDatasetError` 時のみ on-demand にフォールバックするよう実装した（`cvat-cli/_internal/agent.py`）。準備時間を INFO ログに統一し、フォールバック時は WARN で把握できる。
- `function run-agent` に `--tracker-preload-chunks` フラグを追加し、SAM2 Tracker エージェント起動時に `with_chunks=True` / dataset プリロードを opt-in で有効化できるようにした。CLI 例:  
  ```bash
  uv run python -m cvat_cli function run-agent 6 --tracker-preload-chunks \
    --max-cache-tasks-with-chunks 2 --max-cache-tasks-without-chunks 8
  ```
  GPU 実行時は chunk プリロードが成功しているかを `Preloaded tracker dataset for task ...` ログで判断可能。
- キャッシュリミッターの `with_chunks` も tracker 設定に追従させたため、プリロード有効時は Interactor と同じ LRU 制御で chunk ディスク占有を抑制できる。フォールバックした場合は `Task ... does not support tracker chunk preloading` の WARN で把握し、必要に応じて `--tracker-preload-chunks` を無効化する運用とした。

### 手動検証ログ (2025-11-16, Function #7 / CPU + `--tracker-preload-chunks`)
- ⚠️ このセクションは CPU 実行のみ。GPU 環境での値にしか関心が無いことを明示し、今後は GPU 測定ログを最優先で蓄積する。
- 既存の `functions/6` は共有環境のエージェントと競合するため、検証専用の `AI Tracker: SAM2 Dev` (`function_id=7`) を CLI で登録。`job=8`（20 frames, `track_id=27`）に対し `POST /api/jobs/8/functions/7/tracker-actions` を投げ、`run_id=32f42fca-a5a7-45ff-af62-7a38c2d39263` `initial_request_id=74a8d946-07bb-4cd3-8fa0-1665031d134c` を取得。
- エージェント実行コマンド:
  ```bash
  PYTHONPATH=cvat-cli/src UV_HTTP_TIMEOUT=120 \
    uv run --project ai-models/tracker/sam2 python -m cvat_cli \
    --auth admin:admin --server-host http://192.168.10.190 --server-port 8080 \
    function run-agent 7 \
    --function-file ai-models/tracker/sam2/func.py \
    -p model_id=str:facebook/sam2.1-hiera-small \
    -p device=str:cpu \
    --tracker-preload-chunks \
    --max-cache-tasks-with-chunks 2 \
    --max-cache-tasks-without-chunks 8 \
    --burst
  ```
  ログ先頭で `Preloaded tracker dataset for task 8 (0.32s)` が出力され、以降は 19 件の `track` AR をキャッシュ済み `TaskDataset` から順次処理できた。
- `/api/functions/requests/<id>` から `created_at`/`updated_at` を取得し、初期化 + 各フレームの所要時間を算出（`uv run --project ai-models/tracker/sam2 python - <<'PY' ...` でワンショット集計）。CPU 実行でも track AR は **平均 1.30 s**（最小 0.99 s / 最大 1.39 s）で揃い、TaskDataset 再構築が排除できていることを確認。

| Frame | Type | Request ID | Duration [s] |
| --- | --- | --- | --- |
| 0 | init_tracking | 74a8d946-07bb-4cd3-8fa0-1665031d134c | 8.230 |
| 1 | track | a6e525d0-f50c-4a6e-b2d6-0c90c80b0de8 | 0.989 |
| 2 | track | 10943af3-8318-4ee7-acbb-d5afbafd5b37 | 1.088 |
| 3 | track | 810af7b4-1d34-45b5-865c-a2da2ce396d8 | 1.136 |
| 4 | track | 58c31fa6-9c4b-4a6f-aabd-90941c215ad6 | 1.172 |
| 5 | track | 71fedc81-899d-4028-bef0-d95f76131514 | 1.231 |
| 6 | track | 35b9b413-edbf-45e5-aea7-d855ed9a0e9e | 1.312 |
| 7 | track | c841f299-223a-4f5a-99d3-7fbce9781543 | 1.380 |
| 8 | track | 10eefd36-81ca-435e-a37f-771c09b463e8 | 1.360 |
| 9 | track | 23331bfc-1028-43b7-b337-12a071132f41 | 1.351 |
| 10 | track | 006d3fa5-2c0f-4375-8e41-e894edfcdd1f | 1.358 |
| 11 | track | dc13d636-2c14-4b01-bb14-f3783cdba0b7 | 1.393 |
| 12 | track | 98ffa7a7-dc81-48c4-8f97-b1b5997935bf | 1.367 |
| 13 | track | e12954f5-cdf8-4bf2-81b5-1f134c49a82e | 1.348 |
| 14 | track | 1e915342-8561-46f4-bcc7-d0b56917fea4 | 1.360 |
| 15 | track | 96b3d0f1-c888-46cd-98ee-0df2c20de3ce | 1.359 |
| 16 | track | b3fec5fa-3a66-48e8-836d-5cebc66c8154 | 1.355 |
| 17 | track | c577bfbc-3a0b-4b66-b48b-d1a6c5336cee | 1.358 |
| 18 | track | 7d84f3c9-82f1-4edf-9040-926035e32028 | 1.387 |
| 19 | track | 93268ee0-0fa1-4c5b-bf24-55dff3fa5079 | 1.367 |

- `GET /api/functions/runs/32f42fca-a5a7-45ff-af62-7a38c2d39263` は依然 ~4.8 s 要しており、JSONB seq scan 問題が残存。Run summary 改修（優先度3）の必要性を再確認できた。

## 次フェーズの高速化計画 (2025-01-xx)
### Experiment 1: Tracker AR chunk batching on GPU (RTX 4080 前提)
- **目的**: `track` AR をチャンク単位（例: 16〜32 フレーム）でまとめ、REST/DB オーバーヘッドと SAM2 warmup 再実行を抑制する。
- **実装方針**:
  1. `tracker-actions` payload に `batch_size` / `frames` を追加し、agent 側で `TaskDataset` を 1 度だけ構築後 `for frame in frames` で `track_step` を回すパスを追加。
  2. UI / backend では capability フラグ `supports_batched_tracker=true` を参照し、旧 API との互換を維持。
  3. Agent は `torch.cuda.Stream` を使って `preprocess → track → postprocess` を非同期化し、GPU 利用率 70% 以上を維持する。
- **計測**:
  - Job 8（20 frames）と Job 15（200 frames 仮定）で `batch_size=1,8,16,32` を比較し、`AnnotationRequest` 件数・平均処理時間・GPU メモリ使用量を収集。
  - 期待値: `batch_size=16` で 200 frames を ≦6 s (SAM2 推論のみ) + 追加 I/O 2 s 以内に収める。
  - ログ: agent 側に `TRACK_BATCH=<frames>` を INFO で出し、`uv run ... | jq` で即確認できるようにする。

### Experiment 2: Dataset streaming/caching pipeline hardening
- **目的**: Tracker / Interactor で共用する chunk-preload キャッシュを GPU 前提で検証し、大規模 task の I/O 停滞を除去。
- **ステップ**:
  1. `--tracker-preload-chunks` 有効時に `MediaDownloadPolicy.PREFETCH_CHUNKS_ONCE` を選べるよう CLI フラグを追加（未実装の場合）。
  2. LRU の eviction telemetry (`cache_hits`, `evictions_with_chunks`) を Prometheus exporter へ配管。
  3. RTX 4080 + NVMe 環境で `job=30`（1,000 frames 予定）を使い、`TaskDataset init`, `chunk download`, `track_step` の時間を構造化ログで採取。
- **検証**: chunk キャッシュを無効/有効で比較し、`track` AR 時間が 1.3 s → 0.3 s 台へ縮むかを確認。IO バックプレッシャー発生時は `aiohttp` の connection pool サイズを増減して追跡。

### Experiment 3: Run status endpoint refactor
- **目的**: `GET /api/functions/runs/{id}` レイテンシを 4.7 s → 0.2 s 未満へ。UI ポーリングを 1 Hz まで引き上げても DB 負荷が跳ねない状態を作る。
- **TODO**:
  1. `FunctionRun` → `FunctionRunStatus` 集約テーブルを作り、`jsonb` に閉じない列（`progress`, `active_requests`, `failed_requests`）を正規化。
  2. `functions_runstatus_idx` (BTree on `(run_id, updated_date)`) を migration で追加。
  3. DRF view で `select_related` + `prefetch_related(None)` を徹底し、N+1 を排除。
- **検証指標**: `ab -n 20 -c 5` / `wrk -c 16 -d 30s` で percentile を比較。`django_debug_toolbar` の SQL count も記録し回帰を検出。

### Experiment 4: `_apply_tracking_results` diff モードと並列適用
- **目的**: 4 s かかる全削除→再挿入を廃止し、差分更新+並列書き込みにより Job 8 で ≦0.8 s、Job 30 で ≦3 s に短縮。
- **工程**:
  1. `DiffBuilder` プロトタイプを `cvat/apps/functions/tracking_diff.py` に実装し、`TrackedShape` 単位で `added/updated/deleted` を算出。
  2. `apply_tracking_results(diff_mode=True)` を feature flag で切替可能にし、`settings.FUNCTIONS_TRACKING_APPLY_MODE` で制御。
  3. `transaction.atomic` 内で Frame ID ごとにバッチを切り、`asyncio.to_thread` で DB ライターを 2〜4 並列に走らせるオプションを調査（PostgreSQL のセッション数制限へ留意）。
- **検証**: `pytest cvat/apps/functions/tests/test_tracking_apply.py -k diff` を追加し、差分計算の idempotency を保証。実ジョブでは `job=8/30` の `annotations` 反映所要時間を `curl` で測定。

### Experiment 5: 観測と回帰セーフティネット
- `SAM2_TRACKER_VERBOSE` に GPU kernel 実行時間（`cudaEvent`）を追加し、`nvidia-smi dmon` と突き合わせてボトルネックを即判断できるようにする。
- `tests/python/tracker/test_tracker_bottlenecks.py` を作成し、`pytest --run-bottleneck` でチャンク化 + diff モードの有効/無効を切替えながら所要時間のリグレッションを検知。
- GitHub Actions（GPU unavailable）の代替として、`docker compose -f docker-compose.dev.yml -f dev/docker-compose.cuda.yml` で smoke テストのみを実行し、主要なロジックは mock で検証する。
