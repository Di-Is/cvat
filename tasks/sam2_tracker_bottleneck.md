# SAM2 Tracker (OSS) Performance Measurements

## Environment
- Server: `http://192.168.10.190:8080` (OSS stack)
- User: `admin`
- Test data:
  - Job `2` (3 frames) for API sanity checks
  - Job `8` (20 synthetic PNG frames, label `obj`, track id `27`) for timing
    （2025-11-20 時点では `sam2_measure_20` を再生成したため、実際の Job ID は `1` / track id `1`。便宜上これまで通り Job8 と呼称する。）
  - Job `9` (200 synthetic PNG frames, label `obj`, track id `2`) for長尺検証
    （同じく `sam2_measure_200` を再生成しており、現在は Job ID `2` / track id `2`。文中では Job9 として扱う。）
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

### 2025-11-19 full-pipeline re-measurement (Job 2 / batch16)
| Phase | Measurement (wall clock) | Evidence |
| --- | --- | --- |
| Annotation save (`PATCH /api/jobs/2/annotations?action=create`) | 0.118 s | `logs/http/job2_annotations_patch_create_20251119.txt` |
| Annotation reload (`GET /api/jobs/2/annotations`) | 0.128 s | `logs/http/job2_annotations_get_20251119.txt` |
| Tracker submission (`POST /tracker-actions`, batch16) | 0.126 s | `tasks/sam2_tracker_measurements_20251119_job2_full.json` (`submit_latency`) |
| `init_tracking` AR | 1.08 s | same JSON (`init_duration_s`) |
| `track` AR (13 chunks / 199 frames) | 12.59 s total (63 ms/frame avg) | same JSON (`total_track_duration_s`, `avg_track_per_frame_s`) |
| Result apply (`_apply_tracking_results`) | 0.032 s gap between last AR update and run_status update | `logs/sql/run_status_apply_gap_4d1f1e0e_20251119.txt` |
| Run status GET (single poll) | 0.102 s (`curl`), `P95≈108 ms` (`ab -n20 -c5`) | `logs/sql/run_status_ab_4d1f1e0e_after_20251119.txt` |
| Run status GET under load (`wrk -t4 -c32 -d5s`) | 192 ms avg / 324 ms max | `logs/sql/run_status_wrk_4d1f1e0e_after_20251119.txt` |
| Run status SQL (`include_legacy=0`) | 0.063 ms (seq scan across 235 rows) | `logs/sql/run_status_profile_after_20251119_job2.txt` |

- 1 ランの壁時計 13.7 s のうち **92% が推論 (`init_tracking`+`track`)** で占められており、1 チャンク目は依然 1.18 s（warmup + chunk fetch）と突出。Experiment 2 の ChunkCache で改善したとはいえ、ChunkDiff/差分適用が無い限り大規模ジョブではここが支配的。
- `_apply_tracking_results` は `FunctionRunStatus.updated_at` まで 32 ms しかかからず、以前観測した 4 s 待ちは再現しなかった（batch16 + NOT NULL run_status FK 運用では十分に速い）。
- Run status API は JSONB フォールバックを外しても **P95 ≈108 ms** / 平均 192 ms (wrk, 32 接続) と目標 50 ms に届かない。ボトルネックは DRF までの Python オーバーヘッドで、TODO #3（JSONB path の削除 + serializer を summary 専用にする）と `select_related` の対象絞り込み、あるいは `@lru_cache` / Redis cache を挟まない限り UI ポーリング 1 Hz で 100 ms×回数 の固定コストが残る。

### 2025-11-19 XXXX225-02 (500 frames, Job 7 / track 12)
- 元動画 `test_videos/XXXX225-02.mp4` (30 fps, 1920×1080) から `ffmpeg -vf fps=5 -qscale:v 2` で 3,794 枚の JPEG を抽出し、先頭 500 枚のみを `tmp/XXXX225-02_frames_500` へコピー。`zip -qr tmp/XXXX225-02_frames_500.zip .` で固めた後、`cvat_cli task create COPG225_02_tracker_500 local tmp/XXXX225-02_frames_500.zip` を実行して Task 8 / Job 7 を作成した（label `obj` のみ）。
- `job 7` へ `PUT /api/jobs/7/annotations` で 0 フレームに矩形 Polygon を投入し、生成された Track ID は 12。測定ログは `logs/http/job7_annotations_{put,get}_20251119.txt`。
- 計測コマンド:  
  ```bash
  PYTHONPATH=cvat-cli/src:cvat-sdk UV_HTTP_TIMEOUT=120 \
    uv run python scripts/sam2/benchmark_tracker.py \
      --server http://localhost:8080 \
      --host-header 192.168.10.190 \
      --username admin --password admin \
      --job 7 --function 3 --track 12 \
      --start-frame 0 --target-frame 499 \
      --batch-sizes 16 \
      --tracker-preload-chunks --include-fetch-metrics \
      --agent-log-dir logs/sam2_tracker \
      --output tasks/sam2_tracker_COPG225_500_batch16.json
  ```
  Run ID `d49ab6d5-1d46-4373-9dc7-80249184a052` の詳細と chunk 単位の所要時間は JSON に保存済み。

| Phase | Measurement (wall clock) | Notes |
| --- | --- | --- |
| Annotation PUT (`PUT /jobs/7/annotations`) | **0.170 s** | `logs/http/job7_annotations_put_20251119.txt` |
| Annotation GET (`GET /jobs/7/annotations`) | **0.128 s** | `logs/http/job7_annotations_get_20251119.txt` |
| Tracker submission (`POST /tracker-actions`, batch16) | **0.122 s** | `submit_latency` in JSON |
| `init_tracking` AR | **1.07 s** | includes dataset bootstrap / first frame warmup |
| `track` AR (31×16 + 1×3 frames) | **58.96 s total** (avg **118 ms/frame**, min 104 ms, max 138 ms) | 32 requests processed sequentially |
| Run wall clock (init start → last track AR done) | **60.26 s** | 97.8% pure inference; `_apply_tracking_results` + status update ≈0.23 s |
| Run status GET (post-completion) | **0.103 s** | `logs/http/run_d49ab6d5-1d46-4373-9dc7-80249184a052_get_20251119.txt` |

- XXXX225-02 の 1080p フレーム群では per-frame 推論コストが 0.118 s まで増加し、Job8 の合成 PNG (512px) で得た 63 ms/frame に対して **≈1.9×**。Decode + SAM2 `track_step` の合算が支配的で、500 枚でも 60 s/Run とエンドユーザーが待てる限界に近い。
- Chunk 32 本のうち最終チャンクのみ 3 フレームで、残りは batch16 で固定長。Init 1.07 s + track 58.96 s のみで 60.03 s となり、`_apply_tracking_results` / run status 更新は 0.23 s と無視できるレベル。したがって **ボトルネックは GPU 推論** で確定。
- `--include-fetch-metrics` 付きでログを収集したが、`sam2-tracker-agent` が `SAM2_TRACKER_LOG {"phase":"frame_fetch", ...}` を出力しておらず `avg_fetch_ms` / `hit_ratio` は `null` のまま（`SAM2_TRACKER_VERBOSE=1` が compose 側に反映されていない）。Chunk preload 成否や cache hit 率の可視化には agent 側環境変数を追加し再計測が必要。

### 2025-11-20 XXXX225-02 再計測 (500 frames, Job 7 / track 32)
- 既存の `test_videos/XXXX225-02.mp4` → `ffmpeg -vf fps=5 -qscale:v 2 -frames:v 500` で作成した `tmp/XXXX225-02_frames_500/` を再確認し、`frame_000001.jpg` が 1920×1080 であることを `ffprobe` で検証（500 枚揃っていることも `ls | wc -l` で確認）。
- `tmp/COPG_task8_track.json` を `PATCH /api/jobs/7/annotations?action=create` で投入して Track 32 を生成し、下記コマンドで `benchmark_tracker.py` を再実行。結果は `tasks/sam2_tracker_COPG225_500_batch16_rerun_20251120.json`、ログは `logs/sam2_tracker/sam2_tracker_run_b01daa10-9986-4e64-b03e-8ab2c0a57534.log` に保存。
  ```bash
  PYTHONPATH=cvat-cli/src:cvat-sdk UV_HTTP_TIMEOUT=120 \
    uv run python scripts/sam2/benchmark_tracker.py \
      --server http://192.168.10.190:8080 \
      --host-header 192.168.10.190 \
      --username admin --password admin \
      --job 7 --function 3 --track 32 \
      --start-frame 0 --target-frame 499 \
      --batch-sizes 16 \
      --tracker-preload-chunks --include-fetch-metrics \
      --agent-log-dir logs/sam2_tracker \
      --output tasks/sam2_tracker_COPG225_500_batch16_rerun_20251120.json
  ```
  Run ID `b01daa10-9986-4e64-b03e-8ab2c0a57534` / Function 3 (`SAM2 Tracker`).

| Phase | Measurement (wall clock) | Notes |
| --- | --- | --- |
| Annotation GET (`GET /api/jobs/7/annotations`) | **0.459 s** | `curl -w '%{time_total}' ...`。500 frame の全結果を JSON で返すため 0.45 s 前後。 |
| Tracker submission (`POST /tracker-actions`, batch16) | **0.123 s** | `submit_latency` |
| `init_tracking` AR | **1.13 s** | `init_duration_s` |
| `track` AR (31×16 + 1×3 frames) | **23.08 s total** (avg **46 ms/frame**, min 42 ms, max 61 ms) | `total_track_duration_s` / `avg_track_per_frame_s` |
| Run wall clock (init start → last track AR done) | **24.44 s** | `wall_clock_s`（init + track = 24.21 s → `_apply_tracking_results` + status update ≈0.23 s） |
| Run status GET (post-completion) | **0.109 s** | `curl -w '%{time_total}' /api/functions/runs/b01daa10-...` |

- `dataset_fetch.avg_fetch_ms = 3.70 ms`, `hit_ratio = 1.0`, `cache_hits = 499`（`cache_misses = 0`）で、`--tracker-preload-chunks` が全チャンクに効いていることが確認できた。`sam2-tracker-agent` ログでも `chunk_preload_enabled:true` が記録され、HTTP ダウンロードは初期化フレーム以外で発生していない。
- per-frame 推論コストは **118 ms → 46 ms** と 2.5× 改善され、Run 全体は 60 s → 24 s。`wall_clock_s` の 94.5% を `track` AR（GPU `track_step`）、4.6% を `init_tracking`、残り 0.9% を結果適用/ステータス更新が占める構成になった。以後の短縮は GPU カーネル（VOS optimized path の既定化、さらなるバッチ拡大、量子化）に注目する必要がある。
- 500 frame 分の annotation JSON を都度送受信すると 0.46 s かかるため、UI 側での頻繁な保存/再読込は引き続き UX を悪化させる。差分 apply API または result chunking を実装しない限り、長尺タスクではトラッカー完了後の確認に時間を要する。
- `tasks/sam2_tracker_COPG225_500_batch16_rerun_20251120.json` はチャンク単位 duration / dataset fetch metrics を含むため、`jq '.[0].avg_track_per_frame_s'` などで回帰比較が可能。

### 2025-11-20 XXXX225-02 再々計測 (500 frames, Job 7 / track 36, batch16 preload)
- `PATCH /api/jobs/7/annotations?action=create` に `tmp/COPG_task8_track.json` を流して Track 36 を作成し、下記コマンドで再実行。結果は `tasks/sam2_tracker_COPG225_500_batch16_20251120_remeasure.json`、ログは `logs/sam2_tracker/sam2_tracker_run_662bf587-e585-4132-a894-8d724e63afef.log` に保存。
  ```bash
  PYTHONPATH=cvat-cli/src:cvat-sdk UV_HTTP_TIMEOUT=120 \
    uv run python scripts/sam2/benchmark_tracker.py \
      --server http://192.168.10.190:8080 \
      --host-header 192.168.10.190 \
      --username admin --password admin \
      --job 7 --function 3 --track 36 \
      --start-frame 0 --target-frame 499 \
      --batch-sizes 16 \
      --tracker-preload-chunks --include-fetch-metrics \
      --agent-log-dir logs/sam2_tracker \
      --output tasks/sam2_tracker_COPG225_500_batch16_20251120_remeasure.json
  ```
  Run ID `662bf587-e585-4132-a894-8d724e63afef` / Function 3 (`SAM2 Tracker`)。

| Phase | Measurement (wall clock) | Notes |
| --- | --- | --- |
| Annotation GET (`GET /api/jobs/7/annotations`) | **1.113 s** | 5 本の既存トラック + 今回の track36 を含む 500frame payload。 |
| Tracker submission (`POST /tracker-actions`, batch16) | **0.126 s** | `submit_latency`。 |
| `init_tracking` AR | **0.53 s** | `init_duration_s`。 |
| `track` AR (31×16 + 1×3 frames) | **26.36 s total** (avg **52.8 ms/frame**, max 76 ms on chunk0, min 47 ms on最終チャンク付近) | `total_track_duration_s` / `avg_track_per_frame_s`。 |
| Run wall clock (init start → last track AR done) | **27.12 s** | `_apply_tracking_results`+status 更新 ≈0.23 s。 |
| Run status GET (post-completion) | **0.102 s** | `curl -w '%{time_total}' /api/functions/runs/662bf587-...`。 |
| Dataset fetch (`SAM2_TRACKER_LOG`) | **avg 4.15 ms/frame**, `hit_ratio = 1.0`, `cache_hits = 499`, `cache_misses = 0` | `frame_loader=\"_load_frame_image_from_lazy_chunk_cache\"`、`chunk_preload_enabled:true` で HTTP 取得は初回のみ。 |

- init+track 26.89 s / wall 27.12 s なので **99% が SAM2 推論**。chunk0 は 1.22 s（torch.compile + warmup）だが、chunk1 以降は 0.76–0.92 s/chunk（≈48–58 ms/frame）で安定。I/O は cache hit により無視できるレベルとなり、残るボトルネックは GPU `track_step`（memory conditioning）と初回 warmup。
- Annotation の再取得は 1.1 s まで増加（トラック本数増加による payload 膨張）。結果確認のたびに 1 s 超の待ちが発生する点は未解消の UX リスク。
- `SAM2_TRACKER_LOG` をパースして phase 別に集計（run 662bf587...）。Warmup（frame 0–15）では `track_step` 平均 **23.2 ms** / p95 32.7 ms、`sam_prompt_encoder` **1.90 ms** / p95 5.21 ms、`sam_mask_decoder` **3.44 ms** / p95 9.49 ms、`memory_conditioning` **14.6 ms** / p95 17.8 ms。Steady（frame 16–499）では `track_step` **15.5 ms** / p95 16.9 ms、`prompt_encoder` **0.39 ms** / p95 0.48 ms、`mask_decoder` **0.78 ms** / p95 0.86 ms、`memory_conditioning` **11.3 ms** / p95 12.2 ms、`memory_attention` **10.1 ms** / p95 10.7 ms。dataset fetch は chunk0 平均 **4.38 ms** / p95 5.21 ms、以降 **4.13 ms** / p95 4.92 ms と一定で cache hit が効いている。
- 非同期 vs 同期の差分（wall_ms - gpu_ms）をログから算出。`preprocess` は平均 **0.21 ms** / p95 0.26 ms（overhead ≈1.4%）、`track_step` は平均 **0.11 ms** / p95 0.19 ms（overhead ≈0.7%）で、GPUイベント計測と壁時計の乖離は極小。オーバーラップが無い（decode→track_stepを逐次同期）ことが確認でき、現状の長さは純粋な処理時間に起因している。
- ログ集計用スクリプト `scripts/sam2/analyze_tracker_log.py` を追加。`uv run python scripts/sam2/analyze_tracker_log.py logs/sam2_tracker/sam2_tracker_run_<run>.log` で phase ごとの `count/mean/p50/p95/max` を表示し、GPU計測 (`gpu_ms`) があれば併記する。chunk0 とそれ以外の dataset fetch を分けて確認できる。
- 2025-11-20 warmup テスト（track 37, batch16 preload, `SAM2_TRACKER_FAST_PREPROCESS=1`, `SAM2_TRACKER_WARMUP_FRAMES=1`）：run `f6bd5edb-b6c6-45b1-9846-8034d351ac16`（ログ: `logs/sam2_tracker/sam2_tracker_run_f6bd5edb-b6c6-45b1-9846-8034d351ac16.log`, JSON: `tasks/sam2_tracker_COPG225_500_batch16_warmup_test.json`）。`init=0.456 s`、`track=25.80 s`（avg **51.7 ms/frame**）、`wall=26.49 s`、chunk0 0.93 s (58 ms/f)。`dataset_fetch.avg_fetch_ms=3.99`, `hit_ratio=1.0`, `cache_misses=0`。ただし `warmup_*` ログは出力されず、起動時 burn-in フレーム数の可視性は今後のタスク。
- 2025-11-20 warmup テスト2（track 37, batch16 preload, 同設定。run `784083d7-75c1-4a14-b4ff-4408833b9bcc`, ログ: `logs/sam2_tracker/sam2_tracker_run_784083d7-75c1-4a14-b4ff-4408833b9bcc.log`, JSON: `tasks/sam2_tracker_COPG225_500_batch16_warmup_test2.json`）。`init=18.11 s`（torch.compile が初回に集中）、`track=26.11 s`（avg **52.3 ms/frame**）、`wall=44.47 s`。`dataset_fetch.avg_fetch_ms=5.04`, `hit_ratio=0.974`（cache miss 13）。`warmup` ログは依然未出力で、起動時 burn-in の可視化は未解決。
- 2025-11-20 warmup_off 再計測（`SAM2_TRACKER_WARMUP_FRAMES=0`, track37, batch16 preload）。run `a697254c-eb31-4197-9409-f1f3e630209f`（JSON: `tasks/sam2_tracker_COPG225_500_batch16_warmup_off.json`、ログ: `logs/sam2_tracker/sam2_tracker_run_a697254c-eb31-4197-9409-f1f3e630209f.log`）。`init=107.26 s`（torch.compile + autotune が init に集中）、`track=63.07 s`（avg **126.4 ms/frame**）、`wall=170.58 s`。chunk0 が 37.3 s と大幅劣化し、`avg_fetch_ms=5.24`, `hit_ratio=0.974`。warmup 無しのままでは init が壊滅的に遅く、バックグラウンド compile 問題の切り分けが必要。

### 2025-11-22 ダブルバッファ + compile 切り分けメモ
- `cpu_preprocess_image` → `forward_preprocessed_tensor` を追加し、H2D を eager（non-compile）にした上でダブルバッファ経路を導入。
- non-vos (torch.compile OFF) + double buffer では job1/20f 成功 (run `9cba8a4a-2737-4336-abb5-42a3a82cc099`, init 1.36 s / track 0.89 s / wall 2.26 s)。
- vos_optimized ON + cudagraph OFF でダブルバッファを試すと、初回コンパイルが init ≈190 s / chunk0 ≈45 s まで膨張（run `b2934559-cb58-4a55-afcf-6f586c13c8cc`）。H2D を eager にしても compile が新グラフを丸ごとオートチューンするためコンパイル時間は減らず。
- cudagraph OFF + H2D eager でも run `4698676e-3f14-4491-992d-842f46dad9eb` は torch._dynamo がコンテキストで無効化できず RuntimeError で失敗。コンパイルを残したままダブルバッファを安定させるには、compile 対象を `track_step` に限定するか autotune を切るなどの追加設計が必要。
- 500f 測定は未実行。安全運用は compile OFF (`SAM2_TRACKER_VOS_OPTIMIZED=0`) でダブルバッファ無し/有りを比較する形が現実的。
- 2025-11-20 cudagraph off 実験（`TORCHINDUCTOR_USE_CUDAGRAPHS=0` 相当、warmup=0, track37, batch16 preload）。run `3f98c8eb-5a38-49c5-b1a3-78a32a2fd9e5`（JSON: `tasks/sam2_tracker_COPG225_500_batch16_cudagraph_off.json`, ログ: `logs/sam2_tracker/sam2_tracker_run_3f98c8eb-5a38-49c5-b1a3-78a32a2fd9e5.log`）。`init=146.09 s`, `track=65.98 s`（avg **132.2 ms/frame**）, `wall=212.31 s`、chunk0 41.1 s。cudagraph 無効でも compile/autotune が初回 AR に集中し、むしろ悪化。
- 2025-11-20 cudagraph off 再実験（env 明示、warmup=0）。run `ef959c8b-6fda-4689-8d4e-e10d5c107b77`（JSON: `tasks/sam2_tracker_COPG225_500_batch16_cudagraph_off2.json`, ログ: `logs/sam2_tracker/sam2_tracker_run_ef959c8b-6fda-4689-8d4e-e10d5c107b77.log`）。`init=145.58 s`, `track=68.24 s`（avg **136.8 ms/frame**）, `wall=214.06 s`, chunk0 43.1 s。`avg_fetch_ms=4.81`, `hit_ratio=0.974`。cudagraph 無効化でも初回 AR への compile 集中は解消せず、スパイク継続。
- 2025-11-20 warmup デフォルト=1 での回帰（compose に `SAM2_TRACKER_WARMUP_FRAMES=1` を新設して agent をリビルド後、track38）。run `03656af6-55d3-4442-a7c7-8882fe9f3a1b`（JSON: `tasks/sam2_tracker_COPG225_500_batch16_warmup_default1.json`, ログ: `logs/sam2_tracker/sam2_tracker_run_03656af6-55d3-4442-a7c7-8882fe9f3a1b.log`）。`init=45.47 s`（先頭の `phase=\"preprocess\"` が **44.4 s** までスパイク）、`track=25.45 s`（avg **51.0 ms/frame**）、`wall=71.15 s`。`avg_fetch_ms=5.22`, `hit_ratio=0.974`（cache miss 13）。warmup 1 フレームだけでは TorchInductor の compile が init で再発し、前処理がボトルネックのまま。
- 2025-11-20 warmup 中の preprocess を `torch.compiler.disable()` で無効化する実験（track40, batch16 preload）。run `42535804-55b4-4fb0-8f47-423ac13c6a9a`（JSON: `tasks/sam2_tracker_COPG225_500_batch16_warmup_disable_preprocess.json`, ログ: `logs/sam2_tracker/sam2_tracker_run_42535804-55b4-4fb0-8f47-423ac13c6a9a.log`）。`init=136.72 s`, `track=93.64 s`（avg **187.6 ms/frame**）, `wall=230.61 s`。chunk0 が **43.19 s**、`avg_fetch_ms=3.89`, `hit_ratio=1.0`。ログでは warmup 区間の `preprocess` max **85.5 s** / `track_step` max **58.0 s** と、モデル compile が依然 warmup 内に集中しており、preprocess だけ compiler disable してもスパイクは解消しなかった。
- 2025-11-20 warmup 時のみ TorchInductor の `max_autotune=False` に落とす実験（track41, batch16 preload）。run `85f7eff9-6b7d-4e84-8776-9ddeaa6232db`（JSON: `tasks/sam2_tracker_COPG225_500_batch16_warmup_autotune_off.json`, ログ: `logs/sam2_tracker/sam2_tracker_run_85f7eff9-6b7d-4e84-8776-9ddeaa6232db.log`）。`init=222.78 s`, `track=25.28 s`（avg **50.7 ms/frame**）, `wall=248.30 s`。warmup 区間の `preprocess` max **85.5 s** / `sam_mask_decoder` max **28.5 s** / `memory_encode_prepare` max **20.1 s** と compile スパイクが依然 warmup 内に集中し、autotune 無効化でも初回コンパイル時間はむしろ悪化。steady 状態の `track_step mean=13.9 ms` は従来と同等。

### 2025-11-20 async preprocess 実験（SAM2_TRACKER_ASYNC_PREPROCESS=1）
- エージェントを compose 再生成し、async preprocess を有効化したうえで再計測。
- run1（`375e194e-ae27-484b-8ad1-c9b12691d794`, ファイル: `tasks/sam2_tracker_COPG225_500_batch16_async.json`）では torch.compile が init に集中し `init=25.35 s`, `track=56.32 s`（avg 112.9 ms/frame）, wall 81.90 s。warmup 時に巨大スパイク（memory_attention 26 s級）が発生。
- run2（`b6eb797d-55d9-48d1-b2fe-6fe270cc0586`, ファイル: `tasks/sam2_tracker_COPG225_500_batch16_async_repeat.json`）では `init=0.73 s`, `track=24.77 s`（avg 49.6 ms/frame）, wall 25.73 s と通常水準。preprocess `gpu_ms` ≈8.0 ms / `wall_ms` ≈15.0 ms でオーバーヘッドは ~0.2 ms。
- async 有効でも steady 状態の per-frame は従来とほぼ同等（~46–50 ms/frame）。前処理の分離ストリームによるオーバーラップ効果は観測できず、主因は処理そのもの。初回ウォームアップで巨大スパイクが出るリスクがあるため、実運用に入れるには burn-in を agent 起動時に明示的に実行する必要がある。
- 計測オフ検証（`SAM2_TRACKER_VERBOSE=0` で実行, run `6b2d7541-394d-48ef-876b-d0e1b53c3fce`, `tasks/sam2_tracker_COPG225_500_batch16_async_nolog.json`）では chunk0 が **37.8 s** に跳ね、wall 169 s / track 59.9 s と数倍劣化。原因は torch.compile + 前処理/track_step が一括で最初の chunk に載ったためで、計測オフ自体に高速化効果は無い。ウォームアップ有無が支配するので統計を残したまま burn-in を行う方が安全。

### 2025-11-21 XXXX225-02 再計測 (500 frames, Job 7 / track 42, batch16 preload)
- `tmp/COPG_task8_track.json` を `PATCH /api/jobs/7/annotations?action=create` で投入し Track 42 を生成。下記コマンドで 500f を再計測。結果は `tasks/sam2_tracker_COPG225_500_batch16_20251121.json`、ログは `logs/sam2_tracker/sam2_tracker_run_e6f67748-ebe9-4001-90a5-84d8159b3fbd.log` に保存。
  ```bash
  PYTHONPATH=cvat-cli/src:cvat-sdk UV_HTTP_TIMEOUT=120 \
    uv run python scripts/sam2/benchmark_tracker.py \
      --server http://192.168.10.190:8080 \
      --host-header 192.168.10.190 \
      --username admin --password admin \
      --job 7 --function 3 --track 42 \
      --start-frame 0 --target-frame 499 \
      --batch-sizes 16 \
      --tracker-preload-chunks --include-fetch-metrics \
      --agent-log-dir logs/sam2_tracker \
      --output tasks/sam2_tracker_COPG225_500_batch16_20251121.json
  ```
  Run ID `e6f67748-ebe9-4001-90a5-84d8159b3fbd` / Function 3 (`SAM2 Tracker`)。

| Phase | Measurement (wall clock) | Notes |
| --- | --- | --- |
| Annotation GET (`GET /api/jobs/7/annotations`) | **2.10 s** | 12 本のトラックを含む 500f payload（`logs/http/job7_annotations_get_20251121.txt`）。 |
| Tracker submission (`POST /tracker-actions`, batch16) | **0.137 s** | `submit_latency`。 |
| `init_tracking` AR | **13.22 s** | `init_duration_s`。`SAM2_TRACKER_LOG` の `preprocess` max **11.9 s** / `track_step` max **0.54 s** が warmup chunk に集中。 |
| `track` AR (31×16 + 1×3 frames) | **28.28 s total** (avg **56.7 ms/frame**) | `total_track_duration_s` / `avg_track_per_frame_s`。steady の `track_step` p95 ≈14.9 ms。 |
| Run wall clock (init start → last track AR done) | **41.76 s** | `_apply_tracking_results` は ≈0.23 s で僅少。 |
| Run status GET (post-completion) | **0.102 s** | `logs/http/run_e6f67748_get_20251121.txt`。 |

- `dataset_fetch.avg_fetch_ms = 19.5 ms`, `hit_ratio = 0.974`（`cache_hits = 486`, `cache_misses = 13`）。chunk0/outlier で **0.45–0.49 s** の fetch が散発し、ノイズが加算されている。`chunk_preload_enabled:true` だが zip chunk の一部が未キャッシュのまま触れられている可能性。
- 全体の 41.8 s のうち **≈32% が init**（torch.compile/fast-preprocess ウォームアップ）、**≈68% が track**（GPU `track_step`）。steady 状態では `track_step mean ≈13.5 ms` / `memory_attention ≈9 ms` と 11/20 時点と同程度だが、torch.compile が `init_tracking` 内に再発して大幅に悪化。
- Annotation 取得は 2.1 s まで劣化（トラック本数増加 + 500f payload4.7 MB級）。トラッカー完了後の確認が UX 上の待ち時間となる点は継続課題。

### 2025-11-21 chunkプリロード同期・再実験 (500 frames, Job 7 / track 42, batch16 preload)
- `SAM2_TRACKER_PREFETCH_PARALLEL` を導入し、`batch_size*2` 本のスレッドで chunk を同期プリフェッチしてから track を開始するよう `cvat-cli/_internal/agent.py` を更新。`prefetch` start/done を `SAM2_TRACKER_LOG` に記録。
- 新イメージで `docker compose ... up -d --build sam2-tracker-agent` 後、同一 track42 を再実行。
  - Run `0d010ac6-9320-4971-9b8d-2ef06c79c425`, JSON: `tasks/sam2_tracker_COPG225_500_batch16_20251121_prefetch.json`, ログ: `logs/sam2_tracker/sam2_tracker_run_0d010ac6-9320-4971-9b8d-2ef06c79c425.log`。
  - `dataset_fetch`: **avg 8.20 ms**, `hit_ratio = 1.0`, `cache_misses = 0`, `max 73.9 ms`（chunk0のみ）。prefetch event 13本（chunk数）。
  - `track`: **46.95 s total**（avg **94.1 ms/frame**）、`init 94.03 s`（torch.compile が \"warmup\" フェーズで暴発して `track_step` max 57.3 s）。wall **141.23 s**。steady 区間は `track_step mean ≈14.0 ms` / `p95 15.2 ms` と通常値。
- 直後のホット実行（re-run without rebuild）では miss=0 かつ init が 0.77 s / track 23.39 s (46.9 ms/f) まで回復することを別 Run `9693da5f-...` で確認。cold start 時は torch.compile が `init_tracking` に集中するため、プリフェッチ同期だけでは init スパイクを抑えきれない。起動時ウォームアップ or 事前コンパイルの恒久対策が必要。

### 2025-11-22 XXXX225-02 再計測 (500 frames, Job 7 / track 43, batch16 preload)
- `tmp/COPG_task8_track.json` を `PATCH /api/jobs/7/annotations?action=create` で投入し Track 43 を作成したうえで再実行。結果は `tasks/sam2_tracker_COPG225_500_batch16_20251122.json`、ログは `logs/sam2_tracker/sam2_tracker_run_b4b62b7a-e158-4e07-be14-d55e9d441252.log` に保存。
  ```bash
  PYTHONPATH=cvat-cli/src:cvat-sdk UV_HTTP_TIMEOUT=120 \
    uv run python scripts/sam2/benchmark_tracker.py \
      --server http://localhost:8080 \
      --host-header 192.168.10.190 \
      --username admin --password admin \
      --job 7 --function 3 --track 43 \
      --start-frame 0 --target-frame 499 \
      --batch-sizes 16 \
      --tracker-preload-chunks --include-fetch-metrics \
      --compose-cmd "docker compose -f docker-compose.yml -f docker-compose.dev.yml" \
      --agent-log-dir logs/sam2_tracker \
      --output tasks/sam2_tracker_COPG225_500_batch16_20251122.json
  ```
  Run ID `b4b62b7a-e158-4e07-be14-d55e9d441252` / Function 3 (`SAM2 Tracker`)。

| Phase | Measurement (wall clock) | Notes |
| --- | --- | --- |
| Annotation GET (`GET /api/jobs/7/annotations`) | **2.214 s** | `logs/http/job7_annotations_get_20251122.txt`。13 本のトラックを含む 500f payload。 |
| Tracker submission (`POST /tracker-actions`, batch16) | **0.130 s** | `submit_latency`。 |
| `init_tracking` AR | **0.648 s** | torch.compile/warmup は僅少。 |
| `track` AR (31×16 + 1×3 frames) | **21.57 s total** (avg **43.2 ms/frame**, chunk0 **1.04 s** / 64.8 ms/f, 以降 0.65–0.70 s/chunk) | `total_track_duration_s` / `avg_track_per_frame_s`。 |
| Run wall clock (init start → last track AR done) | **22.47 s** | `_apply_tracking_results` + status 更新 ≈0.24 s。 |
| Run status GET (post-completion) | **0.103 s** | `logs/http/run_b4b62b7a_get_20251122.txt`。 |

- `dataset_fetch.avg_fetch_ms = 8.38 ms`, `hit_ratio = 1.0`（`cache_hits = 499`, `cache_misses = 0`）。chunk0 平均 **9.26 ms** / p95 11.7 ms、以降 **8.31 ms** / p95 9.65 ms とプリロードが安定しており I/O は支配的でない。
- ログ集計（`analyze_tracker_log.py`）: `track_step mean=14.5 ms` / p95 15.9 ms、`preprocess wall mean=11.9 ms`（GPU 4.9 ms → decode/resize 分が大半）、`memory_conditioning mean=10.5 ms`（`memory_attention mean=9.3 ms`）。warmup chunk は 1.04 s で収まり、残り 31 chunk は 0.66–0.69 s。`mask_to_shape mean=0.88 ms` と周辺処理は軽微。
- ボトルネックは依然 **GPU 推論（memory conditioning + preprocess decode）** で、wall 22.5 s の **≈96% が init+track**。HTTP/DB オーバーヘッドは 0.1 s 程度に抑えられているが、500f annotation 再取得は 2.21 s まで膨らんでおり UX 上の待ち時間は残存。

#### 2025-11-20 XXXX225-02 encoder/decoder breakdown（Job 7 / track 35, Run `e7e89e8f-3607-49e2-bee7-4d6cfed50df6`）
- `SAM2_TRACKER_VERBOSE=1 SAM2_TRACKER_VOS_OPTIMIZED=1 SAM2_TRACKER_FAST_PREPROCESS=1 SAM2_TRACKER_EXTRA_AGENT_ARGS="--tracker-preload-chunks --include-fetch-metrics"` を付けて agent を `docker compose --profile sam2-agent restart sam2-tracker-agent` で再起動し、再度 `scripts/sam2/benchmark_tracker.py` を実行。結果は `tasks/sam2_tracker_COPG225_500_batch16_encoder_breakdown_20251120.json`、`SAM2_TRACKER_LOG` は `logs/sam2_tracker/sam2_tracker_run_e7e89e8f-3607-49e2-bee7-4d6cfed50df6.log` に保存した。
- chunk #0 が **17.81 s**（Torch compile + fast preprocess 初期化 + SAM2 cross-attention warmup）で支配的。以降の 31 chunk は平均 **0.796 s/chunk (≈51 ms/frame)** となり、24 s 計測時の 46 ms/frame より僅かに遅いが再現性の範囲内。
- `dataset_fetch.avg_fetch_ms = 5.04 ms`, `hit_ratio = 0.974`（cache miss 13 件）。`MediaDownloadPolicy.PREFETCH_CHUNKS_ONCE` で chunk 0 を丸ごと VRAM に入れ、残り 486 frame はローカルヒット。`cache_bytes_peak ≈7.6 MB` で headroom は十分。

| Phase | Warm-up (ms) | Steady avg (ms/frame) | 備考 |
| --- | --- | --- | --- |
| Preprocess (`fast_preprocess`) | 10,813 | **14.7** | GPU decode + resize。warmup 後は 14–17 ms で安定。 |
| `track_step`（SAM2 全体） | 8,725 / 16,999 | **14.8** | chunk #0 の 16 frame が 17.8 s。以降は 14–17 ms で推移。 |
| Memory conditioning | 11,078 | **10.8** (**73%** of `track_step`) | `_prepare_memory_conditioned_features`。このうち `memory_attention` が 9.5 ms (64.5%) を占める。 |
| SAM prompt encoder | 1,712 / 345 | **0.38** (**2.6%**) | warmup 2 回で 2片とも torch.compile。以降は 0.3–0.4 ms。 |
| SAM mask decoder | 4,023 / 5,557 | **0.79** (**5.4%**) | compile 後は 0.6–2 ms。 |
| Memory encode prepare | 2,959 / 4.5 | **0.99** (**6.7%**) | High-res mask 後処理。`memory_encoder` 本体は 0.56 ms (3.8%)。 |
| Mask → shape | 4.28 | **0.85** | 383/500 frame で polygon を返却（outside frame は `None` なのでカウント外）。 |

- `track_step` の中身は **memory conditioning → attention (約 65%)** が支配的で、prompt/mask/encoder は数 % 台。steady 状態では `preprocess14.7 ms + track_step14.8 ms ≈ 29.5 ms` が純粋な推論時間で、残り ~20 ms/frame は Python ループ・`mask_to_shape`・結果書込み等の周辺処理が占める。
- warm-up 2 frame を含む chunk #0 を別スレッドへ逃がさない限り、500 frame inference の先頭 **17.8 s** が UX を支配する。`torch.compile` のキャッシュ共有や agent 常駐（コンテナ再起動を避ける）が必須。
- `SAM2_TRACKER_LOG` の `phase="memory_*"` / `sam_*` / `track_step` / `preprocess` が出力されるようになったため、`jq 'select(.phase==\"memory_attention\") | .wall_ms'` 等で GPU 内部の配分をそのまま確認できる。

### Tracker logger instrumentation (2025-11-19)
- `cvat-cli/_internal/agent.py` に `function_run_id` ベースのコンテキストを追加。`frame_fetch` / `dataset_fetch` / `dataset_cache` / `track_chunk` の各イベントは `{"phase": "...", "function_run_id": ..., "ar_id": ...}` を含むため、1 ラン内の AR と GPU ログ (`track_step`) を容易に突合できる。
- `dataset_fetch` イベントは `cached`（ロード直前のチャンク状態）と `cached_after`（読込直後）を出力し、`download_ms` / `decode_ms` をヒューリスティックに按分する。`cached=false` の場合は全時間を download とみなし、`cached=true` は decode 側に積む。新規チャンクを取得したタイミングでは `phase="dataset_cache"` / `event="put"` を発行し、zip サイズ（`cost_bytes`）で I/O 量を推定できる。
- `ai-models/tracker/sam2/func.py` 側では `shape_to_mask` / `mask_to_shape` の CPU 処理を `SAM2_TRACKER_LOG` で分離。Polygon 変換が支配的なトラックでも GPU `track_step` との比率が可視化できるため、今後の最適化ターゲットを明示できる。
- `SAM2_TRACKER_VERBOSE=1` を `sam2-tracker-agent` コンテナ環境に設定し、`--include-fetch-metrics` 併用で `scripts/sam2/benchmark_tracker.py` が `avg_fetch_ms` / `hit_ratio` を埋められるようにしておく（`logs/sam2_tracker/sam2_tracker_run_<run>.log` に生ログが残る）。

### 2025-11-19 XXXX225-02 verbose run（Job 7 / track 14, Run `22228f0d-dc59-436d-9c69-a79fa9ffc391`）
- `SAM2_TRACKER_VERBOSE=1 docker compose --profile sam2-agent up -d sam2-tracker-agent` で agent を再起動し、`PUT /api/jobs/7/annotations` で Track 14 を生成し直したうえで `scripts/sam2/benchmark_tracker.py --tracker-preload-chunks --include-fetch-metrics` を再実行。計測結果は `tasks/sam2_tracker_COPG225_500_batch16_verbose.json`、ログは `logs/sam2_tracker/sam2_tracker_run_22228f0d-dc59-436d-9c69-a79fa9ffc391.log`。
- 指標:

| Phase | Measurement (wall clock) | Notes |
| --- | --- | --- |
| Annotation PUT / GET | 0.171 s / 0.129 s | unchanged |
| Tracker submission | 0.123 s | `submit_latency` |
| `init_tracking` AR | 1.05 s | warmup |
| `track` AR | **57.57 s total** (avg **115 ms/frame**, min 100 ms, max 132 ms) | 31×16 + 1×3 frames |
| Run wall clock | **58.91 s** | `wall_clock_s` |
| Run status GET | 0.102 s | `logs/http/run_22228f0d-..._get_20251119.txt` |
| Dataset fetch (`SAM2_TRACKER_LOG`) | **avg 36.8 ms/frame**, `hit_ratio=0.0` | `frame_loader="_load_frame_image_from_server"` |

- `dataset_fetch` ログ例（frame 0〜2）: `{"chunk_id":0,"cached":false,"download_ms":78.394,"decode_ms":0.0}` など、全フレームで `cached=false` が続いており chunk cache は未使用。compose 側で `SAM2_TRACKER_EXTRA_AGENT_ARGS="--tracker-preload-chunks --include-fetch-metrics"` を設定しない限り、imageset でも毎フレーム CVAT サーバーから JPEG を直接取得して約 35–40 ms を費やす。GPU inference は 500 枚で 57 s と既存 run と同等だが、I/O を短縮しない限り 60 s 近辺の壁は突破できないことが再確認できた。

### 2025-11-19 XXXX225-02 preload run（Job 7 / track 15, Run `0d95dd17-f399-41ba-9e87-05ecfcec758f`）
- `.env` に `SAM2_TRACKER_EXTRA_AGENT_ARGS="--tracker-preload-chunks --include-fetch-metrics"` を追加し、`SAM2_TRACKER_VERBOSE=1 docker compose --profile sam2-agent up -d sam2-tracker-agent` で再起動。Track 15 を投入したあと再度 `scripts/sam2/benchmark_tracker.py` を実行し、結果を `tasks/sam2_tracker_COPG225_500_batch16_preload.json` に保存。
- 指標:

| Phase | Measurement (wall clock) | Notes |
| --- | --- | --- |
| `track` AR | **29.21 s total** (avg **58 ms/frame**) | 2×高速化 |
| Run wall clock | **30.56 s** | init 1.01 s + track 29.21 s |
| Dataset fetch | **avg 10.18 ms/frame**, `hit_ratio=0.974`, `cache_hits=486` | chunk cache有効 (`frame_loader="_load_frame_image_from_lazy_chunk_cache"`) |
| Cache telemetry | `cache_bytes_peak=7.3 MB`, `event="dataset_cache", cost_bytes≈250 KB` | zip サイズの積み上げで確認 |

- ログ例: `SAM2_TRACKER_LOG {"phase":"dataset_cache","event":"put","chunk_id":12,"cost_bytes":257218,"function_run_id":"0d95dd17-...","ar_id":"0412..."}`
  により chunk が初回取得時のみ書き込まれていることが分かり、その後の `dataset_fetch` は `cached=true`, `download_ms≈0`, `decode_ms≈4 ms` に収束した。I/O ボトルネック解消により SAM2 `track_step` だけのコスト（≈50–70 ms/frame）に近づき、500 フレーム run が ~30 s まで短縮された。

### 2025-11-19 XXXX225-02 preload + `vos_optimized` 試験
- 2025-11-19: `SAM2_TRACKER_VOS_OPTIMIZED=1` を `.env` に追加し、`ai-models/tracker/sam2/func.py` 側で `vos_optimized=True` を SAM2 本体へ伝播。PyTorch の `torch.compile` は CUDAGraphs 前提のため、`torch._inductor.config.use_cuda_graphs = False` / `config.triton.cudagraphs = False` / `torch.compiler.cudagraph_mark_step_begin()` を差し込んだ。
- 2025-11-19: それでも `RuntimeError: accessing tensor output of CUDAGraphs that has been overwritten by a subsequent run`（`memory_attention.py`→`mask_decoder` 内）で AR 処理が失敗し、Run `66ee48ed-0714-402b-9f34-dc0b363f54af` は chunk 1 回目で停止。失敗ログ: `logs/sam2_tracker/sam2_tracker_run_66ee48ed-0714-402b-9f34-dc0b363f54af.log`.
- 2025-11-21: `SAM2VideoPredictorVOS` を継承した CVAT 専用クラスを追加し、`memory_attention.forward` の compile モードを [issue #501 comment](https://github.com/facebookresearch/sam2/issues/501#issuecomment-3540254359) に倣って `max-autotune-no-cudagraphs` へ差し替え。XXXX225-02 (Job7/Track21) で再計測した結果、Run `0a251a4a-26ed-4ecc-baa3-a29340a14069` が完走。計測 JSON: `tasks/sam2_tracker_COPG225_500_batch16_preload_vos_patch.json`。
  - wall clock **22.70 s** (init **0.48 s** + track **21.99 s**, avg **44 ms/frame**)。プリロード ON かつ torch.compile 有効で、従来の preload run (30.56 s) 対比 **1.35×** 高速化。chunk #0 だけ 1.06 s かかり、以降は 0.65 s → 0.17 s に収束（torch.compile の warmup + autotune 一回分）。
  - dataset fetch: `avg_fetch_ms=3.70`, `hit_ratio=1.0`, `cache_hits=499`（`frame_loader="_load_frame_image_from_lazy_chunk_cache"`）。`SAM2_TRACKER_LOG` で欠損無し、`agent_log_path=logs/sam2_tracker/sam2_tracker_run_0a251a4a-26ed-4ecc-baa3-a29340a14069.log`.
- 現状の PyTorch 2.9.1 + TorchInductor では今のところ安定動作を確認したが、Meta release note が推奨する **2.5.1 系** 以外では再現性保証がない。`vos_optimized` を常用する際は今回の `memory_attention` パッチを維持しつつ、新しい torch.compile バージョンごとに smoke test を回す必要がある。

### 2025-11-21 コンポーネント別プロファイル（実験コード）
- `ai-models/tracker/sam2/experiments/profile_components.py` を追加。`_Sam2Tracker` を直接呼び出し、フレームディレクトリ（デフォルト: `experiments/data/`）を順次処理しながら `preprocess` / `track_step` と SAM2 各モジュール（vision backbone / memory_encoder / memory_attention / prompt_encoder / mask_decoder）の GPU 時間を計測する。  
  実行例（コンテナ内）:
  ```bash
  docker compose exec sam2-tracker-agent \
    uv run python /workspace/src/ai-models/tracker/sam2/experiments/profile_components.py \
      --frames-dir /workspace/src/ai-models/tracker/sam2/experiments/data/XXXX225-02_frames_500 \
      --limit 500 --model-id facebook/sam2.1-hiera-small --device cuda
  ```
  - TorchInductor + torch.compile 版では forward をフックした瞬間に再び `CUDAGraphs` エラーが発生したため、実験コードでは `vos_optimized=False`（非 compile）で計測。編成的には encoder/decoder の相対比（どちらが支配的か）を把握する目的なので、絶対値は compiled 版より大きいものの内訳はそのまま利用できる。
  - 出力サンプル（`ai-models/tracker/sam2/experiments/data/profile_components_eager.txt` に保存）:

    | 区分 | Avg ms/frame | Track内比率 |
    | --- | --- | --- |
    | `preprocess_total` | **22.30 ms** | – |
    | └ `vision_backbone` | **10.10 ms** | –（preprocessの45%程度） |
    | `track_step_total` | **15.12 ms** | 100 % |
    | └ `memory_attention` | **9.27 ms** | **61 %** |
    | └ `mask_decoder` | **2.52 ms** | **17 %** |
    | └ `memory_encoder` | **1.39 ms** | **9 %** |
    | └ `prompt_encoder` | **0.59 ms** | **4 %** |
    | └ その他（post-proc 等） | **1.35 ms** | **9 %** |

  - 500 フレーム換算の合計は `preprocess_total 11.15 s` + `track_step_total 7.54 s` ≒ **18.7 s**。compile 版の 22.7 s と比較すると絶対値は異なるが、`memory_attention` が `track_step` の 6 割を占めるという傾向は一致。`mask_decoder` は ~17 % 程度で、さらなる高速化の余地は encoder/attention 側にあることが確認できた。
  - `--vos-optimized` で本計測を回す場合は `torch._inductor.config.use_cuda_graphs=False` を内部で明示的にセットする必要があるが、それでも `sam_mask_decoder` をラップした時点で CUDAGraphs エラーが発生したため、ひとまず「eager モードでの内訳」という位置付けで運用する。
- `vos_optimized` 有効時の Autotune/LT warmup 課題:
  - Run `3e6082ea-1e1b-4830-96f2-ea8f607d330e`（fast preprocess ON + `vos_optimized=1`）では chunk #0 が **1.65 s**, chunk #1 も **1.57 s** を要し、その後の chunk は 0.8–0.9 s へ収束。TorchInductor の `max-autotune` が毎コンポーネントでフル実行されており、初期 1–2 chunk がウォームアップ扱いになっている。
  - 実ジョブでは「本番 chunk に入る前にウォームアップを1度だけ済ませる」仕組みがないため、500 フレーム run でも **45 s** 台まで遅延。Autotune を温存したい場合は (1) tracker 起動後にテストフレームで1回だけ run を回す、(2) `torch._inductor.select_algorithm.compile_cache_dir` を agent 毎に継続利用する、(3) `--max-autotune` 以外の mode（`reduce-overhead` 等）を検討するタスクが必要。
- 前処理のみを評価するため `--preprocess-only --profile-preprocess` オプションを追加し、`vos_optimized` ON/OFF のコスト比較を実行。結果ログは
  - 非 compile: `ai-models/tracker/sam2/experiments/data/profile_preprocess_only_eager.txt` ⇒ `preprocess_total=21.67 ms` / `vision_backbone=10.13 ms`.
  - compile: `ai-models/tracker/sam2/experiments/data/profile_preprocess_only_vos.txt` ⇒ 初回 warmup で平均が伸び `preprocess_total=34.67 ms` / `vision_backbone=22.18 ms`。P95（24.8 ms / 10.2 ms）を見ると steady-state は +15% 程度の増加に留まるが、torch.compile の初回 JIT (500 枚中 1–2 枚) が 100+ ms 発生し全体平均を押し上げる。今後の高速化評価では「warmup除外平均」を別途算出するか、torch.compile の `reduce-overhead` モードで初期 JIT を短縮する必要がある。
- `SAM2_TRACKER_FAST_PREPROCESS=1` で GPU サイド前処理（`torchvision.io.decode_image` → Resize → Normalize を CUDA 上で実施）を opt-in。`cvat-sdk` 側で `MediaElement.load_encoded_bytes()` を追加し、`TaskDataset` が ZIP/REST から取得した JPEG バイト列を `PIL.Image.info["_encoded_bytes"]` に埋め込むようにした。これにより agent は追加コピー無しで GPU decode を呼び出せる。
  - 評価（500 frames, eager モード、`--preprocess-only`）: `preprocess_total=21.67 ms` → **16.13 ms**（**25.6% 改善**）、`vision_backbone=10.13 ms` → **9.57 ms**（`profile_preprocess_only_fast.txt`）。差分 ≈5.5 ms/frame は CPU→GPU 転送と正規化の削減分に相当する。
  - `vos_optimized` 併用時は CUDA Graphs まわりでまだ安定していないため、現段階では eager 用の実験フラグ扱い。量産投入する場合は CUDA decode を安定させる（`torchvision.io.decode_image(..., device='cuda')` など）か、TensorRT 前処理に置き換える方向を検討する。

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

## Workstream Board (2025-11-20時点)
| Stream | Scope (詳細セクション) | 現状ステータス | 次アクション | 期待アウトカム |
| --- | --- | --- | --- | --- |
| Tracker batching | Experiment 1 / W47タスク群 | backend / agent / UI / CLI / docs を merge 済み。Job8/9 計測ログと ADR 転記も完了し、本流へ取り込み済。 | 定期的な再計測とリグレッション監視のみ（必要時）。 | Batch tracking を既定値で運用でき、before/after 指標と手順が共有された状態。 |
| Dataset streaming & caching | Experiment 2 | **完了**：ChunkCacheMode 実装 + CLI/agent 配線済み。 | Job30 preload ON/OFF の再計測結果を `tasks/sam2_tracker_dataset_fetch_20251121_summary.md` へ反映済み。 | ChunkCacheMode 有効時に `avg_fetch_ms≈1 ms` / `hit_ratio=1.0` を確認し、Experiment 2 の目的を達成。 |
| Run status refactor | Experiment 3 | FunctionRunStatus への移行 migration（0007+0008）と `backfill_run_status` CLI v2, Run status view/cancel の summary専用化を実装済み。 | 既存 run の backfill 実行 → `AnnotationRequest.run_status` を NOT NULL 化 → profiling SQL を取得して before/after を比較。 | Run status API の before/after 計測計画が承認可能な状態。 |
| `_apply_tracking_results` diff | Experiment 4 | DiffBuilder 設計中。apply パスの PoC なし。 | FrameDigest/ShapeDigest の比較仕様を固め、ユースケース別に必要クエリ数を算出。 | diff 適用ユニットテストの素案とベンチ計測手順を共有。 |
| Observability & regression | Experiment 5 | `SAM2_TRACKER_VERBOSE` ログや GPU smoke の枠組みが未完成。 | `cuda_event_pair` ログ仕様をまとめ、`tests/python/tracker/test_tracker_bottlenecks.py` のスケルトンを追加。 | bottleneck 測定用の共通 CLI/Test entry が reviewers に説明できる。 |

> メモ: 「Now」を上記5本に固定し、それ以外の発散アイデアは後段の各 Experiment セクション末尾にメモする方針に切り替えた。

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

4. **パイプライン化/非同期適用の導入**
   - Tracker agent 内で `MediaDownloadPolicy.PREFETCH_CHUNKS_ONCE` と `ChunkCacheMode` を併用し、GPU `track_step` を実行しながら次フレームの `load_image()` を別ワーカーで先行実行して I/O 待ちを隠蔽する。Experiment 2 の chunk cache 実装を基盤に、`track` AR の処理をフレームチャンク単位で非同期化する計測も追加する。
   - `_apply_tracking_results` を DiffBuilder でチャンクごとに差分適用できるようにし、最後の `track` 完了を待たずに UI へ部分的な変更を書き戻す。`SAM2_TRACKER_VERBOSE` に `phase="apply_diff"` を追加して apply の重畳を可視化する。
   - `FunctionRunStatusView` は Experiment 3 で設計中の summary テーブルへ書き込みを行い、AnnotationRequest 完了時にバックグラウンドで集計を進めておく。UI ポーリングは summary 参照のみとし、トラッキング実行とステータス集計を並列化する。

5. **結果適用の差分化**
   - `_apply_tracking_results` を「agent から返ったフレームだけ更新」「outside を付け足すだけのフレームは `UPDATE ... SET outside=true`」に分岐。
   - `handle_annotations_change` 呼び出しも create/update/delete それぞれ 1 回ずつにまとめ、自動イベント量を抑える。

6. **AR バッチ化の具体化**
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
- `_enqueue_track_request` で `remaining_frames` をチャンク化する実装を段階的に導入し、SAM2 agent へ `frames: [idx...]` を渡す REST schema を拡張。戻り値も frame ごとの shape 配列に変え、POC では 4 フレーム単位から開始する。
- Agent 側では `tracker.track()` の while ループ内に `for frame in chunk` を挿入し、`SAM2Tracker.track_step()` を連続呼び出しして結果をまとめて返す。Python 側での画像ロードを chunk 内で再利用し、`load_image()` 呼び出し回数を 1/chunk に減らす。
- 計画中の実験: `chunk_size ∈ {1,4,8,16}` で job 8 を GPU/CPU の両方で測定し、`AR count`, `total DB writes`, `run duration` をダッシュボード化。chunk サイズごとの最適点を決める。

### 3. Run status API/DB 最適化
- `functions_annotationrequest` に `(parameters->>'function_run_id')` を抽出した仮想列 + B-Tree index を追加する migration を作成し、`FunctionRunStatusView` に `values('type').annotate(count=Count('id'), avg_duration=Avg(...))` を導入してクエリ数を 1 回に集約する。
- その後、`FunctionRunSummary` (materialized) テーブルを検討し、Agent から run progress を PUT する設計と比較する RFC をまとめる。UI polling interval を 1 s に短縮しても 200 ms 未満で応答できるかを SLIs に設定。
- ベンチマーク: `ab -n 10 -c 1 http://localhost:8080/api/functions/runs/<id>` で median latency を計測し、4.7 s → 0.2 s をターゲットに進捗を記録。

### 4. `_apply_tracking_results` の差分更新
- `cvat/apps/functions/tracking.py` に `DiffBuilder` ヘルパーを追加し、agent から返ってきた shapes をフレーム毎に比較。変更フレームだけ delete/insert し、outside 付与は `TrackedShape` の `outside=True` update で済ませる。
- 現状の「全削除→bulk_create」を廃止し、diff モードで `handle_annotations_change` の呼び出しを 1 回にまとめる。`FunctionRun` の undo/rollback ロジックとも整合するよう、delete/insert の `function_run_id` 追跡を維持する。
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

### 手動計測ログ (2025-11-16, Function #6 / GPU `batch_size ∈ {1,8,16,32}`)
- `SAM2_TRACKER_DEVICE=cuda` で `docker compose --profile sam2-agent up -d` を維持したまま、`scripts/sam2/benchmark_tracker.py` を使って Job 8 (`track_id=27`, `frame=0→19`) を再実行。コマンド例:
  ```bash
  uv run python scripts/sam2/benchmark_tracker.py \
    --server http://localhost:8080 \
    --host-header 192.168.10.190 \
    --username admin --password admin \
    --job 8 --function 6 --track 27 \
    --start-frame 0 --target-frame 19 \
    --batch-sizes 1 8 16 32 \
    --output tasks/sam2_tracker_batch_measurements_20251116.json
  ```
  スクリプトは各 run 完了後に `docker compose exec cvat_db psql` で `functions_annotationrequest` を集計し、構造化 JSON を出力する。
- Raw data: `tasks/sam2_tracker_batch_measurements_20251116.json`

| batch_size | AnnotationRequests (init+track) | Track chunks | Avg track / frame [ms] | Total track [s] | Init [s] | Run wall clock (init→最後の track) [s] | Function run ID |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 20 | 19 | 96.5 | 1.8340 | 0.7363 | 2.646 | `ea791099-1cfd-4d47-b1f8-345b1c9a432b` |
| 8 | 4 | 3 | **63.9** | 1.2144 | 1.0332 | 2.259 | `10efe05d-078c-4d33-b023-c0554d46d43e` |
| 16 | 3 | 2 | 80.5 | 1.5297 | 0.1894 | **1.727** | `fbd46043-2737-4ff2-b713-fe9712c29b1d` |
| 32 | 2 | 1 | 71.4 | 1.3572 | 0.6165 | 1.978 | `0bf0ffbb-a046-46d2-8e11-bbf7e32f3210` |

- 観測:
  - AR 件数は `batch_size=1` の 20 件から `batch_size=16` で 3 件（-85%）、`batch_size=32` では 2 件まで減るため、REST/DB ロードが一気に圧縮できる。
  - `batch_size=16` は今回の GPU セットアップで最短の **1.73 s wall clock** を記録。init の追加コストが 0.19 s に抑えられ、track chunk 2 回で区間を処理できる。
  - `batch_size=8` は 2 本の 8-frame chunk がどちらも ~0.50 s (per-frame ≈63 ms) まで落ち、純粋な推論効率は最も良い。ただし init が 1.03 s まで増えるため、ウォームアップを別フェーズに切り出さないと end-to-end benefit が減る。
  - `batch_size=32` は per-frame 71 ms / wall clock 1.98 s と 16 より僅かに遅い。1 run で 20 frame しか無いケースでは chunk を大きくするメリットが薄いので、デフォルト clamp 値は 16 付近が無難。
  - `avg track / frame` と `wall clock` の乖離は `_apply_tracking_results` が依然フル書き換えのため。Diff apply (Experiment 4) を入れない限り、バッチ化は track AR 部にしか効かない。

### 手動計測ログ (2025-11-16, Function #6 / Job 9 `sam2_measure_200`, frame 0→199)
- 再現手順:
  1. 512×512 のシンセティック PNG（200 枚）を `tmp/sam2_job200` に生成。
     `python - <<'PY' ...` でオレンジ矩形をフレーム番号付きで描画。
  2. `PYTHONPATH=cvat-cli/src:cvat-sdk UV_HTTP_TIMEOUT=120 uv run python -m cvat_cli --auth admin:admin --server-host http://192.168.10.190 --server-port 8080 task create --labels '[{"name":"obj"}]' sam2_measure_200 local tmp/sam2_job200/*.png`
     → Task `9` / Job `9` を作成。
  3. `PUT /api/jobs/9/annotations` で frame 0 に polygon keyframe を投入（生成 Track ID = 30）。
  4. 測定コマンド:
     ```bash
     uv run python scripts/sam2/benchmark_tracker.py \
       --server http://localhost:8080 \
       --host-header 192.168.10.190 \
       --username admin --password admin \
       --job 9 --function 6 --track 30 \
       --start-frame 0 --target-frame 199 \
       --batch-sizes 1 8 16 32 \
       --output tasks/sam2_tracker_batch_measurements_job9_20251116.json
     ```
- Raw data: `tasks/sam2_tracker_batch_measurements_job9_20251116.json`

| batch_size | AnnotationRequests (init+track) | Track chunks | Avg track / frame [ms] | Total track [s] | Init [s] | Run wall clock (init→最後の track) [s] | Function run ID |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 200 | 199 | 109.1 | 21.714 | 1.401 | 23.909 | `75575797-8654-4b58-a78b-d0f1b6298350` |
| 8 | 26 | 25 | 67.4 | 13.420 | 1.210 | 14.731 | `5923dec9-976b-41cd-b13b-524b3d4bb134` |
| 16 | 14 | 13 | 64.9 | 12.916 | 1.110 | 14.081 | `0dfe4e6f-0260-4dca-819d-6c91cd3c115c` |
| 32 | 8 | 7 | **62.6** | 12.455 | **0.496** | **12.982** | `0d41ef50-27ab-4b20-aa21-72001d406cb2` |

- 観測:
  - `batch_size=1` と比較して `32` では AR が 200→8 件、REST/DB 書き込みは **96% 減**。サーバー側の queue 負荷が大幅に軽くなる。
  - per-frame 速度は `109 ms → 62 ms` と約 1.75×に改善。GPU 側は chunk 32 本でも VRAM 16 GB には収まった。
  - `batch_size=32` が wall clock 最短（12.98 s、baseline 比 -45%）だが、`16` も 14.08 s と近似値で、短いジョブ（20 frame）と矛盾しない共通解。
  - よって `CVAT_FUNCTION_TRACKER_DEFAULT_BATCH_SIZE` は 1 から **16** へ引き上げ、UI 既定 + CLI 未指定時のチャンクサイズを 16 に統一する。長尺ジョブのみ上書きしたいケースは `localStorage` のバッチ設定 or エージェント引数で 32 を選択すれば良い。
  - 今後は `_apply_tracking_results` の差分化と `SAM2_TRACKER_VERBOSE` ログ取り込みで `batch_size>=32` が VRAM を圧迫しないか監視。200 frame テストでは peak GPU Mem 13.2 GB（`nvidia-smi`）だった。

## 実装ログ (2025-11-18)
- `SAM2_TRACKER_VERBOSE=1` をセットすると、CLI エージェントと SAM2 追跡器の双方が構造化ログ (`SAM2_TRACKER_LOG {"component":"sam2_tracker", ...}`) を INFO で出力するようにした。`docker compose --profile sam2-agent logs -f sam2-tracker-agent | rg SAM2_TRACKER_LOG` で抽出し、`jq` に渡せる。
- CLI 側 (`cvat_cli/_internal/agent.py`) では tracker AR の `sample.media.load_image()` を計測し、`phase="frame_fetch"` / `ar_type` / `frame_index` / `wall_ms` を記録。TaskDataset init のログと合わせれば I/O ボトルネックの可視化がすぐにできる。
- `ai-models/tracker/sam2/func.py` では `preprocess` と `track_step` を GPU Event で計測し、`wall_ms` と `gpu_ms` を記録。RTX 4080 で実行時は kernel 占有時間を `nvidia-smi dmon` と容易に突き合わせられる。
- SDK に `MediaDownloadPolicy.PREFETCH_CHUNKS_ONCE` を追加し、tracker 用 TaskDataset はチャンクを初回アクセス時に zip 取得→以後はローカルキャッシュから供給するようにした。`--tracker-preload-chunks` でエージェントを起動すると最初のフレームまではオンデマンドだが、以降は HTTP ラウンドトリップが激減する。
- コンテナ内で軽い操作を行うときは、`docker-compose.dev.yml` を併用して `cvat_server` サービスにワンショットで入る。例:
  ```bash
  docker compose -f docker-compose.yml -f docker-compose.dev.yml \
    run --rm --entrypoint bash cvat_server -lc "python --version"
  ```
  これで Python 3.10 系の環境が即座に確認でき、続けて `python manage.py check` や `uv run pytest ...` をコンテナ内で安全に実行できる。
  例として `python manage.py check` を実施したところ、既存サービス（DB/Redis等）を止めることなく `"System check identified no issues"` を得られた。今後はこのパターンをテンプレ化して検証ログに残す。
- SDK pytest をホストから実行する場合は、fixtures が `docker compose` で独自に `test_cvat_*` コンテナ群を起動するため、既存の `cvat_server` 系が稼働していると `pytest` が開始時に終了する。手順としては:
  1. `docker compose -f docker-compose.yml -f docker-compose.dev.yml -f tests/docker-compose.file_share.yml -f tests/docker-compose.minio.yml -f tests/docker-compose.test_servers.yml up -d` で付随サービスを上げる。
  2. `UV_HTTP_TIMEOUT=120 uv sync --group dev --group test --python 3.10` で `.venv` を整備し、`PYTHONPATH=$PWD/cvat-sdk UV_HTTP_TIMEOUT=120 uv run --python 3.10 python -m pytest tests/python/sdk/test_datasets.py -k basic --maxfail=1` を実行する。
  3. 実行前に既存の `cvat_server` などを `docker compose down` で停止しておかないと、fixtures が `It's looks like you already have running cvat containers` で終了する点に注意。

## 実装ログ (2025-11-19)
- `tracker-actions` に `batch_size` / `frames` を導入し、バックエンドが `pending_frames` をチャンクに分割して `AnnotationRequest` を発行するようにした。デフォルトは 1 のままなので既存 UX への影響は無く、`conversion_mode` や `tracking_targets` も従来通りに扱える。
- CLI エージェント (`cvat_cli/_internal/agent.py`) は `ar_params.frames` を検出すると 1 回の AR で複数フレームを `TaskDataset` から順次ロードし、結果を `frames: [{frame, shapes}]` 配列として返却する。互換性のため単一フレーム時は従来通り `shapes` も併記し、`SAM2_TRACKER_VERBOSE=1` では `phase="track_batch"` ログで対象フレーム一覧を出す。
- フロントエンドの `NativeFunctionTrackerAction` は `localStorage.setItem('cvat.nativeTrackerBatchSize', '<N>')` で指定した正の整数を `Job.runFunctionTrackerAction` へ引き渡し、ブラウザコンソールだけでチャンク単位を切り替えられる（無効値や未設定時は無視）。
- TypeScript/SDK 側で `FunctionTrackerRunParams` に `batchSize?: number` を追加し、`session-implementation.ts` が snake_case の `batch_size` を POST。`server-proxy.ts`→Django まで JSON フィールドが通り抜ける。
- バックエンドは `CVAT_FUNCTION_TRACKER_DEFAULT_BATCH_SIZE`（既定 16）と `CVAT_FUNCTION_TRACKER_MAX_BATCH_SIZE`（既定 32）を新設して入力を clamp しつつ、`AnnotationRequest.parameters.frames` / `result.frames` を `tracking.apply` で再構成。`FunctionRunStatusView` など既存の統計処理は `parameters__frame` を先頭フレームとして継続使用できる。
- 回帰として `source ~/.cargo/env && UV_HTTP_TIMEOUT=120 uv run python manage.py test --keepdb cvat.apps.functions.tests.test_api.FunctionsApiTests.test_tracker_action_flow_updates_tracked_shapes cvat.apps.functions.tests.test_api.FunctionsApiTests.test_tracker_action_batches_frames_when_requested` を実行済み。新設テストは multi-frame chunk の結果扱いと `pending_frames` の再チャンクをカバーする。

## 実装ログ (2025-11-20)
- SAM2 tracker エージェント（`cvat_cli/_internal/agent.py`）で `frames` チャンクごとの処理を最適化。`_worker_job_track` がトラック毎の状態をまとめて `track_batch()` に渡すようになり、SAM2 側が複数の state を一度に更新できる。チャンク処理中はフレーム毎の壁時計 (`frame_latencies_ms`) とチャンク全体の `wall_ms`、実際に使われた `frame_loader` を `SAM2_TRACKER_VERBOSE` の `phase="track_chunk"` ログとして記録し、I/O 対 GPU 計算の配分を即座に確認できる。
- `ai-models/tracker/sam2/func.py` に `track_batch()` フックを追加し、既定では既存 `track()` をループ呼び出しするものの、今後 CUDA Stream 等の最適化を埋め込みやすくした。CLI 側は新フックを優先的に呼び、戻り数が期待と異なれば `BadFunctionError` で即座に失敗させる防御も入れている。
- チャンク単位ログのおかげで `TaskDataset` を共有した際の効果を可視化できるため、`track` AR 1 件あたりの I/O + 推論時間を収集→`tasks/sam2_tracker_batch_measurements_*.json` に反映する準備が整った。今後はログと `scripts/sam2/benchmark_tracker.py` を組み合わせて I/O ボトルネックの発生タイミングを記録する。
- テスト:
  1. 既存 compose スタックを完全停止（`docker compose -f docker-compose.yml -f docker-compose.dev.yml down`）し、`test_cvat_*` 用 compose が起動できる状態を確保。
  2. `UV_HTTP_TIMEOUT=120 PYTHONPATH=$PWD/cvat-sdk:$PWD/cvat-cli/src uv run python -m pytest tests/python/cli/test_cli_misc.py -k tracking_supports` を実行 → `TestCliMisc::test_worker_tracking_supports_polygon_and_mask_inputs` が pass。pytest 実行中に fixtures が自動で `test_cvat_*` コンテナ群を立ち上げる。
  3. 終了後は `docker compose --project-name=test_cvat --file docker-compose.yml --file docker-compose.dev.yml --file tests/docker-compose.file_share.yml --file tests/docker-compose.minio.yml --file tests/docker-compose.test_servers.yml down -v` でテスト用コンテナ/ボリュームを破棄。必要に応じて本番 compose を再起動する。

## 次フェーズの高速化計画 (2025-01-xx)
### Experiment 1: Tracker AR chunk batching on GPU (RTX 4080 前提)
- **ステータス (2025-11-21)**: backend / agent / UI / scripts の batch 処理と Job8/9（現在の Job1/2）計測・ADR 反映・ドキュメント更新まで完了し、本流へ取り込み済み。残作業は季節的な再計測のみ。最新ログ: `tasks/sam2_tracker_batch_measurements_20251120_job{8,9}_postenable.json`, `logs/sql/run_requests_20251120_job{8,9}_postenable.csv`。
- **目的**: `track` AR をチャンク単位（例: 16〜32 フレーム）でまとめ、REST/DB オーバーヘッドと SAM2 warmup 再実行を抑制しつつ、OSS ユーザーが設定無しで恩恵を受けられる状態にする。

#### Tracker batching TODO（完了）
- [x] **Function capability rollout**
  - [x] `cvat-cli function create-native` に `--supports-batched-tracker/--no-supports-batched-tracker` を追加し、SAM2 tracker 登録時は既定で true を送る。
  - [x] 既存 Function への一括付与 migration（`RunPython` で tracker kind + SAM2 名称を対象）と適用手順メモを `tasks` に残す。
- [x] **DB / runtime enablement**
  - [x] `python manage.py migrate functions` を本番/開発で適用し、`Function.supports_batched_tracker` が schema に反映されているか確認。（2025-11-20: 最初は PostgreSQL port が開いておらず失敗→`docker compose -f docker-compose.yml -f docker-compose.dev.yml run --rm cvat_server ...` で stack を再作成し、Host:5432 を公開させた後に `UV_HTTP_TIMEOUT=120 uv run python manage.py migrate functions` を実行して `0003/0004` 適用済み）
  - [x] `.env` / compose で `CVAT_FUNCTION_TRACKER_DEFAULT_BATCH_SIZE=16`, `CVAT_FUNCTION_TRACKER_MAX_BATCH_SIZE=32` を固定し、`docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d cvat_server sam2-tracker-agent` で env を再読み込みする手順を記録。
- [x] **UI/Docs周知**
  - [x] `NativeFunctionTrackerAction` の docstring / user guide に「設定 → Run Annotation Action → Batch size」の手順を記載。短期的には `localStorage` での切替方法を README/ADR に追記。（README.md の SAM2 セクションへ `localStorage` 手順を追記済み）
  - [x] `AGENTS.md` に tracker capability と env 依存の注意点（例: Batch 未対応 Function での UI 挙動）を追加。
- [x] **テスト & 計測**
  - [x] `uv run python manage.py test --keepdb cvat.apps.functions.tests.test_api.FunctionsApiTests.test_tracker_action_batches_frames_when_requested` を migrate 後に再実行し、serializer バリデーション退行を検知。（2025-11-20: `UV_HTTP_TIMEOUT=120` 付きで pass 確認）
  - [x] `scripts/sam2/benchmark_tracker.py --batch-sizes 1 8 16 32 --repeat 3` を Job8/9 で再実行し、`tasks/sam2_tracker_batch_measurements_20251120_job{8,9}_postenable.json` を保存。
  - [x] SQL ログ (`logs/sql/run_requests_20251120_job{8,9}_postenable.csv`) を採取し、Experiment 1 セクションに before/after サマリを追記。
- [x] **完了条件**
  - [x] CLI / migration / docs の PR 連携が merge 済み。
  - [x] Job8/9 の batch16 実測で per-frame 70 ms 以下・AnnotationRequest 件数 1/16 以下を再確認（post-enable ログ参照）。
  - [x] `tasks/sam2_tracker_bottleneck.md` の Workstream ボードと Experiment 1 セクションが「完了」を示し、次の Experiment へ移行可能な状態。

### Experiment 2: Dataset streaming/caching pipeline hardening
- **目的**: Tracker / Interactor で共用する chunk-preload キャッシュを GPU 前提で検証し、大規模 task の I/O 停滞を除去。
- **ステップ**:
  1. `--tracker-preload-chunks` 有効時に `MediaDownloadPolicy.PREFETCH_CHUNKS_ONCE` を選べるよう CLI フラグを追加（未実装の場合）。
  2. LRU の eviction telemetry (`cache_hits`, `evictions_with_chunks`) を Prometheus exporter へ配管。
  3. RTX 4080 + NVMe 環境で `job=30`（1,000 frames 予定）を使い、`TaskDataset init`, `chunk download`, `track_step` の時間を構造化ログで採取。
- **検証**: chunk キャッシュを無効/有効で比較し、`track` AR 時間が 1.3 s → 0.3 s 台へ縮むかを確認。IO バックプレッシャー発生時は `aiohttp` の connection pool サイズを増減して追跡。

### Experiment 3: Run status endpoint refactor
- **目的**: `GET /api/functions/runs/{id}` レイテンシを 4.7 s → 0.2 s 未満へ。UI ポーリングを 1 Hz まで引き上げても DB 負荷が跳ねない状態を作る。
- **TODO（2025-11-22 アップデート）**:
  1. [x] `functions_functionrunstatus`（旧 `FunctionRunSummary` の後継）を定義し、AnnotationRequest から切り離した集約情報を 1 行で保持する。
  2. [x] `AnnotationRequest.run_status` FK を required にし、agent/serializer 層で `function_run_id` をキーに `FunctionRunStatus` を常に参照させる。
  3. [x] `FunctionRunStatusView` / Cancel API を summary only 実装へ切り替え、JSONB fallback を削除する。2025-11-19 run（`run_id=11205f08-4f92-4b03-9190-0b97a31faacc`）で tracker を実行 → `tasks/profile_run_status.sql` (`include_legacy=0`) を再取得し、`logs/sql/run_status_profile_after_20251119_job2_repeat2.txt` に 0.05 ms 台の summary lookup を記録。`cvat/apps/functions/tracking.py` は `run_status_id` のみ参照、`register_request_*` も FK 欠損時に `logger.warning` を吐くため JSONB パスは完全に廃止済み。`logs/cvat_server.log` に fallback 警告が出ていないことも確認。
  4. [x] `profile_run_status.sql` を使った before/after ベンチをテンプレート化し、`ab` / `wrk` のシナリオを `tasks` に保存する（`include_legacy` フラグで旧クエリを省略可）。before/after の両ログ (`logs/sql/run_status_profile_*_072330.txt`, `..._after_20251119_191422.txt`) と HTTP 計測結果 (`ab`/`wrk` の before/after ログ) を取得済み。今後は summary-only 実装の最適化（例: index 調整）で P99≦50 ms まで短縮するタスクに置き換える。
- **検証指標**: `ab -n 20 -c 5` / `wrk -c 16 -d 30s` で percentile を比較。`django_debug_toolbar` の SQL count も記録し回帰を検出。

#### FunctionRunStatus table draft
> 備考: 2025-11-21 snapshot で導入した `FunctionRunSummary` は run_id ごとに 1 行あるが、`AnnotationRequest.run_summary` は `NULL` 許容のままで fallback も残っている。本設計ではそれを v2 として作り直し、`FunctionRunStatus` へリネーム + NOT NULL FK 化 + index 強化を行う。

| Column | Type | Source / updateタイミング | 備考 |
| --- | --- | --- | --- |
| `run_id` | `uuid` PK | Tracker action submit 時に確定 | `functions_annotationrequest.parameters->>'function_run_id'` と同値 |
| `owner_id` | `int` FK | Function owner | `auth_user(id)` への FK |
| `task_id` / `job_id` | `int` FK | AnnotationRequest.job/task | `job_id` は NOT NULL、`task_id` は job 経由で整合チェック |
| `function_id` | `int` FK | 対象 Function | kind=tracker 以外でも再利用可能にする |
| `status` | `varchar(16)` | run_summary helpers | Enum (`pending/running/done/failed/cancelled`) |
| `total_requests` / `completed_requests` / `failed_requests` / `cancelled_requests` | `int` | `register_request_*` | `total_requests` には init+chunk AR を含める |
| `expected_frames` / `completed_frames` | `int` | init + track 更新 | `expected_frames` NULL 時は request count ベース |
| `progress` | `numeric(5,4)` | `calculate_progress` | done=1.0、その他は 0.0–0.99 |
| `active_request_id` / `type` / `progress` / `frame_span` / `updated_at` | mix | `register_request_running/progress` | UI が「今動いている AR」を描画するための最小セット |
| `last_error` | `jsonb` | 失敗時 | 例: `{"request_id": "...", "detail": "...", "updated_at": "..."}` |
| `payload_version` | `smallint` | migration 以降 | 将来の schema 変更検知に使用 |
| `created_at` / `updated_at` | `timestamptz` | DB default | `updated_at` は trigger (`set_current_timestamp`) で更新 |

#### Index & FK plan
- PK: `PRIMARY KEY (run_id)` を維持。`run_id` はビュー/Cancel の lookup 主キー。
- Owner filter: `CREATE INDEX CONCURRENTLY functions_runstatus_owner_idx ON functions_functionrunstatus (owner_id, run_id);`
- Live polling filter: `CREATE INDEX CONCURRENTLY functions_runstatus_job_idx ON functions_functionrunstatus (job_id, status, updated_at DESC);`
- Background metrics: `CREATE INDEX CONCURRENTLY functions_runstatus_function_idx ON functions_functionrunstatus (function_id, status);`
- `AnnotationRequest` から `FunctionRunStatus` へ `run_status` FK（`NOT NULL`, `on_delete=CASCADE`）を追加。`CASCADE` により run 削除時に AR も落ちるため整合性維持。

#### Migration phases & locking memo
1. **Phase A – additive schema**  
   - Create `functions_functionrunstatus` + indexes via `CREATE TABLE` + `CREATE INDEX CONCURRENTLY`.  
   - Add nullable `run_status` FK to `functions_annotationrequest`. Default `NULL` で既存行は未設定のまま。
2. **Phase B – backfill**  
   - New management command `backfill_run_status --chunk-size 1000` が `parameters->>'function_run_id'` でグループ化し、`INSERT ... ON CONFLICT (run_id)` で summary 行を upsert。  
   - 同一トランザクションで対象 run の `AnnotationRequest` を `UPDATE ... SET run_status_id = <run_id>`。`FOR UPDATE SKIP LOCKED` を使い、1 回のトランザクションで 1000 件まで更新してロック滞留を抑える。  
   - command には `--dry-run` / `--since '2025-10-01'` 等を用意し、長時間ロック防止のため `SET LOCAL statement_timeout='5s'` を入れる。
3. **Phase C – dual write**  
   - `register_request_*` および tracker submission 経路で `FunctionRunStatus` を必ず参照し、`AnnotationRequest.run_status_id` を必須化。  
   - `settings.FUNCTION_RUN_STATUS_USE_SUMMARY` flag を `True` にすると、FunctionRunStatusView は summary だけを読む。flag OFF の間は fallback を残し、ログで検知できるよう WARNING を出す。
4. **Phase D – cleanup**  
   - Fallback ロジックと `parameters__function_run_id` への `filter` を削除。  
   - `AnnotationRequest.parameters` から `function_run_id` を optional 化（互換のため当面維持）。  
   - Migration で `run_status` FK を `NOT NULL` に変更し、`DROP INDEX` / `VACUUM (ANALYZE)` を実施。

**Lock impact**  
- Phase A の `CREATE TABLE` は瞬時ロックのみ。`CREATE INDEX CONCURRENTLY` で長時間ロックを避ける。  
- Phase B の `UPDATE ... WHERE id IN (...)` は chunk サイズを 1000 以下に制限し、`SKIP LOCKED` で他処理と競合しない。  
- Phase C の `ALTER TABLE ... SET NOT NULL` は重いので backfill 完了を `SELECT count(*)` で確認→夜間メンテ窓で実行。 `LOCK TABLE functions_annotationrequest IN ACCESS EXCLUSIVE MODE` が走るため、`--lock-timeout=5s` を設定し必要に応じて再実行。

#### profile_run_status.sql 雛形
```sql
\set run_id '00000000-0000-0000-0000-000000000000'

-- 現行: JSONB seq scan
EXPLAIN (ANALYZE, BUFFERS, VERBOSE)
SELECT count(*) FROM functions_annotationrequest
WHERE parameters ->> 'function_run_id' = :'run_id';

EXPLAIN (ANALYZE, BUFFERS, VERBOSE)
SELECT *
FROM functions_annotationrequest
WHERE parameters ->> 'function_run_id' = :'run_id'
ORDER BY created_at;

-- 提案: summary テーブルのみ
EXPLAIN (ANALYZE, BUFFERS, VERBOSE)
SELECT status, progress, total_requests, completed_requests
FROM functions_functionrunstatus
WHERE run_id = :'run_id';

-- pg_stat_statements snapshot
SELECT total_exec_time, mean_exec_time, calls, query
FROM pg_stat_statements
WHERE query ILIKE '%functions_run%'
ORDER BY mean_exec_time DESC
LIMIT 10;
```
- ファイル配置: `tasks/profile_run_status.sql`。`docker compose exec cvat_db psql -U root -d cvat -f tasks/profile_run_status.sql | tee logs/sql/run_status_$(date +%Y%m%d_%H%M).txt` をワンライナー化して再利用。
- Backfill 前後の欠損確認用に `tasks/check_run_status_backfill.sql` も配置済み。`missing_total=0` を確認できるログ（`logs/sql/run_status_backfill_{before,after}_*.txt`）を残し、NOT NULL migration の前提条件にする。
- Before/After で `functions_annotationrequest` に対する `Seq Scan` が `Index Scan using functions_runstatus_owner_idx` へ置き換わることを確認する。

#### Implementation snapshot (予定)
- [設計済] モデル層: `FunctionRunStatus` dataclass の型定義 + `run_status` FK を `cvat/apps/functions/models.py` に追加。
- [未実装] Serializer/View: `FunctionRunStatusSerializer` を summary モデルに完全対応させ、`FunctionRunStatusView` が `select_related('function', 'job')` の 1 クエリで済むようにする。
- [未実装] Backfill CLI: `python manage.py backfill_run_status --dry-run` で対象 run 数と最大 AR 件数を表示し、`--run-id` で個別処理できるようにする。
- [未実装] Telemetry: `register_request_*` が JSONB fallback を通った場合は `logger.warning("run_status_missing", run_id=...)` を吐き、切替忘れを検知する。
- [2025-11-19 更新] `_apply_tracking_results` は `annotation_request.run_status_id` のみを参照し、`parameters["function_run_id"]` には依存しない。`register_request_*` で FK 欠損を検知した場合は `logger.warning` を吐くようにしたため、JSONB フォールバックが動作していれば即座にログで気付ける。

#### Backfill & verification runbook
1. 事前に `docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d cvat_db` で DB を起動し、`uv run python manage.py showmigrations functions` で `0007/0008` 適用済みを確認。
2. `docker compose exec cvat_db psql -U root -d cvat -f tasks/check_run_status_backfill.sql | tee logs/sql/run_status_backfill_before_$(date +%Y%m%d_%H%M).txt` を実行し、`run_status_id IS NULL` の件数と category breakdown を取得。
3. `UV_HTTP_TIMEOUT=120 uv run python manage.py backfill_run_status --chunk-size 500 --skip-locked --dry-run | tee logs/sql/run_status_backfill_dryrun_$(date +%Y%m%d_%H%M).txt` で対象 run を確認。Interactor も合わせて処理する場合は `--include-interactive` を付ける。
4. 問題なければ `UV_HTTP_TIMEOUT=120 uv run python manage.py backfill_run_status --chunk-size 500 --skip-locked [--include-interactive] | tee logs/sql/run_status_backfill_apply_$(date +%Y%m%d_%H%M).txt` を実行。長時間ロックを避けるため必要に応じて `--limit` で分割。
5. 再度 `tasks/check_run_status_backfill.sql` を流し、`missing_total=0` になったことを確認。結果を `logs/sql/run_status_backfill_after_*.txt` へ保存。

#### Backfill execution (2025-11-23)
- 事前計測: `logs/sql/run_status_backfill_before_20251123_????.txt` に `missing_total=4375`（うち `batch=4260`, `interactive=115`）を記録。`function_run_id` を持つ不足分は tracker run のみ。
- `CVAT_POSTGRES_HOST=$(docker inspect -f '{{range.NetworkSettings.Networks}}{{.IPAddress}}{{end}}' cvat_db)` を付与してホスト側から `UV_HTTP_TIMEOUT=120 uv run python manage.py backfill_run_status --chunk-size 500 --skip-locked --dry-run`→本番実行を走らせ、結果を `logs/sql/run_status_backfill_{dryrun,apply}_20251123_*.txt` に保存。
- 実行結果: tracker run 97件（max 200 requests/run）が backfill され、`status` は `done` もしくは過去の `failed/cancelled` を維持。`logs/sql/run_status_backfill_apply_20251123_*.txt` に run_id ごとの件数が残っている。
- 再計測: `logs/sql/run_status_backfill_after_20251123_*.txt` で tracker + interactor とも `missing_total=0` を確認。Interactor 向けには `--include-interactive` 付きで `python manage.py backfill_run_status` を再実行し、`logs/sql/run_status_backfill_interactive_{dryrun,apply}_20251123_*.txt` に結果を保存済み。

#### profile_run_status runbook
1. `UV_HTTP_TIMEOUT=120 uv run python manage.py shell -c "from cvat.apps.functions.models import FunctionRunStatus; print(FunctionRunStatus.objects.order_by('-updated_at').first().run_id)"` で直近 run を取得。
2. `RUN_ID=<uuid>` を `.env.local` に書き、`docker compose exec cvat_db env RUN_ID=$RUN_ID psql -U root -d cvat -v run_id="'$RUN_ID'" -f tasks/profile_run_status.sql > logs/sql/run_status_before_${RUN_ID}.txt`.
3. Migration & backfill 後に同じ手順で `run_status_after` ログを取得し、`grep -E 'Execution Time'` で実行時間を比較。改善幅 4.7 s → 0.2 s（または better）であることを確認。

#### Profiling snapshot (run_id=ff50c1ee-a55b-49f1-a7df-5e36baa126ae)
- `tasks/profile_run_status.sql` を `docker compose exec cvat_db psql -v run_id=ff50...` で実行し、`logs/sql/run_status_after_ff50c1ee.txt` と `logs/sql/run_status_after_4f63c559_pgstat.txt` を取得。
- `functions_annotationrequest` への JSONB seq scan は依然として ~58 ms/クエリなのに対し、`functions_functionrunstatus` lookup は 0.01 ms。`pg_stat_statements` を dev compose の `shared_preload_libraries` に追加 → `CREATE EXTENSION IF NOT EXISTS pg_stat_statements;` を `postgres/cvat` の両 DB で実行したことで snippet セクションにも集計が表示されるようになった。
- `ab -n 50 -c 5 -H 'Host: 192.168.10.190' -H 'Authorization: Basic ...' http://localhost:8080/api/functions/runs/<run_id>` で Run status API（トレーリングスラッシュ無し）の HTTP レイテンシを計測。`logs/sql/run_status_ab_after.txt` では `RPS≈43` / `median=103 ms`（ほぼ DB seq scan の待ち）。
- `wrk -t4 -c100 -d30s .../runs/<run_id>` も実行し、`logs/sql/run_status_wrk_after.txt` に `Requests/sec ≈177`, `平均レイテンシ≈558 ms`（p95≒922 ms）が出ている。pg_stat_statements の `SELECT ... FROM functions_functionrunstatus WHERE owner_id=? AND run_id=?` は `calls=5432` / `mean_exec_time=0.049 ms`（`max=4.14 ms`）で済んでおり、残りほぼ全てが JSONB seq scan → Django レイヤーの待ちであることを示す。

#### Implementation snapshot (2025-11-22)
- `FunctionRunSummary` を `FunctionRunStatus` へリネームし、`last_error` / `payload_version` カラム追加と index 固定化を `cvat/apps/functions/migrations/0007_functionrunstatus_v2.py` で実装。`AnnotationRequest.run_summary` → `run_status` へ改称し、JSONB fallback を削除。
- `create_tracker_run_status` により tracker submission 時に summary を生成し、`FunctionRunStatusView` / Cancel API も summary 専用に一本化。`FunctionRunStatusSerializer` は DB モデルをそのままシリアライズする構造へ整理。
- `backfill_run_status` management command を `--chunk-size`（default 500）と `--skip-locked` に対応させ、`SELECT ... FOR UPDATE` でロック可能な run を chunk 処理する。旧 `backfill_run_summary` は非推奨 alias として残し、help で移行を案内。
- Interactor 側でも `start_interactor_request` が `create_interactor_run_status` を呼び出し、`parameters["function_run_id"]` と `run_status` FK を必ず設定するようになった。既存 interactor リクエストは `python manage.py backfill_run_status --include-interactive` で生成した run_id（欠損時は request UUID）を割り当て済み。
- `FunctionsApiTests` に `_create_run_status` helper を追加し、run status endpoint / cancel テストが summary ベースで動作することを検証。UI/API が JSONB fallback へ戻らないよう回帰検出できる。

#### Implementation snapshot (2025-11-21, 現状の既存実装メモ)
- `FunctionRunSummary` モデルと `AnnotationRequest.run_summary` FK を追加（migrations `0005_functionrunsummary.py`, `0006_annotationrequest_run_summary.py`）。Tracker init 時に summary を生成し、以降の track AnnotationRequest は summary に紐付けるよう更新。
- `cvat/apps/functions/run_summary.py` を新設し、request 作成/進行/完了/失敗/キャンセルごとに summary を更新。`create_tracker_run_summary` や `register_request_*` ヘルパを `tracking.py` / Queue views / `acquire_annotation_request` に組み込んで、DB `SELECT ... FOR UPDATE` ベースで集計する。
- `FunctionRunStatusView` は `FunctionRunSummary` を優先参照し、未サマりの既存 run は従来の JSONB 集計にフォールバック。Cancel エンドポイントも summary を即座に `status="cancelled"` に更新する。
- 新規コマンド `python manage.py backfill_run_summary [--run-id ...] [--dry-run] [--limit N]` を追加。`--dry-run` で対象 run を洗い出したあと、本番環境では `--run-id` で段階的に backfill できる。サンプル: `UV_HTTP_TIMEOUT=120 uv run python manage.py backfill_run_summary --dry-run` → `run_id` ごとの request 数を一覧表示（Job8/9 run を含む） → `UV_HTTP_TIMEOUT=120 uv run python manage.py backfill_run_summary --run-id <job8-run> --run-id <job9-run>` で個別 backfill。
- テスト: `UV_HTTP_TIMEOUT=120 uv run python manage.py test --keepdb cvat.apps.functions.tests.test_api.FunctionsApiTests.test_tracker_action_batches_frames_when_requested` を実行し、tracker API 回りの回帰が無いことを確認。

#### Next actions（Run status refactor v2）
- [x] Migration 00xx: `functions_functionrunsummary` → `functions_functionrunstatus` のリネーム + 新カラム（`payload_version`, `last_error` 等）追加 + `run_status` FK 追加（Phase A）。`0007_functionrunstatus_v2` / `0008_alter_*` で反映済み。
- [x] Backfill CLI v2: `python manage.py backfill_run_status --chunk-size 1000 --skip-locked` を実装し、Phase B の手順（dry-run → 実行 → ログ採取）を README へ展開予定。旧コマンドは非推奨 alias のみ。
- [x] Hardening: 既存 run の backfill を完了させ、`AnnotationRequest.run_status` を `NOT NULL` に変更。backfill レポート (`logs/sql/run_status_backfill_after_20251123_*.txt`, `logs/sql/run_status_backfill_after_notnull_*.txt`) を保存済み。`python manage.py backfill_run_status --include-interactive` で interactor を含む欠損ゼロを確認後、`python manage.py makemigrations functions` → `migrate functions` で `0009_alter_annotationrequest_run_status` を適用し、`check_run_status_backfill.sql` で再検証した。以降は Tracker/Interactor どちらも run_status FK が必須となる。
- [x] Hardening: 既存 run の backfill を完了させ、`AnnotationRequest.run_status` を `NOT NULL` に変更。backfill レポート (`logs/sql/run_status_backfill_after_20251123_*.txt`, `logs/sql/run_status_backfill_after_notnull_*.txt`) を保存済み。`python manage.py backfill_run_status --include-interactive` で interactor を含む欠損ゼロを確認後、`python manage.py makemigrations functions` → `migrate functions` で `0009_alter_annotationrequest_run_status` を適用し、`check_run_status_backfill.sql` で再検証した。以降は Tracker/Interactor どちらも run_status FK が必須となる。
- [x] Profiling: `tasks/profile_run_status.sql` を before/after で実行し、`logs/sql/run_status_{before,after}_<date>.txt` を格納。`run_id=4f63c559-c35f-4ead-9fda-5a60f862bb76` では `include_legacy=1`（before, `...072330.txt`）と `include_legacy=0`（after, `...after_20251119_191422.txt`）の両方を取得し、後者では legacy クエリブロックが完全にスキップされ summary lookup のみが **0.095 ms** で返ることを確認。HTTP bench も `ab -n 20 -c 5`（`...after_20251119_191432.txt`, P99=111 ms）と `wrk -t2 -c16 -d30s`（`...after_20251119_191437.txt`, 115 req/s, avg 139 ms）を採取し、before ログと合わせて比較できる状態にした（Phase D 完了）。
- [x] HTTP bench: `ab -n 10 -c 2` と `wrk -t2 -c8 -d15s` を `run_id=4f63c559-c35f-4ead-9fda-5a60f862bb76` で実行し、`Authorization: Basic admin:admin` + `Host: 192.168.10.190` を付与した結果を `logs/sql/run_status_ab_*.txt` / `logs/sql/run_status_wrk_*.txt` に保存。現状 API 1 回あたり ≈100 ms（P99=102 ms, 74 req/s）で頭打ちになっており、JSONB fallback を除去し summary table へ完全移行する必要性が定量化できた。

### Experiment 4: `_apply_tracking_results` diff モードと並列適用
- **目的**: 4 s かかる全削除→再挿入を廃止し、差分更新+並列書き込みにより Job 8 で ≦0.8 s、Job 30 で ≦3 s に短縮。
- **工程**:
  1. `DiffBuilder` プロトタイプを `cvat/apps/functions/tracking_diff.py` に実装し、`TrackedShape` 単位で `added/updated/deleted` を算出。
  2. `_apply_tracking_results` を diff モードへ恒久的に切り替え、`settings` から参照する必要が無いようにする。
  3. `transaction.atomic` 内で Frame ID ごとにバッチを切り、`asyncio.to_thread` で DB ライターを 2〜4 並列に走らせるオプションを調査（PostgreSQL のセッション数制限へ留意）。
- **検証**: `pytest cvat/apps/functions/tests/test_tracking_apply.py -k diff` を追加し、差分計算の idempotency を保証。実ジョブでは `job=8/30` の `annotations` 反映所要時間を `curl` で測定。
- **タスク backlog**（背景: `_apply_tracking_results` が全削除→再挿入のため Job 8 で ~4 s を消費、`avg track / frame` と `wall clock` の乖離を解消する必要がある）:
  - [ ] `cvat/apps/functions/tracking_diff.py` を作成し、`TrackedShape` と tracker 結果から `added/updated/deleted` を求める `DiffBuilder` を実装する（ShapeDigest で points/outside/z_order を比較）。
  - [ ] `_apply_tracking_results` を DiffBuilder 出力に基づく apply ロジックへ切り替え、`SAM2_TRACKER_VERBOSE` に `phase="apply_diff"` / `apply_diff_ms` を記録して壁時計短縮効果を可視化する。
  - [ ] `cvat/apps/functions/tests/test_tracking_apply.py` に差分適用が idempotent であり、変更が無いフレームには書込みが走らないことを検証するケースを追加する。

### Experiment 5: 観測と回帰セーフティネット
- `SAM2_TRACKER_VERBOSE` に GPU kernel 実行時間（`cudaEvent`）を追加し、`nvidia-smi dmon` と突き合わせてボトルネックを即判断できるようにする。
- `tests/python/tracker/test_tracker_bottlenecks.py` を作成し、`pytest --run-bottleneck` でチャンク化 + diff モードの有効/無効を切替えながら所要時間のリグレッションを検知。
- GitHub Actions（GPU unavailable）の代替として、`docker compose -f docker-compose.dev.yml -f dev/docker-compose.cuda.yml` で smoke テストのみを実行し、主要なロジックは mock で検証する。

## Container / Dependency Notes (2025-11-19)
- 直近で問題になっているソースは `Dockerfile` → `backend_entrypoint.d/` → `backend_entrypoint.sh` で組み上げる `cvat_server` イメージ。SAM2 tracker の Python 側の実装は `cvat_cli/_internal/agent.py`, `ai-models/tracker/sam2/func.py`, `cvat/apps/functions/tracking.py` に分散しているため、コンテナを経由する検証時はこの 3 箇所のコード差分をマウントして挙動を確認する。
- `cvat_server` の追加依存: `uv`, `pytest`, `pytest-cases`, `pipx` を `pip uninstall -y pip setuptools wheel` より前にインストールすると、既存の自動クリーニングを壊さずにワンショット検証が可能。イメージ内ではまだ `docker` CLI が無いので、SDK pytest のような fixture が `docker compose` を要求するケースはホスト側でのみ実行する。
- Linter で警告が出ている `cvat-sdk/pyproject.toml` の `tool.setuptools` セクションは、`tool.uv` に追従する形で段階的に整理する。`workspace`（uv の概念）への移行が完了するまでは、`pyproject` を巻き戻した状態でも `uv sync --group dev --group test --python 3.10` で `.venv` を再生成し、`PYTHONPATH=$PWD/cvat-sdk` を渡して CLI/SDK のテストを走らせる。
- コンテナで軽作業を行う際の確認コマンド:
  ```bash
  docker compose -f docker-compose.yml -f docker-compose.dev.yml \
    run --rm --entrypoint bash cvat_server -lc "python manage.py check && uv --version && python -m pytest --version"
  ```
  (`cvat_server` イメージが手元でビルド済みであることが前提)。この一発で Django 健康状態と uv/pytest の導入を同時に検証できる。

## Execution Sprint (2025-W47)
1. **Tracker batching PoC**
   - 期限: W47 Thu EOD / Owner: dai.
   - `tracker-actions` payload に `batch_size` を追加 → `cvat_cli/_internal/agent.py` にバッチ処理パスを実装。`SAM2_TRACKER_VERBOSE` ログは `batch_frames=[...]` を含める。
   - 計測: Job 8/15 で `batch_size=1,8,16,32` を実施し、`AnnotationRequest` 件数、`track_step` 平均、GPU 利用率を記録。結果を本ファイルに追記。
2. **Chunk preload hardening**
   - `MediaDownloadPolicy.PREFETCH_CHUNKS_ONCE` を Tracker/Interactor 双方で選択可能にし、`--tracker-preload-chunks` がセットされたときのみキャッシュを初期化。
   - `ai-models/tracker/sam2/func.py` で `cache_hit_ratio` をログ出力し、Prometheus exporter で scrape。
   - 1000 frame ジョブを想定した I/O スループット計測の段取りを tests/python/shared/fixtures 手順に沿って整える。
3. **Run status endpoint リファクタ設計**
   - `FunctionRunStatusView` の SQL/JSON パスを `explain analyze` で採取 → migration (index/集約テーブル) の PoC を `cvat/apps/functions/migrations/00xx` に起こす。
   - Django テストは `UV_HTTP_TIMEOUT=120 uv sync ...` → `uv run python manage.py test --keepdb cvat.apps.functions.tests.test_api.FunctionsApiTests.test_tracker_action_*` で影響を確認。
4. **Tracking diff apply prototype**
   - `_apply_tracking_results` を新モジュール `tracking_diff.py` に切り出す PoC を開始し、差分ビルダーに型ヒントを付与。
   - `pytest cvat/apps/functions/tests/test_tracking_apply.py -k diff` を追加予定（AGENTS.md にもテスト手順を追記済みなので、ここではテストの設計と必要データを洗い出す）。

各ステップで得たログ・計測値は `tasks/sam2_tracker_bottleneck.md` に追記し、AGENTS.md の運用手順と乖離しないよう並行して更新する。

## Implementation / Validation Breakdown
### Experiment 1（Tracker batching）
- **Touch points**: `cvat_cli/_internal/agent.py`, `cvat/apps/functions/serializers.py`, `cvat/apps/functions/views.py`, `ai-models/tracker/sam2/func.py`, `cvat-ui/src/api-legacy/functions.ts`（capability 宣言）。必要に応じて `cvat/apps/functions/tests/test_api.py` に追加ケースを作る。
- **Dependencies**: 既存 PyTorch/SAM2 で完結。`torch.cuda.Stream` を使うため CUDA toolkit が有効か確認。REST API では新しい `batch_size`/`frames` キーが追加されるため、`pyproject.toml` 側の `cvat-cli` エントリーで schema を同期する。
- **Validation**:
  ```bash
  UV_HTTP_TIMEOUT=120 uv sync --group dev --group test --python 3.10
  UV_HTTP_TIMEOUT=120 PYTHONPATH=$PWD/cvat-sdk uv run --python 3.10 \
    python manage.py test --keepdb cvat.apps.functions.tests.test_api.FunctionsApiTests.test_tracker_action_*
  ```
  その後、Job 8/15 で `batch_size=1,8,16,32` を CLI から実行し、`SAM2_TRACKER_VERBOSE=1` でログを収集。

### Experiment 2（Chunk preload hardening）
- **Touch points**: `cvat-cli/_internal/agent.py`（フラグ処理）、`cvat-sdk/cvat_sdk/datasets/task_dataset.py`, `cvat-sdk/cvat_sdk/datasets/common.py`, `ai-models/tracker/sam2/func.py`（cache hit telemetry）、`cvat/apps/analytics/metrics.py`（必要ならメトリクス登録）。
- **Dependencies**: `aiohttp` のコネクション設定、Prometheus exporter (`prometheus_client`) を既存プロセスにバンドル。`uv` workspace で `prometheus-client` を optional-deps に加える際は `uv add` を利用。
- **Validation**: ホスト側で fixtures を伴う SDK テストを実行。
  ```bash
  docker compose -f docker-compose.yml -f docker-compose.dev.yml \
    -f tests/docker-compose.file_share.yml -f tests/docker-compose.minio.yml \
    -f tests/docker-compose.test_servers.yml up -d
  PYTHONPATH=$PWD/cvat-sdk UV_HTTP_TIMEOUT=120 uv run --python 3.10 \
    python -m pytest tests/python/sdk/test_datasets.py -k "prefetch or caching" --maxfail=1
  ```
  実ジョブでは `--tracker-preload-chunks` 有無で `SAM2_TRACKER_LOG` を比較し、`cache_hit_ratio` を記録。

### Experiment 3（Run status endpoint）
- **Touch points**: `cvat/apps/functions/models.py`, `cvat/apps/functions/views.py`, `cvat/apps/functions/serializers.py`, 新規 migration (`cvat/apps/functions/migrations/00xx_run_status_refactor.py` など)。
- **Dependencies**: PostgreSQL で `jsonb_path_query` 周りの統計を `ANALYZE` した上で `CREATE INDEX` を評価。`pytest-cases` など既存依存で十分。
- **Validation**:
  ```bash
  UV_HTTP_TIMEOUT=120 uv sync --group dev --group test --python 3.10
  UV_HTTP_TIMEOUT=120 uv run python manage.py test --keepdb \
    cvat.apps.functions.tests.test_api.FunctionsApiTests.test_run_status_lifecycle
  ```
  実サービスでは `ab` や `wrk` を `docker compose ... run cvat_server` 内から起動し、1 run あたりのレイテンシを計測。

### Experiment 4（Diff apply）
- **Touch points**: `cvat/apps/functions/tracking.py`, 新規 `cvat/apps/functions/tracking_diff.py`, 追加ユニットテスト `cvat/apps/functions/tests/test_tracking_apply.py`。必要なら `cvat/apps/functions/tests/__init__.py` に helper を追加。
- **Dependencies**: 追加 Python 依存なし。`asyncio` を使う際は Django transaction との整合性に注意。
- **Validation**:
  ```bash
  PYTHONPATH=$PWD/cvat-sdk UV_HTTP_TIMEOUT=120 uv run --python 3.10 \
    python manage.py test --keepdb cvat.apps.functions.tests.test_tracking_apply
  ```
  手動計測では Job 8/30 で `curl -w '%{time_total}'` を使い `annotations` 反映時間を確認。

### Experiment 5（観測/回帰）
- **Touch points**: `ai-models/tracker/sam2/func.py`, `cvat-cli/_internal/agent.py`, 新規テスト `tests/python/tracker/test_tracker_bottlenecks.py`, GitHub Actions 用の `dev/docker-compose.cuda.yml`（smoke）。`tasks/sam2_tracker_bottleneck.md` にログ形式をサンプル付きで記載する。
- **Dependencies**: `pytest-cases`（既に導入済み）でパラメトリックテストを構築。GPU ログを扱う場合は `nvidia-smi` が利用可能なホストで実施。
- **Validation**: `pytest --run-bottleneck` フラグを追加し、CI では skip、GPU ノードでは `UV_HTTP_TIMEOUT=120 uv run --python 3.10 python -m pytest tests/python/tracker/test_tracker_bottlenecks.py --run-bottleneck` を手動実行。

## W47 Thu Detailed Plan (2025-11-20)
### 1. Tracker batching PoC
#### Implementation checklist
- [ ] `cvat/apps/functions/serializers.py`: `TrackerActionSerializer` に `batch_size: int | None`, `frames: list[int] | None` を追加し、`supports_batched_tracker` capability が false の場合は validation error を返す。
- [ ] `cvat/apps/functions/views.py`: `NativeFunctionTrackerAction` で `frames` を省略時に `range(target_frame - start_frame + 1)` を生成し、既存単発モードと互換にする。
- [ ] `cvat-cli/src/cvat_cli/_internal/agent.py`: `run_tracker_action()` に batch 処理ループを追加し、1 つの `TaskDataset` を共有したまま `frames` を順に処理する。`SAM2_TRACKER_VERBOSE` で `{"phase":"track","batch_frames":[...]}` を出す。
- [ ] `ai-models/tracker/sam2/func.py`: `track_frames()` を新設し、`SAM2Tracker` が `init_tracking` 後に `track_step` を複数回連続実行できるよう調整（`torch.cuda.Stream` を optional で利用）。
- [ ] `cvat-ui/src/components/annotation-page/annotations-actions/native-function-action.ts`: Function capability を参照して `batch_size` を設定できる UI フックを追加し、既定値は 1。
#### Measurement & experiment setup
- Job 8（20 frames）と Job 15（200 frames 仮定）を用意し、`scripts/sam2/benchmark_tracker.py --batch-sizes 1 8 16 32 --warmup 1` で測定。
- `uv run python scripts/sam2/benchmark_tracker.py --server http://localhost:8080 --host-header 192.168.10.190 --username admin --password admin --job <id> --function 6 --track <track_id> --batch-sizes 1 8 16 32 --output tasks/sam2_tracker_batch_measurements_20251120_job<id>.json`
- 記録テンプレート:

| job | batch_size | AnnotationRequests | Run wall clock [s] | Avg track / frame [ms] | GPU util avg [%] | Peak VRAM [GB] | run_id |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 8 | 1 / 8 / 16 / 32 | TBD | TBD | TBD | TBD | TBD | TBD |
| 15 | 1 / 8 / 16 / 32 | TBD | TBD | TBD | TBD | TBD | TBD |

### Tracker batching API/Agent デザイン概要
#### REST payload 拡張
```jsonc
POST /api/jobs/{job_id}/functions/{function_id}/tracker-actions
{
  "frame": 0,
  "target_frame": 63,
  "track_ids": [27],
  "batch_size": 16,        // optional: サーバー側で `_resolve_tracker_batch_size` により clamp
  "frames": [0, 1, 2, ..., 63] // optional: start/end を含む strictly increasing list
}
```
- サーバー→Agent へ渡す `AnnotationRequest.parameters` は `{"frame": start, "frames": [...], "pending_frames": [...], "states": [...], "tracking_targets": [...]}` を必須化（`pending_frames` は backend 内部で維持）。
- `init_tracking` 完了時は `frames` を 1 chunk（<= batch_size）に区切って track AR を作成。agent は `frames` が配列で来ることを前提にする。

#### Agent 実行フロー
1. `run_tracker_action()` で `frames` を受け取り、`TaskDataset` を 1 回構築。
2. chunk ごとに `for frame in frames:` を回し、`sample = dataset.get_sample(frame)` → `model.track_step(state, image)` を連続実行。
3. `SAM2_TRACKER_VERBOSE` で `{"phase":"track","batch_frames":[...],"frame_latencies_ms":[...],"dataset_reuse":true}` を出力。
4. レスポンス payload:
   ```json
   {
     "states": [...],              // 次チャンク用 state メタ
     "frame_results": [            // 新規
       {"frame": 1, "shapes": [...], "outside": false},
       ...
     ]
   }
   ```
5. サーバー側は `frame_results` をそのまま `_apply_tracking_results` の diff builder に渡し、複数フレーム更新を 1 リクエストで完結させる。

#### バリデーションと後方互換
- `batch_size`・`frames` が未指定の場合は `frames=[frame, frame+1, ..., target_frame]` を自動生成して互換性維持。
- `frames` が `target_frame` を超える場合や非昇順の場合は 400 を返却。
- Agent は `frames` が無い古いサーバーとは通信しない前提で、CLI フラグ `--tracker-require-frames` で早期に失敗させる。
- `_enqueue_track_request` 内の `pending_frames` は配列サイズ縮小のみ行い、既存の `states` / `tracking_targets` との整合性を保つ。

### 2. Chunk preload hardening
#### Implementation checklist
- [ ] `cvat-sdk/cvat_sdk/datasets/common.py`: `ChunkCacheMode` を追加し、`PREFETCH_CHUNKS_ONCE` と `FETCH_ON_DEMAND` を切替可能にする。
- [ ] `cvat-sdk/cvat_sdk/datasets/task_dataset.py`: `TaskDataset` 初期化時に `tracker_allow_chunk_preload` を読み取り、`MediaDownloadPolicy` を切替。`TaskDatasetCache` を LRU で制御できるよう `max_cache_tasks_with_chunks` を設定。
- [ ] `cvat-cli/_internal/agent.py`: CLI フラグ `--tracker-preload-chunks` を既存実装からリファクタし、Interactor と共有する `_DatasetRepositoryBase` を流用。cache ヒット率を `SAM2_TRACKER_VERBOSE` に出力。
- [ ] `ai-models/tracker/sam2/func.py`: inference ループ前後に `cache_hit_ratio`, `chunk_download_ms` を INFO ログ化し、Prometheus exporter が scrape できるメトリクスを `prometheus_client.Gauge` で publish。
#### Progress note (2025-11-21)
- ChunkCacheMode の仕様ドラフトと、Job30 を用いたベンチ計測プロトコル草案を作成開始。`tasks/sam2_tracker_dataset_fetch_20251121_plan.md` にキャッシュヒット率算出式とログレイアウトを記述中。
- `--include-fetch-metrics` で取得する JSON schema を決めるため、`dataset_fetch` レコードのダンプ例を準備し、agent ログと突合できるフィールド一覧を整理している。
  - 目標: 次回更新で schema, 手順, 想定ログのサンプルを本節へ反映し、実測へ進める。
- ChunkCache 共有キャッシュの key/entry/eviction 方針と `SAM2_TRACKER_VERBOSE` の `dataset_fetch` ログ仕様を `tasks/sam2_tracker_dataset_fetch_20251121_plan.md` に追記済み。CLI で `--include-fetch-metrics` を指定した際は同ログを解析し、`chunk_ids`, `download_ms`, `decode_ms`, `hit_ratio`, `cache_stats` を JSON に格納する。Job8 を用いたログ検証 checklist も同ファイルに記載し、`scripts/sam2/check_dataset_fetch_logs.py` を使った突合フローを整備した。
- `scripts/sam2/benchmark_tracker.py` に `--tracker-preload-chunks` / `--include-fetch-metrics` フラグと agent ログ収集機能を追加。Run ごとに `logs/sam2_tracker/sam2_tracker_run_<run_id>.log` を保存し、`dataset_fetch` + `cache_stats` を測定 JSON へ埋め込むようになった。
- Job1 (20f) で `--tracker-preload-chunks --include-fetch-metrics` / `--no-tracker-preload-chunks` を実行し、`tasks/sam2_tracker_dataset_fetch_20251121_job1*.json` を取得。`SAM2_TRACKER_LOG phase="dataset_fetch"` から per-frame fetch ms / chunk_id を記録し、`check_dataset_fetch_logs.py` も `matched 19/19 frames` でパス（chunk reuse 判定は今後の課題）。集計サマリは `tasks/sam2_tracker_dataset_fetch_20251121_summary.md` へ追記済み。
- `docker-compose.yml` の `sam2-tracker-agent` サービスに `SAM2_TRACKER_VERBOSE=${SAM2_TRACKER_VERBOSE:-0}` を追加し、`SAM2_TRACKER_VERBOSE=1 docker compose ... up -d sam2-tracker-agent` で verbose ログを有効化できるようにした。
- Job30（alias、実 ID: task=4/job=4, 1000 frames）を `tmp/sam2_job1000` のシンセティック PNG で新規作成し、frame0 polygon（track id=9）を投入。batch16/repeat5 で `--tracker-preload-chunks` 有無の計測を実施し、`tasks/sam2_tracker_dataset_fetch_20251121_job30_preload.json` / `_job30_nopreload.json` を取得。各 run のログは `scripts/sam2/check_dataset_fetch_logs.py` で 999/999 frame 突合済み。
  - preload ON/OFF いずれも `avg_fetch_ms≈24` / `hit_ratio=0` / `cache_hits=0` で差分なし。agent ログの `chunk_preload_enabled` も常に false であるため、ChunkCacheMode 実装が未配線であることを確認。`tasks/sam2_tracker_dataset_fetch_20251121_summary.md` に Job30 サマリを追加済み。
  - `cvat-sdk`/`cvat-cli` に `ChunkCacheMode` を配線後、`sam2-tracker-agent` を再ビルドして `SAM2_TRACKER_EXTRA_AGENT_ARGS="--tracker-preload-chunks --include-fetch-metrics"` で再起動。Job30 を再計測し、`tasks/sam2_tracker_dataset_fetch_20251121_job30_preload_retry.json` で `chunk_preload_enabled=true` / `cache_hits=999` / `avg_fetch_ms≈1.04 ms` / `hit_ratio=1.0` を確認。続けて preload OFF 版 (`tasks/sam2_tracker_dataset_fetch_20251121_job30_nopreload_retry.json`) も取得し、`avg_fetch_ms≈25 ms` / `hit_ratio=0` を対照として確保。双方とも `check_dataset_fetch_logs.py` で 999/999 frame 突合済み。Experiment 2 scope は完了。|
#### ChunkCache design memo (2025-11-21 初稿)
- `ChunkCacheMode = Enum('ChunkCacheMode', 'FETCH_ON_DEMAND PREFETCH_CHUNKS_ONCE')` を SDK へ追加し、Tracker CLI の `--tracker-preload-chunks`（既定 off）/`--no-tracker-preload-chunks` で切替。Interactor と共有する `_DatasetRepositoryBase` から mode を注入する。
- 共有キャッシュは `(task_id, chunk_id, media_type)` を key にし、`ChunkCacheEntry(dataclass)` が `frame_indices`, `payload`, `cost_bytes`, `created_at`, `last_used`, `hit_count` を保持。`frame_indices` を保持することで、`dataset_fetch` 計測時に chunk dump と frame 単位ログを突合できる。
- API:
  - `get(key) -> Optional[ChunkCacheEntry]`: thread-safe に `hit_count` と `last_used` を更新。
  - `put(entry, *, allow_replace=False)`: `cost_bytes > max_ram_bytes` の entry を拒否。LRU eviction (`heapq` + `last_used`) を行い、`evictions_total` メトリクスをインクリメント。
  - `release_task(task_id)` / `drop_all()` / `stats()` を公開し、Job 完了時や OOM 回避時に確実に掃除できる。
- `ChunkCache(max_ram_mb=_env('SAM2_TRACKER_CACHE_MAX_GB', 4*1024))` を `cvat_cli/_internal/dataset_cache.py` に常駐させ、`TaskDataset(..., dataset_cache=global_cache, chunk_cache_mode=ChunkCacheMode.PREFETCH_CHUNKS_ONCE)` で注入。`TaskDataset` は `MediaDownloadPolicy.PREFETCH_CHUNKS_ONCE` 時に chunk download→decode→`cache.put()` を 1 pipeline にまとめる。
- Telemetry:
  - Prometheus exporter で `tracker_chunk_cache_hits_total`, `tracker_chunk_cache_misses_total`, `tracker_chunk_cache_evictions_total`, `tracker_chunk_cache_bytes`.
  - `SAM2_TRACKER_VERBOSE` に `{"phase":"dataset_cache","event":"put","chunk_id":12,"cost_bytes":1048576,"bytes_used":...}`, `{"phase":"dataset_cache","event":"evict","reason":"lru"}` を出力し、`benchmark_tracker.py --include-fetch-metrics` が `cache_stats` へ取り込む。
- フェイルセーフ: `aiohttp.ClientPayloadError` や `TimeoutError` を捕捉した際は `cache.invalidate(chunk_id)` を行い、壊れた payload を残さない。`psutil.virtual_memory()` の 60 s 移動平均が 80% を超えた場合は警告を出し、`ChunkCache.drop_all()` の fallback を CLI から呼べるようにする。

#### Fetch metrics schema & CLI flow
- 計測 JSON (`tasks/sam2_tracker_dataset_fetch_*.json`) に run ごとの `dataset_fetch` レコード（`frames`, `chunk_ids`, `cached`, `download_ms`, `decode_ms`, `per_frame_fetch_ms`, `avg_fetch_ms`, `hit_ratio`, `log_missing`）と `cache_stats`（`prefetch_enabled`, `cache_bytes_peak`, `cache_hits`, `cache_misses`, `evictions`）を格納。`chunk_cache_mode` は root-level で `FETCH_ON_DEMAND` / `PREFETCH_CHUNKS_ONCE` を記録。
- CLI フロー（`benchmark_tracker.py`）:
  1. Run 提出後に `docker compose logs sam2-tracker-agent --since <submitted_at>` を収集し、`SAM2_TRACKER_VERBOSE` の JSON 行を `logs/sam2_tracker/sam2_tracker_run_<run>.log` として保存。
  2. `phase=="dataset_fetch"` / `phase=="dataset_cache"` を抽出し、chunk 粒度 metrics を構築。
  3. `--include-fetch-metrics` 指定時のみ `dataset_fetch` を measurement JSON に埋め込み。`--no-tracker-preload-chunks` でも測定できるが、`--tracker-preload-chunks` 未指定との混同を避けるため CLI usage で両フラグの同時使用可否を明記。
  4. `scripts/sam2/check_dataset_fetch_logs.py --json ... --log ...` で突合。Job1 (20f) で 19/19 frame の一致を確認済みで、`tasks/sam2_tracker_dataset_fetch_20251121_summary.md` に結果を貼付。
- サンプル JSON schema は `tasks/sam2_tracker_dataset_fetch_20251121_plan.md` に記載。`benchmark_tracker.py` の出力 (`tasks/sam2_tracker_dataset_fetch_20251121_job1*.json`) が同 schema に沿っていることを確認した。

#### Measurement & experiment setup
- `job=30`（1,000 frames）を synthetic で再生成し、preload 有無で 5 回ずつ計測。`chunk_cache_mode` と `hit_ratio` が measurement JSON に含まれることを必須条件にする。
- 実行例:
  ```bash
  UV_HTTP_TIMEOUT=120 uv run python scripts/sam2/benchmark_tracker.py \
    --server http://localhost:8080 --host-header 192.168.10.190 \
    --username admin --password admin \
    --job 30 --function 6 --track <track_id> \
    --batch-sizes 16 --repeat 5 \
    --tracker-preload-chunks --include-fetch-metrics \
    --output tasks/sam2_tracker_dataset_fetch_20251121_job30_preload.json
  ```
  非 preload 版は `--no-tracker-preload-chunks --output ..._nopreload.json`。
- 収集物:
  - 測定 JSON + agent ログ（`logs/sam2_tracker_dataset_fetch_20251121_job30*.log`）。
  - Prometheus 指標: `tracker_chunk_cache_hits_total`, `tracker_chunk_cache_misses_total`, `tracker_chunk_cache_evictions_total`, `tracker_chunk_download_seconds_sum`.
- 解析:
  1. `scripts/sam2/check_dataset_fetch_logs.py` でログ一致を確認。
  2. `tasks/sam2_tracker_dataset_fetch_20251121_summary.md` に `avg_fetch_ms`, `p95_fetch_ms`, `hit_ratio`, `cache_bytes_peak` を追記。
  3. 差分（preload vs nopreload）の `dataset_fetch` グラフを貼り、Job8 で観測した 0.27 s/フレームのうち I/O 部分がどこまで減るかを説明する。

### 3. Run status endpoint refactor
#### Implementation checklist
- [ ] `cvat/apps/functions/models.py`: `FunctionRunSummary`（1:1, `progress: Decimal`, `active_requests: IntegerField`, `last_request_updated: DateTimeField`）のモデルを追加。
- [ ] Migration (`cvat/apps/functions/migrations/00xx_function_run_summary.py`): summary テーブル作成 + `FunctionRun` の `updated_date` の index (`idx_functionrun_updated`) を追加。
- [ ] `cvat/apps/functions/views.py`: `FunctionRunStatusView` で `select_related('summary')` を使い、JSONB から値を引かない新シリアライザを使う。
- [ ] `cvat/apps/functions/serializers.py`: `FunctionRunSerializer` を分離し、summary ベースのレスポンスへ移行する（旧 JSONB 依存のレスポンスは廃止）。
#### Data flow & migration notes
- Backfill 手順: migration 内で `RunSummaryBackfill` データマイグレーションを作成し、既存の `FunctionRun` を 1,000 件単位で走査 → `jsonb` フィールド `data` から `progress`, `active_request_id`, `failed_request_id`, `total_requests`, `completed_requests` を抽出し `FunctionRunSummary` に挿入。CPU 負荷を抑えるため、`explain analyze` で実行計画を確認しながら `SET LOCAL work_mem='256MB'` を指定。
- リアルタイム更新: `cvat/apps/functions/services.py`（`update_function_run` 系）か `handle_request_completion` から summary を更新。`FunctionRun` の `save(update_fields=[...])` に合わせて `FunctionRunSummary.objects.update_or_create(...)` を呼び、`updated_at` を同期。`transaction.on_commit` で非同期更新して UI へのレスポンス遅延を避ける案も検討。
- API スキーマ互換: `FunctionRunStatusSerializer` を `FunctionRunStatusLegacySerializer`（jsonb 使用）と `FunctionRunStatusSummarySerializer` に分割し、`settings.FUNCTIONS_RUN_STATUS_MODE` で切替。デプロイ段階では `legacy` → `dual`（両方更新）→ `summary` の 3 ステップを踏む。
- プロファイリング: `scripts/sql/profile_run_status.sql` を用意し、`\set run_id '...'` 式で指定した ID の `FunctionRunStatusView` 内 SQL を `EXPLAIN (ANALYZE,BUFFERS,TIMING)` で採取できるようにする。before/after の出力を `tasks/sam2_tracker_bottleneck.md` に貼付してリグレッションを可視化。
#### Benchmark plan
- DB 側で `explain (analyze, buffers)` を取得する helper (`scripts/sql/profile_run_status.sql`) を作り、before/after で比較。
- HTTP 計測: `wrk -t4 -c8 -d30s --latency http://localhost:8080/api/functions/runs/<id>` を `docker compose ... run cvat_server` から実行。
- 成果目標: median < 200 ms, p99 < 500 ms, SQL 実行数 3 以下。

### 4. `_apply_tracking_results` diff mode
#### Implementation checklist
- [ ] `cvat/apps/functions/tracking_diff.py`: `ShapeDigest = NamedTuple('ShapeDigest', id, frame, outside, z_order, points_hash)` を定義し、`build_diff(current: list[ShapeDigest], incoming: list[ShapeDigest]) -> DiffResult` を実装。
- [ ] `cvat/apps/functions/tracking.py`: `_apply_tracking_results` に `mode="bulk"|"diff"` を追加。`diff` では `DiffResult` から `delete/update/insert` を最小限に行い、`handle_annotations_change` 呼び出し回数を 1 回へ抑える。
- [ ] `cvat/apps/functions/tests/test_tracking_apply.py`: `test_apply_tracking_results_noop_is_noop`, `test_apply_tracking_results_single_frame_update`, `test_apply_tracking_results_outside_extension` を追加。
- [ ] `SAM2_TRACKER_VERBOSE`: `{"phase":"apply_diff","frame_count":N,"added":x,"updated":y,"deleted":z,"elapsed_ms":...}` をログ出力。
#### Experiment plan
- Job 8/30 で `diff` モードと既存モードを比較し、`curl -w '%{time_total}' -o /dev/null -X GET http://localhost:8080/api/jobs/<job>/annotations` を run 完了直後に 5 回計測。
- DB `EXPLAIN ANALYZE` で delete/insert 件数、ロック待ち時間を記録。
- 期待値: Job 8 で apply < 0.8 s、Job 30 で < 3 s。

### 5. Observability & regression safety net
#### Implementation checklist
- [ ] `ai-models/tracker/sam2/func.py`: `cuda_event_pair()` helper を作り、`SAM2_TRACKER_VERBOSE` ログに `tracker_cuda_ms`, `preprocess_ms`, `postprocess_ms` を書く。
- [ ] `tests/python/tracker/test_tracker_bottlenecks.py`: `@pytest.mark.run_bottleneck` テストを追加し、`TrackerRunHarness(job_id, batch_size, diff_mode)` で指定された設定が数秒以内に完走することを検証（CI では skip）。
- [ ] `dev/docker-compose.cuda.yml`: GPU ノードで smoke テストを回す compose を作成し、`tests/python/tracker/test_tracker_bottlenecks.py::test_job8_batch16` のみを実行するターゲットを追加。
- [ ] `tasks/sam2_tracker_bottleneck.md`: 計測ログの JSON schema とアップロード先（`tasks/sam2_tracker_batch_measurements_YYYYMMDD.json`）の命名規則を明文化済みか確認し、不足があれば更新。
#### Experiment plan
- `nvidia-smi dmon -s u` をバックグラウンドで記録しつつ、`uv run python scripts/sam2/benchmark_tracker.py --job 8 --function 6 --batch-sizes 1 16 --diff-apply` を実行。GPU 使用率ログを `logs/sam2_tracker_gpu_20251120.csv` に保存。
- テストパイプライン:
  ```bash
  UV_HTTP_TIMEOUT=120 uv sync --group dev --group test --python 3.10
  UV_HTTP_TIMEOUT=120 uv run python -m pytest tests/python/tracker/test_tracker_bottlenecks.py --run-bottleneck -k job8_batch16
  ```
- 成果物: 計測ログを本ファイルに貼り付け、回帰チェックの閾値（例: job8 batch16 の track 平均 < 70 ms）を追記。

### XXXX225-02 前処理ボトルネック改善案（検討メモ）
- 現状: `preprocess_image()` が decode+resize+正規化に加え `predictor.forward_image`（vision backbone）まで含み、1080p では fast-preprocess ON でも ~15 ms/frame、warmup では chunk0 が支配的。`SAM2_TRACKER_ASYNC_PREPROCESS` でも `track` 直前で `wait_event` を入れるためフレーム間オーバーラップが無い。
- 実装着手: `_extract_encoded_bytes` を補強し、`_encoded_bytes` が無い場合もメモリ上で再エンコードして GPU デコード経路を継続利用するように変更（CPU Transform フォールバックを極力防止）。`SAM2_TRACKER_WARMUP_FRAMES`（デフォルト1）を導入し、起動時に dummy フレームで preprocess/track/torch.compile を走らせる。ウォームアップ失敗時は warn のみ。
- Warmup ログ: `warmup_preprocess`/`warmup_track` を `SAM2_TRACKER_VERBOSE` で計測し、起動時ウォームアップの所要時間とスパイク有無を確認できるようにした。
- Warmup 完了ログに `warmup_wall_ms` を追加。今後の agent 起動ログから burn-in 所要時間を直接読める。
- GPU デコードの堅牢化: `_extract_encoded_bytes` 失敗時に CPU Transform へフォールバックする。`TaskDataset._image_from_encoded` 同等に `_encoded_bytes` を確実に付与するローダへ統一し、フォールバック発生時は 1 回だけ warn。fast-preprocess を既定 ON か capability/env で明示 opt-in。
- オーバーラップ案（ダブルバッファ）: `pp_prev/pp_next` を持ち、frame N の `track` 実行中に別 CUDA stream で frame N+1 の `forward_image` をキック → `track` 冒頭で前フレームの preprocess Event を wait する形に変更し decode+backbone を隠す。
- Warmup 専用フック: torch.compile / fast-preprocess の初期 JIT を agent 起動時に 1–2 frame だけ走らせ、chunk0 スパイク（17 s クラス）を潰す。`SAM2_TRACKER_VERBOSE=0` でも burn-in する。
- Decoder/normalize 最適化: `torchvision.io.decode_jpeg` / nvJPEG 検証、`channels_last=True` 適用、`torchvision.transforms.v2.functional.resize` で resize+normalize を fused。decode+resize で 2–5 ms/f 削減余地。
- mask→形状変換の枝刈り: polygon 返却時の `findContours`/`approxPolyDP` が Python 側で ~数 ms を占めるため、mask のまま返す fast-path や面積閾値 skip を準備。
- Next step: 上記 3 点（ダブルバッファ、warmup、fast-preprocess 安定化）をパッチにし XXXX225-02 500f を再計測、`preprocess` p50/p95 と chunk0 の wall clock 変化を記録する。
- 追加メモ（warmup オフ再計測）: `SAM2_TRACKER_WARMUP_FRAMES=0` で再ビルド・再起動後の実験では init が 107 s, chunk0 37 s、track 平均 126 ms/f に劣化（run `a697254c-...`）。autotune/compile が初回 AR に集中しているため、safe モードでも事前コンパイル/キャッシュ持ち込み、もしくはバックグラウンド compile を例外なく完了させる仕組みが必要。
- cudagraph 無効化（`TORCHINDUCTOR_USE_CUDAGRAPHS=0` 等）でも初回 compile は改善せず、init 146 s / chunk0 41 s（run `3f98c8eb-...`）。torch.compile/autotune の走り先を初回 AR に載せない（起動時軽量プリコンパイル＋完了ログ）か、事前ビルド済みキャッシュを読み込む方針を検討する。

### 計測準備ログ（2025-11-20 AM）
- `scripts/sam2/benchmark_tracker.py` に `--plan-only` と `--repeat`（デフォルト1）を追加し、RESTコール無しで AnnotationRequest パターンを出力できるようにした。`repeat_index` はバッチサイズごとの反復番号を示す。
- Job 8 / Job 9 で `--batch-sizes 1 8 16 32 --repeat 3 --plan-only` を実行し、以下に保存。
  - `tasks/sam2_tracker_batch_measurements_20251120_job8_plan.json`
  - `tasks/sam2_tracker_batch_measurements_20251120_job9_plan.json`
- `plan_only=true` では `total_frames`, `track_chunks`, `chunks[].frames` だけが含まれ、Agent/DB を叩かない。`batch_size=16` の場合:

| job | total_frames | track_chunks | chunk分割例 |
| --- | --- | --- | --- |
| 8 | 19 | 2 | `[1..16]`, `[17..19]` |
| 9 | 199 | 13 | `[1..16]`×12 + `[193..199]` |

これをベースに、`--plan-only` → 本実行の順で 3 回ずつ計測するフローを確立。

### 実測ログ（2025-11-20 10:00 UTC）
- Job 1（20 frames、track id 1、Function id 3）で `batch_size=16` を 3 回実行。結果は `tasks/sam2_tracker_batch_measurements_20251120_job8.json` に保存。主な数値:

| repeat | init [s] | track chunks | total track [s] | avg per frame [ms] | wall clock [s] | run_id |
| --- | --- | --- | --- | --- | --- | --- |
| 0 | 1.615 | 2 | 1.221 | 64.2 | 2.845 | c74d44ea-fd18-4573-81e0-82c22e8c04e7 |
| 1 | 0.252 | 2 | 1.389 | 73.1 | 1.650 | 46667427-a177-49d2-b98e-4aba4cec5e53 |
| 2 | 0.235 | 2 | 1.175 | 61.9 | 1.419 | 2b1fadba-281b-49f8-9a06-49d2d52fd384 |

  - 初回のみ SAM2 warmup に 1.6 s かかったが、2 回目以降は init 0.25 s 前後。`logs/sql/run_requests_20251120_job8_job9.csv` で AR ごとの duration も記録済み。

- Job 2（200 frames、track id 2、Function id 3）でも `batch_size=16` を 3 回実行。結果は `tasks/sam2_tracker_batch_measurements_20251120_job9.json` に保存。

| repeat | init [s] | track chunks | total track [s] | avg per frame [ms] | wall clock [s] | run_id |
| --- | --- | --- | --- | --- | --- | --- |
| 0 | 1.436 | 13 | 13.025 | 65.5 | 14.746 | 1a37fb82-d2d6-40a8-ac1f-293f79623f7b |
| 1 | 1.195 | 13 | 12.615 | 63.4 | 14.308 | 91a92b83-fd0e-46f2-b137-50c036bb6d7d |
| 2 | 0.480 | 13 | 12.718 | 63.9 | 12.739 | ae37b34c-c0f3-443b-90b3-f086badf5624 |

  - per-frame 60–65 ms で安定。init の揺らぎは SAM2 キャッシュ有無の差と推測。Postgres から抜いた直近 50 件の `functions_annotationrequest` レコードは `logs/sql/run_requests_20251120_job8_job9.csv` に保管。
- 追加で batch size 1 / 8 / 32 を計測し、`tasks/sam2_tracker_batch_measurements_20251120_job9_bs{1,8,32}.json` と SQL (`logs/sql/run_requests_20251120_job9_bs1_8_32.csv`) を取得。サマリ:

| batch | repeats | init range [s] | avg track [ms/frame] | wall clock range [s] | 備考 |
| --- | --- | --- | --- | --- | --- |
| 1 | 3 | 0.43–1.25 | 100–104 | 21.9–22.1 | AR 199件、REST/DBオーバーヘッド支配。 |
| 8 | 3 | 0.11–0.46 | 64–75 | 13.0–15.5 | 初回 chunk（frames 1–8）が 0.30 s/Frame と突出（warmup/IO）。 |
| 16 | 3 | 0.48–1.44 | 61–65 | 12.7–14.7 | 既存測定。 |
| 32 | 3 | 0.10–0.73 | 59–62 | 12.4–13.1 | chunk 7件で REST/DB 負荷最小。 |

  - batch 8 の repeat 0（`eed1be97-...`）で最初の chunk が 2.4 s かかっており、SAM2 warmup + chunk fetch が重なったと分析。repeat 1/2 では 0.5 s 前後まで落ち着くため、Agent で先行 preload を入れる価値が高い。

### 実測ログ（Post-enable, 2025-11-20 14:30 JST）
- 目的: `CVAT_FUNCTION_TRACKER_DEFAULT_BATCH_SIZE=16` 運用・Function capability 恒久化後の baseline を Job8(=task1/job1) / Job9(=task2/job2) で再取得。
- コマンド例:
  ```bash
  UV_HTTP_TIMEOUT=120 uv run python scripts/sam2/benchmark_tracker.py \
    --server http://localhost:8080 --host-header 192.168.10.190 \
    --username admin --password admin \
    --job 1 --function 3 --track 1 --start-frame 0 --target-frame 19 \
    --batch-sizes 1 8 16 32 --repeat 3 \
    --compose-cmd "docker compose -f docker-compose.yml -f docker-compose.dev.yml" \
    --output tasks/sam2_tracker_batch_measurements_20251120_job8_postenable.json
  ```
  Job9（200 frames）は `--job 2 --track 2 --target-frame 199 --output ...job9_postenable.json`。
- SQL raw: `logs/sql/run_requests_20251120_job8_postenable.csv`, `logs/sql/run_requests_20251120_job9_postenable.csv`。

| Job | batch | Avg track [ms/frame] | Wall clock [s] | Init [s] | Avg chunks |
| --- | --- | --- | --- | --- | --- |
| 8 (20f) | 1 | 108.8 | 2.74 | 0.57 | 19.0 |
| 8 (20f) | 8 | 82.8 | 2.53 | 0.94 | 3.0 |
| 8 (20f) | 16 | 71.3 | 1.86 | 0.50 | 2.0 |
| 8 (20f) | 32 | 63.9 | 1.58 | 0.36 | 1.0 |
| 9 (200f) | 1 | 111.8 | 23.79 | 0.74 | 199.0 |
| 9 (200f) | 8 | 70.3 | 14.83 | 0.74 | 25.0 |
| 9 (200f) | 16 | 67.7 | 14.17 | 0.64 | 13.0 |
| 9 (200f) | 32 | 66.4 | 13.61 | 0.36 | 7.0 |

- 所感:
  - batch16/32 では per-frame 65–71 ms と目標（≤70 ms）を概ね達成。Job9 でも AnnotationRequest 件数が 199 → 13/7 まで圧縮され、wall clock も 13–14 s で安定。
  - batch1 との比較で Job9 の wall clock は 23.8 s → 13.6 s（~43% 短縮）。init 時間も 0.74 s → 0.36 s まで改善。
  - Job8 はフレーム数が少ないため wall clock 差分は 1 s 程度だが、chunk1 回化により run_id ごとの wait time が 40% 近く減少。

## W47 Thu PM Execution Block (2025-11-20)
### 完了条件
- Job 8 / Job 9 について `batch_size=16` でエージェント → backend → DB の一連の遅延を 3 回ずつ再計測し、ログと psql 出力を `tasks/` 以下へ保存。
- `tracker-actions` API 拡張のシリアライザ仕様と agent ループ pseudo-code を `adr-sam2-tracker-batching` に転記できるレベルまで文章化。
- Run status refactor と diff apply の PoC 方針をレビュー依頼できるよう、テーブル定義と migration 影響（downtime, lock 範囲）を整理。

### 作業順序（想定 13:30–18:00）
| 時刻帯 | Stream | 具体アクション | 主なアウトプット |
| --- | --- | --- | --- |
| 13:30–14:00 | Tracker batching | `benchmark_tracker.py --plan-only` 実装 → `TrackerActionSerializer` 下書き | PRドラフト用の diff と curl 検証ログ |
| 14:00–15:30 | Tracker batching | Job 8/9 で `batch_size=16` の plan → 実行計測 (3 回) | `tasks/sam2_tracker_batch_measurements_20251120_job{8,9}.json`, `logs/sql/...` |
| 15:30–16:30 | Dataset streaming | `ChunkCache` 設計と fetch メトリクス仕様整理 | `tasks/sam2_tracker_dataset_fetch_20251120.json`, 設計メモ |
| 16:30–17:15 | Run status refactor | Summary テーブルDDL + migrationロック検討 + `profile_run_status.sql` 雛形 | DDLメモ, SQLスクリプト |
| 17:15–18:00 | Diff apply & Observability | FrameDigest仕様、`SAM2_TRACKER_VERBOSE` ログサンプル、pytestスケルトン | Doc追記, テストファイル雛形 TODO |

### 詳細タスク（参照用）
#### A. Tracker batching API + Agent
- 客観的な API 差分をまとめるため、`diff <(python -m json.tool payload_old.json) <(python -m json.tool payload_new.json)` を実行して図示する。`payload_new.json` は `frames` を 0-based, strictly increasing、`batch_size` を 1–64 clamp と明記。
- Backend 側では `Function.supports_batched_tracker`（デフォルト false）を導入済み。Serializer で capability を参照し、flag OFF の Function に対する `batch_size>1` / `frames` 指定は 400 を返す。
- Agent pseudo-code:
  ```
  dataset = TaskDataset(...)
  chunks = chunk_frames(frames, batch_size)
  for chunk in chunks:
      images = dataset.load_images(chunk)
      tracker.feed(chunk, images)
      flush_results(chunk)
  ```
  `dataset.load_images` は `asyncio.gather` で 4 並列 fetch を行い、`MediaDownloadPolicy` を `PREFETCH_CHUNKS_ONCE` に切替える。
- ログ例: `{"phase":"track","frames":[0,1,...,15],"dataset_reused":true,"frame_latencies_ms":[58,63,...],"cuda_stream":"default","peak_vram_mb":4231}`。UI 側で計測結果を拾えるよう JSON の key を固定。
- バックエンドでは `TrackerActionSerializer.validate` で `frames` が `target_frame` と矛盾した場合に 400 を返すバリデーションを追加。`supports_batched_tracker` false 時は `ValidationError("Batch tracking is not supported by this function.")` を即返す。

#### B. Dataset streaming / caching
- `cvat-cli/src/cvat_cli/_internal/dataset_cache.py`（新設予定）に `ChunkCache(max_ram_mb=4096)` を導入し、`TaskDataset` init 時に `dataset_cache=global_cache` を注入する方針を文章化。
- `scripts/sam2/benchmark_tracker.py --plan-only --include-fetch-metrics` が `dataset.fetch_ms` を表示するよう計測点を足し、Job 9 での I/O 分布をヒストグラム化（`numpy.histogram`）する。結果は `tasks/sam2_tracker_dataset_fetch_20251120.json` に保存。
- 失敗パターン: `aiohttp.ClientPayloadError` 発生時に chunk キャッシュを破棄しないと破損データが残るため、`finally: cache.invalidate(chunk_id)` を差し込む。これを ADR にも記述する。
- **ChunkCache 詳細案**:
  - API: `get(frame_index, *, chunksize) -> bytes | None`, `put(frame_index, payload, *, cost_bytes)`, `release(task_id)`、`stats()`。
  - Key は `(task_id, chunk_id)`。`chunk_id = frame_index // chunk_size`。画像ジョブは zip chunk、動画ジョブはデコード済みフレームを bytearray で保持し `numpy.ndarray` へ lazy 変換。
  - 容量制御: `max_ram_mb` を env (`SAM2_TRACKER_CACHE_MAX_GB`) から決定し、`heapq` で `last_access_ts` を管理。上限超過時に LRU でエヴィクトし、`evictions_total` メトリクスを加算。
  - 共有: `TaskDataset` は `dataset_cache: Optional[ChunkCache]` を受け取り、`MediaDownloadPolicy.PREFETCH_CHUNKS_ONCE` 時は `asyncio` で `cache.put`。Interactor と Tracker で同一実装を再利用するため `cvat_cli/_internal/cache.py` に配置。
  - スレッド安全性: Agent は `ThreadPoolExecutor` から fetch するので `ChunkCache` の `get/put` には `threading.Lock` を用意。`task_id` ごとの参照カウンタを持ち `release(task_id)` で chunk を掃除。
  - 実装 TODO: `cvat-cli/src/cvat_cli/_internal/dataset_cache.py` に `class ChunkCache:` と `class CacheEntry(NamedTuple)` を置き、`global_cache = ChunkCache(max_ram_mb=_env("SAM2_TRACKER_CACHE_MAX_GB", 4*1024))` を初期化。`TaskDataset.__init__` で `self._cache = dataset_cache` を受け取れるようシグネチャを広げる。
  - `TaskDataset.get_sample(frame_index)` は `cache.get(frame_index)` ヒット時に `Sample` を構築せず、`media_loader` に直接 cached chunk を渡せるよう `MediaDownloader` に `from_bytes()` API を追加する必要あり。これを ADR 化して UI/agent の互換性を説明する。
- **Fetch metrics & telemetry**:
  - CLI 側で `dataset.fetch_ms` を計測し、`SAM2_TRACKER_VERBOSE` ログに `{"phase":"dataset_fetch","chunk_id":12,"cached":true,"elapsed_ms":5.2}` を追加。
  - `scripts/sam2/benchmark_tracker.py --include-fetch-metrics` では各 chunk の `cached_hit` / `download_ms` / `decode_ms` を集計して JSON に保存。Job2(200f) で 3 run、Job??(1000f) で 1 run を想定。
  - Prometheus exporter: `sam2_tracker_cache_hits_total`, `sam2_tracker_cache_misses_total`, `sam2_tracker_prefetch_queue_depth` を agent ログから抽出する仕組みを追加（短期的には CSV ログで代替）。

#### C. Run status + diff apply
- `FunctionRunSummary` の列: `run = OneToOneField(FunctionRun, primary_key=True)`, `progress = DecimalField(max_digits=5, decimal_places=2)`, `active_requests = IntegerField(default=0)`, `last_request_updated = DateTimeField(null=True)`, `completed_requests = IntegerField(default=0)`, `failed_requests = IntegerField(default=0)`。
- Migration 影響: `FunctionRun` に対して `ALTER TABLE ... ADD COLUMN` は `ACCESS EXCLUSIVE` を要求するため、`CONCURRENTLY` で index を張りつつ、本番では `--cluster=... --noinput` でロールアウトする手順をまとめる。
- `_apply_tracking_results` の diff PoC では `FrameDigest` (`frame:int`, `hash:str`, `outside:bool`) の比較を Python 側で実施し、差分ゼロの場合は `handle_annotations_change` を skip。`pytest` では `assert query_count <= baseline` を `django_assert_num_queries` で担保する。

### 計測・実験ログ化
- `scripts/sam2/benchmark_tracker.py` 実行結果はすべて `tasks/sam2_tracker_batch_measurements_20251120_<job>.json` に保存し、`git lfs` 対象か確認のうえコミット対象外にする。
- DB プロファイルは `logs/sql/run_status_before_20251120.txt`, `logs/sql/run_status_after_20251120.txt` に分ける。`make logs/sql` のようなターゲットが無いため、`mkdir -p logs/sql` を明記。
- 収集した `nvidia-smi dmon` CSV は `logs/sam2_tracker_gpu_20251120_job8.csv` の形式に統一し、本ファイルからリンクする。

### リスクと対応
- API が後方互換を壊すリスク: capability フラグ OFF で `batch_size` / `frames` を受け取った場合の fallback パスを残す。リリース直後はバッチ無しが既定で UI から opt-in。
- Dataset キャッシュでメモリ不足を起こすリスク: `SAM2_TRACKER_CACHE_MAX_GB` env を導入し、`psutil.virtual_memory()` で headroom を監視。過去 60 s の平均 RAM 使用率が 80% 超過したら cache を一括破棄する。
- Run status summary migration はサービス停止が必要になる可能性があるため、`rolling flag` を導入して `summary` への書込みだけ先に有効化し、読み出しを段階的に切替える計画を策定。
  - CLI 拡張: `--include-fetch-metrics` で `results[].chunks[].dataset_fetch` を返す。構造は `{"frames":[...],"per_frame_fetch_ms":[...],"cache_hits":[...],"cache_misses":[...],"avg_fetch_ms":...}`。
  - `--plan-only` と併用時は実測しないため、`--include-fetch-metrics` は非対応（エラー）とする。実測時のみ agent ログから `SAM2_TRACKER_VERBOSE` 行を抽出→JSON へマージ。
  - 取得方法: `benchmark_tracker.py` 内で `docker compose logs sam2-tracker-agent --since <submitted_at>` を呼び、`SAM2_TRACKER_LOG` 行を正規表現でパース。`phase=="dataset_fetch"` の `wall_ms` / `cached` / `chunk_id` を `chunk` レコードに集約する。
  - 失敗時フォールバック: ログ取得が 0 行の場合は `dataset_fetch=None` を記録し `stderr` に Warning を出すだけに留める。

### 2025-11-22 CUDAGraphs=ON + warmup 有無の比較 (Job 7 / track 42, 500f)
- 条件: `SAM2_TRACKER_USE_CUDAGRAPHS=1`, batch16 preload, CUDAGraphs有効。
- warmup=1 (`SAM2_TRACKER_WARMUP_FRAMES=1`), Run `f9e1eb01-1ee9-4224-b843-daff0e93599f` → JSON: `tasks/sam2_tracker_job7_500f_cudagraphs1_warmup1.json`, ログ: `logs/sam2_tracker/sam2_tracker_run_f9e1eb01-1ee9-4224-b843-daff0e93599f.log`。
  - `init=1.261 s`, `track=21.915 s` (avg **43.9 ms/frame**, chunk0 0.934 s / 58 ms/f)、`wall=23.433 s`。
  - `dataset_fetch.avg_fetch_ms=8.25`, `hit_ratio=1.0`（プリロード全ヒット）。
- warmup=0 (`SAM2_TRACKER_WARMUP_FRAMES=0`), Run `04176e7e-c6fc-4673-84de-ba393e88f070` → JSON: `tasks/sam2_tracker_job7_500f_cudagraphs1_warmup0.json`, ログ: `logs/sam2_tracker/sam2_tracker_run_04176e7e-c6fc-4673-84de-ba393e88f070.log`。
  - `init=1.081 s`, `track=22.154 s` (avg **44.4 ms/frame**, chunk0 0.878 s / 55 ms/f)、`wall=23.477 s`。
  - `dataset_fetch.avg_fetch_ms=8.58`, `hit_ratio=1.0`。
- 所見: CUDAGraphs を ON にすると初回 compile スパイクが消失し、warmup 0/1 で挙動はほぼ同等（init ≈1 s、track ≈22 s、wall ≈23 s）。500f でも安定して ~44 ms/frame に収束。

### 2025-11-22 CUDAGraphs=OFF (torch.compile継続) + warmup=0 (Job 7 / track 42, 500f)
- 条件: `SAM2_TRACKER_USE_CUDAGRAPHS=0`, `SAM2_TRACKER_WARMUP_FRAMES=0`, batch16 preload。
- Run `fd818465-60fd-4cb6-aa39-c7536b9248f6` → JSON: `tasks/sam2_tracker_job7_500f_cudagraphs0_warmup0.json`, ログ: `logs/sam2_tracker/sam2_tracker_run_fd818465-60fd-4cb6-aa39-c7536b9248f6.log`。
  - `init=0.677 s`, `track=22.496 s` (avg **45.1 ms/frame**, chunk0 0.669 s / 42.8 ms/f)、`wall=23.407 s`。
  - `dataset_fetch.avg_fetch_ms=8.77`, `hit_ratio=1.0`。
- 所見: CUDAGraphs OFF でも初回スパイクは無く、CUDAGraphs=ON (warmup 0/1) と同等の wall ≈23 s / ≈44–45 ms/f で完走。差はごく僅少で、現行条件では CUDAGraphs 有無が決定的な差分にならない。

### 2025-11-22 XXXX225-02 再計測 (500 frames, Job 7 / track 45, batch16 preload)
- `tmp/COPG_task8_track.json` を `PATCH /api/jobs/7/annotations?action=create` で投入し Track 45 を生成（0.126 s）。計測コマンドは下記のとおり。結果 JSON: `tasks/sam2_tracker_COPG225_500_batch16_20251122.json`、ログ: `logs/sam2_tracker/sam2_tracker_run_4edc831d-c669-4f5e-91a4-95c458a9ff7c.log`。
  ```bash
  PYTHONPATH=cvat-cli/src:cvat-sdk UV_HTTP_TIMEOUT=120 \
    uv run python scripts/sam2/benchmark_tracker.py \
      --server http://192.168.10.190:8080 \
      --host-header 192.168.10.190 \
      --username admin --password admin \
      --job 7 --function 3 --track 45 \
      --start-frame 0 --target-frame 499 \
      --batch-sizes 16 \
      --tracker-preload-chunks --include-fetch-metrics \
      --agent-log-dir logs/sam2_tracker \
      --output tasks/sam2_tracker_COPG225_500_batch16_20251122.json
  ```

| Phase | Measurement (wall clock) | Notes |
| --- | --- | --- |
| Annotation GET (`GET /api/jobs/7/annotations`) | **2.40 s** | `logs/http/job7_annotations_get_20251122.json`。500f + 14 tracks 分の payload。 |
| Tracker submission (`POST /tracker-actions`, batch16) | **0.125 s** | `submit_latency`。 |
| `init_tracking` AR | **1.20 s** | `init_duration_s`。 |
| `track` AR (31×16 + 1×3 frames) | **20.27 s total** (avg **40.6 ms/frame**, min 38.0 ms, max 53.8 ms) | `total_track_duration_s` / `avg_track_per_frame_s`。 |
| Run wall clock (init start → last track AR done) | **21.71 s** | `_apply_tracking_results` + status 更新 ≈0.25 s。 |
| Run status GET (post-completion) | **0.103 s** | `logs/http/run_4edc831d_get_20251122.json`。 |
| Dataset fetch (`SAM2_TRACKER_LOG`) | **avg 8.65 ms/frame**, `hit_ratio = 1.0`, `cache_hits = 499`, `cache_misses = 0` | 1080p decode が主成分。 |

- init+track 21.47 s / wall 21.71 s で **≈98.9% が推論 (SAM2 init+track)**。I/O は avg 8.65 ms/f まで縮小し、依然として GPU track_step が支配的。
- per-frame は **40.6 ms** と 11/20 (46 ms/f) からさらに短縮し、500f でも **21–22 s/run** に収束。TorchInductor compile スパイクは再発せず、warmup 1 chunk (torch.compile ON) が安定している。
- Annotation 再取得は 2.4 s まで増加（トラック本数 14 本に起因）。トラッカー完了後の確認待ちは依然 UX の固定コスト。

### 2025-11-22 Prefetch 優先化＋再計測 (500 frames, Job 7 / track 45, batch16 preload)
- 変更: `cvat-cli/_internal/agent.py` の分岐を調整し、`SAM2_TRACKER_PREFETCH_FRAMES=1`（既定）ではフレームプリフェッチ＋ロードをトラッキングとオーバーラップするルートを優先（ダブルバッファを使いたい場合は `SAM2_TRACKER_PREFETCH_FRAMES=0` に切替）。`sam2-tracker-agent` を再ビルド／再起動。
- Run #1 (コンテナ再ビルド直後の初回): run `a3d0d519-598c-49d8-b406-2ecb60c57ff7` → JSON `tasks/sam2_tracker_COPG225_500_batch16_20251122_prefetch.json`。`init=50.38 s`（torch.compile autotune 集中）、`track=41.26 s`（82.7 ms/f）、`wall=91.89 s`。ウォームアップ扱い。
- Run #2 (ウォーム後の本番値): run `f36771a4-fe87-4324-b0d7-90ff59cb569c` → JSON `tasks/sam2_tracker_COPG225_500_batch16_20251122_prefetch_rerun.json`, ログ `logs/sam2_tracker/sam2_tracker_run_f36771a4-fe87-4324-b0d7-90ff59cb569c.log`。
  - submit 0.139 s / init 0.954 s / track 20.08 s (avg **40.2 ms/f**, min 38.6 ms, max 55.6 ms) / apply+status ≈0.26 s → wall 21.30 s。
  - Annotation GET 2.45 s (`logs/http/job7_annotations_get_20251122_prefetch.json`), Run status GET 0.104 s (`logs/http/run_f36771a4_get_20251122.json`)。
  - Dataset fetch: **avg 8.48 ms/f**, `hit_ratio=1.0`, `cache_misses=0`, `cache_bytes_peak≈7.6 MB`。fetch 自体は変わらず decode が主因だが、オーバーラップにより track_step への待ち時間は隠蔽されている。
- 所見: ウォーム後は per-frame ≈40 ms / wall ≈21–22 s で従来水準を維持。コンテナ再ビルド直後は compile スパイクが再発するため、1 run 捨ててから本計測を行うこと。decode 8.5 ms/f は残存ボトルネックで、さらなる短縮には非同期デコード/NVJPEG などが必要。
