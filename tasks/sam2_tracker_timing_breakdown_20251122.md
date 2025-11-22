# SAM2 Tracker timing breakdown (Job1 / Job7 / Job30, 2025-11-22)

P1–P2 系の最適化（`_load_tracker_image` 簡素化 + `TaskDataset` lazy decode + ChunkCacheMode + tracker prefetch）を適用した後の、代表ジョブごとの処理時間内訳を整理する。

対象はすべて `function=3 (SAM2 Tracker)`, `batch_size=16`, `--tracker-preload-chunks`, `--include-fetch-metrics` の条件とする。

## 1. 測定に使ったエントリ

- Job 1（20 frames, task_id=1/job_id=1, track=1）
  - 測定 JSON: `tasks/sam2_tracker_job1_fetch_after_p2_3.json`
  - エージェントログ: `logs/sam2_tracker/sam2_tracker_run_d08f95f1-b914-4091-9165-5f745259ef96.log` ほか
- Job 7（500 frames, XXXX225-02, job_id=7, track=43）
  - 測定 JSON: `tasks/sam2_tracker_job7_500f_fetch_after_p2_3.json`
  - エージェントログ: `logs/sam2_tracker/sam2_tracker_run_4f2ba431-f957-4b16-8b67-9cc7cd136e15.log` ほか
- Job 30（1000 frames, task_id=4/job_id=4, track=9）
  - 測定 JSON: `tasks/sam2_tracker_job30_fetch_after_p2_3.json`
  - エージェントログ: `logs/sam2_tracker/sam2_tracker_run_b0132a34-575b-4d1c-a5fe-e9377f840d7e.log` ほか

補助ツール:

- CVAT API 経由の AnnotationRequest 統計: `scripts/sam2/benchmark_tracker.py` の `avg_track_per_frame_s` / `dataset_fetch.avg_fetch_ms`。
- Agent 内部の SAM2 フェーズ別統計: `scripts/sam2/analyze_tracker_log.py` (`SAM2_TRACKER_LOG` ベース)。

## 2. Job1 (20 frames, small imageset)

### 2.1 フレーム単位の壁時計（pipeline 全体）

- 測定 JSON: `tasks/sam2_tracker_job1_fetch_after_p2_3.json`
- 平均値（repeat=5 の run 平均）:
  - `dataset_fetch.avg_fetch_ms`:
    - per run: `[0.697, 0.690, 0.662, 0.633, 0.694]`
    - mean: **≈0.68 ms/frame**
  - `avg_track_per_frame_s`:
    - per run: `[35.94, 38.73, 36.76, 36.90, 38.68] ms`
    - mean: **≈37.4 ms/frame**

ここで `avg_track_per_frame_s` は AnnotationRequest の `updated_at - created_at` をフレーム数で割った値であり、server 側キュー処理・DB 書き込み・結果適用を含む「CVAT 全体 pipeline」の 1 フレームあたり壁時計時間を表す。

### 2.2 Agent 内 SAM2 フェーズ（Job1, 1 run の例）

`scripts/sam2/analyze_tracker_log.py logs/sam2_tracker/sam2_tracker_run_d08f95f1-b914-4091-9165-5f745259ef96.log` より（`wall_ms`, overall mean）:

- `dataset_fetch` / `frame_fetch`: **mean ≈ 1.91 ms**（chunk0 1枚＋残り 18 枚, chunk0 スパイクを含む）
- `preprocess`: **mean ≈ 2.30 ms**
- `track_step`:
  - overall mean ≈ **30.8 ms**（warmup を含む）
  - steady (frame_idx >= 16) mean ≈ **18.5 ms**
- その他（概要）:
  - `memory_conditioning` ~1.37 ms / `memory_attention` ~0.51 ms
  - `sam_prompt_encoder` ~0.49 ms / `sam_mask_decoder` ~0.53 ms
  - `track_postprocess` ~0.46 ms / `mask_to_shape` ~0.30 ms

Job1 はフレーム数が少なく warmup の影響が大きいため、`avg_track_per_frame ≈37ms` のうち多くを「初期 chunk の heavy track_step」が占めており、fetch は平均 ~1〜2ms レベルで支配的ではない。

## 3. Job7 (500 frames, XXXX225-02, heavy imageset)

### 3.1 フレーム単位の壁時計（pipeline 全体）

- 測定 JSON: `tasks/sam2_tracker_job7_500f_fetch_after_p2_3.json`
- 平均値（repeat=3 の run 平均）:
  - `dataset_fetch.avg_fetch_ms`:
    - per run: `[3.392, 3.204, 3.326]`
    - mean: **≈3.31 ms/frame**
  - `avg_track_per_frame_s`:
    - per run: `[46.76, 45.85, 46.50] ms`
    - mean: **≈46.4 ms/frame**

### 3.2 Agent 内 SAM2 フェーズ（Job7, 1 run の steady 区間）

`scripts/sam2/analyze_tracker_log.py logs/sam2_tracker/sam2_tracker_run_4f2ba431-f957-4b16-8b67-9cc7cd136e15.log` の steady (frame_idx >= 16, `wall_ms`) より:

- `dataset_fetch` / `frame_fetch`:
  - mean ≈ **3.31 ms**, chunk0: mean ≈ 3.39ms, chunk>0: mean ≈ 3.31ms
- 前処理:
  - `preprocess`: mean ≈ **5.13 ms**
  - `memory_encode_prepare`: mean ≈ **0.76 ms**
  - `memory_encoder`: mean ≈ **0.34 ms**
- メモリ系:
  - `memory_conditioning`: mean ≈ **1.58 ms**
  - `memory_attention`: mean ≈ **0.62 ms**
- デコード系:
  - `sam_prompt_encoder`: mean ≈ **0.45 ms**
  - `sam_mask_decoder`: mean ≈ **0.51 ms**
- トラッキング本体:
  - `track_step` (steady, `wall_ms`): mean ≈ **18.25 ms**
  - `track_step` (steady, `gpu_ms`): mean ≈ **12.4 ms**
- 後処理:
  - `track_postprocess`: mean ≈ **0.71 ms**
  - `mask_to_shape`: mean ≈ **0.86 ms**（overall）

この run では、1 フレーム ≈46ms のうち:

- fetch (`dataset_fetch`): ~3.3ms
- SAM2 前処理 + encoder: ~6ms
- SAM2 メモリ系: ~2.2ms
- SAM2 デコード系: ~1ms
- `track_step` 本体: ~18ms (GPU ~12ms)
- 後処理 / shape conversion: ~1.5〜2ms
- 残り（~15ms 前後）は server 側の queue / REST / AnnotationRequest 適用 / DB 書き込みなど、agent 外部の処理とフェーズ間のオーバーラップに相当する。

## 4. Job30 (1000 frames, Job alias 30)

### 4.1 フレーム単位の壁時計（pipeline 全体）

- 測定 JSON: `tasks/sam2_tracker_job30_fetch_after_p2_3.json`
- 平均値（repeat=5 の run 平均）:
  - `dataset_fetch.avg_fetch_ms`:
    - per run: `[0.697, 0.724, 0.710, 0.721, 0.691]`
    - mean: **≈0.71 ms/frame**
  - `avg_track_per_frame_s`:
    - per run: `[27.14, 27.17, 26.90, 26.10, 26.87] ms`
    - mean: **≈26.8 ms/frame**

### 4.2 Agent 内 SAM2 フェーズ（Job30, 1 run の steady 区間）

`scripts/sam2/analyze_tracker_log.py logs/sam2_tracker/sam2_tracker_run_b0132a34-575b-4d1c-a5fe-e9377f840d7e.log` の steady (frame_idx >= 16, `wall_ms`) より:

- `dataset_fetch` / `frame_fetch`: mean ≈ **0.70 ms**
- 前処理:
  - `preprocess`: mean ≈ **1.78 ms**
  - `memory_encode_prepare`: mean ≈ **0.68 ms**
  - `memory_encoder`: mean ≈ **0.30 ms**
- メモリ系:
  - `memory_conditioning`: mean ≈ **1.18 ms**
  - `memory_attention`: mean ≈ **0.45 ms**
- デコード系:
  - `sam_prompt_encoder`: mean ≈ **0.38 ms**
  - `sam_mask_decoder`: mean ≈ **0.45 ms**
- トラッキング本体:
  - `track_step` (steady, `wall_ms`): mean ≈ **18.17 ms**
  - `track_step` (steady, `gpu_ms`): mean ≈ **11.96 ms**
- 後処理:
  - `track_postprocess`: mean ≈ **0.43 ms**
  - `mask_to_shape`: mean ≈ **0.29 ms**

Job30 では、1 フレーム ≈26.8ms のうち:

- fetch: ~0.7ms
- SAM2 前処理〜メモリ系〜デコード〜track_step〜後処理の合計: おおよそ ~24〜25ms
- 残り ~1–2ms 程度が queue / DB / そのほかのオーバーヘッドと考えられる。

## 5. 3 Job の比較と「見えていない部分」

### 5.1 fetch の影響

- Job1（20f）: `avg_fetch_ms ≈ 0.68ms`
- Job7（500f）: `avg_fetch_ms ≈ 3.31ms`
- Job30（1000f）: `avg_fetch_ms ≈ 0.71ms`

いずれも ChunkCacheMode + tracker preload により HTTP/zip I/O は数 ms 以下に抑えられている。Job7 のみ高解像度＋非対称な warmup の影響で 3ms 台に乗るが、それでも 1 フレーム 45–50ms のトラッキング全体から見れば 1 桁％程度の寄与に留まる。

### 5.2 SAM2 モデル内の支配成分

- 3 Job の steady 区間で共通して:
  - `track_step.wall_ms` ≈ 18ms / frame（GPU ≈12ms / frame）
  - `preprocess` + encoder 系 ≈ 2〜6ms / frame（Job7 が最も重い）
  - memory_conditioning / memory_attention / decoder / postprocess はそれぞれ <2ms / frame レベル。
- したがって **GPU 側の core は「preprocess + track_step」が支配的**であり、fetch や mask_to_shape/postprocess は steady 状態ではほぼ隠蔽可能なレベルになっている。

### 5.3 CVAT server 側（agent 外部）の寄与

`avg_track_per_frame_s`（AR の `updated_at - created_at` に基づく）と agent 内ログを比較すると:

- Job7:
  - `avg_track_per_frame` ≈ 46.4ms
  - agent 内 SAM2 フェーズ合計は ≈ 30ms 弱
  - 差分の ~15ms 前後は、queue / REST / AnnotationRequest apply / DB 書き込み／その他 Python ロジックに相当。
- Job1:
  - warmup の影響が大きく、`track_step` の outlier（100ms超）が平均値を押し上げている。
- Job30:
  - `avg_track_per_frame` ≈ 26.8ms
  - agent 内 SAM2 合計 ≈ 24–25ms
  - 差分は ~1–2ms 程度で、Job7 より server 側オーバーヘッドが小さい。

現状の計測では、server 側キュー処理 / DB 書き込み時間は `SAM2_TRACKER_LOG` に現れないため、「SAM2 内部」と「CVAT 全体」を一致するように足し合わせることはできないが、おおまかなギャップの大きさは上記のように推定できる。

2025-11-22 時点で `SAM2_TRACKER_SERVER_LOG` / `server_timing` / `approximate_breakdown_per_frame_ms` を用いた再計測（Job1/Job7/Job30, batch16, repeat=5/3/5）では、以下のように整理できる:

- Job1（20f, small imageset, repeat=5 の平均）:
  - `avg_track_per_frame_ms` ≈ **34.8 ms**
  - dataset fetch: `fetch_ms` ≈ **0.37 ms/frame**
  - server 側（queue/apply）: `server_ms` ≈ **0.0 ms/frame**（ノイズレベル）
  - 残り ≈ **34.4 ms/frame** が SAM2 core + Python ロジックに対応。
- Job7（500f, XXXX225-02, repeat=3 の平均）:
  - `avg_track_per_frame_ms` ≈ **47.3 ms**
  - dataset fetch: `fetch_ms` ≈ **3.86 ms/frame**
  - server 側: `server_ms` ≈ **0.0 ms/frame**
  - 残り ≈ **43.4 ms/frame** が SAM2 core + agent 側処理。
- Job30（1000f, imageset, repeat=5 の平均）:
  - `avg_track_per_frame_ms` ≈ **26.0 ms**
  - dataset fetch: `fetch_ms` ≈ **0.44 ms/frame**
  - server 側: `server_ms` ≈ **0.0 ms/frame**
  - 残り ≈ **25.6 ms/frame** が SAM2 core + agent 側処理。

`server_timing` の集計では、今回の環境・条件では queue acquire / `_apply_tracking_results` ともに 1 ランあたり数〜数十 ms 未満に収まり、1 フレームあたりへの寄与は ≪1ms レベルに押さえ込まれている。一方で `avg_track_per_frame_ms` 自体は Job7 と Job30 で大きく異なり、GPU 側の `track_step`＋前後処理が 1 フレームあたり 20〜45ms を支配していることが改めて確認できる。

## 6. TODO（処理全体の内訳をさらに明確化するタスク）

1. **server 側フェーズの計測追加**
   - `functions_annotationrequest` 更新・`_apply_tracking_results`・queue acquire/update を対象に、server ログ側でフェーズ別 `wall_ms` を計測する。
   - 具体的には:
     - AnnotationRequest 処理開始/完了に `SAM2_TRACKER_SERVER_LOG` を追加（run_id/AR ID/phase: `queue_wait`, `apply_results`, `db_write` 等）。
     - `scripts/sam2/analyze_tracker_log.py` に server ログも統合して、agent + server のフルパイプラインを 1 本のタイムラインとして集計する。

2. **`avg_track_per_frame` と SAM2 ログの突合**
   - `benchmark_tracker.py` に server 側 `SAM2_TRACKER_SERVER_LOG` を取り込むフックを追加し、1 run について:
     - fetch / preprocess / SAM2 core / postprocess / queue / DB の各カテゴリ毎に per-frame 平均を算出。
   - Job1 / Job7 / Job30 について、「avg_track_per_frame ≈ fetch + SAM2 + server」の内訳が足し算で近似できるか検証する。

3. **フェーズオーバーラップの可視化**
   - agent 内では dataset_fetch / preprocess / track_step がオーバーラップしうるため、単純な足し算ではなく、「クリティカルパス」を推定する必要がある。
   - `SAM2_TRACKER_LOG` に frame ごとのタイムスタンプ（相対時間）を追加し、1 フレームあたりの timeline をプロットできるようにする（別ツールで可視化）。

4. **他 Job への展開**
   - 今回まとめた Job1/Job7/Job30 以外の代表ジョブ（例: 長尺動画, 非 imageset, 異なる解像度）でも同様の breakdown を取り、共通するボトルネックパターンを抽出する。
   - 特に video タスクでは ChunkCacheMode が効かないため、fetch vs SAM2 vs server の比率を別途評価する必要がある。

5. **ドキュメントへの反映**
   - 本ファイルの内容を `tasks/sam2_tracker_bottleneck.md` や ADR 群とリンクさせ、次の最適化フェーズ（例: SAM2 core のさらなる高速化 / server 側の run-status まわり最適化）へのインプットにする。

### 6.1 実装タスク TODO（詳細）

- [x] `SAM2_TRACKER_SERVER_LOG` のフォーマット設計と共通ロガー（ヘルパ関数）の追加  
- [x] `acquire_annotation_request`（`cvat/apps/functions/services.py`）に queue wait / acquire の計測と `SAM2_TRACKER_SERVER_LOG` 出力を追加  
- [x] `_apply_tracking_results`（`cvat/apps/functions/tracking.py`）に apply 全体・DB 書き込みなどのフェーズ別計測とサーバーログ出力を追加  
- [x] 必要に応じて `FunctionQueueUpdateView` / `FunctionQueueCompleteView`（`cvat/apps/functions/views.py`）にも軽量なフェーズ計測ログを追加するか検討し、必要なら実装  
- [x] `scripts/sam2/analyze_tracker_log.py` を拡張し、`SAM2_TRACKER_LOG` と `SAM2_TRACKER_SERVER_LOG` の両方を読み込んで、agent/server のフェーズ別統計と run_id・frame 単位の統合タイムライン JSON（例: `--timeline-json` オプション）を出力できるようにする  
- [x] `scripts/sam2/benchmark_tracker.py` を拡張し、サーバーログ取得用オプション（例: `--server-log-service`）と `_collect_server_logs` を追加し、server ログをパースして run ごとの `server_timing` サマリ（queue / apply / DB 等）を測定 JSON に埋め込む  
- [x] 同じく `benchmark_tracker.py` で、dataset fetch / SAM2 / server の合計から `approx_total_ms_per_frame` と `avg_track_per_frame_s` との差分（誤差）を計算して JSON に記録する  
- [x] `ai-models/tracker/sam2_onnx_trt/func.py`（必要なら `cvat-cli/_internal/agent.py`）の `SAM2_TRACKER_LOG` に frame 相対あるいは run 相対のタイムスタンプ（例: `frame_t_ms`）を追加し、フェーズオーバーラップが見えるようにする  
- [x] 上記変更を反映した状態で Job1 / Job7 / Job30 のベンチマークコマンドを再実行し、新しい測定 JSON / ログで `avg_track_per_frame` と `approx_total_ms_per_frame` の誤差や server 側フェーズの寄与を確認する  
- [ ] 代表的な追加 Job（長尺 video / 解像度違いなど）でも同様に測定し、パターンを比較する  
- [ ] `tasks/sam2_tracker_timing_breakdown_20251122.md` と `tasks/sam2_tracker_bottleneck.md`（必要に応じて関連 ADR）に、上記計測方法・結果サマリ・TODO 消化状況を反映してドキュメントを更新する  

## 7. 関連コマンド一覧

### 7.1 Tracker ベンチマーク（Job1 / Job7 / Job30）

共通オプション:

- `--server http://localhost:8080`
- `--host-header 192.168.10.190`
- `--username admin --password admin`
- `--batch-sizes 16 --tracker-preload-chunks --include-fetch-metrics --include-server-timing`
- `--compose-cmd "docker compose -f docker-compose.yml -f docker-compose.dev.yml"`

Job1（20 frames, task_id=1/job_id=1, track=1）:

```bash
UV_HTTP_TIMEOUT=120 PYTHONPATH=cvat-cli/src:cvat-sdk \
  uv run python scripts/sam2/benchmark_tracker.py \
    --server http://localhost:8080 \
    --host-header 192.168.10.190 \
    --username admin --password admin \
    --job 1 --function 3 --track 1 \
    --start-frame 0 --target-frame 19 \
    --batch-sizes 16 --repeat 5 \
    --tracker-preload-chunks --include-fetch-metrics --include-server-timing \
    --compose-cmd "docker compose -f docker-compose.yml -f docker-compose.dev.yml" \
    --output tasks/sam2_tracker_job1_fetch_after_p2_3.json
```

Job7（500 frames, XXXX225-02, track=43）:

```bash
UV_HTTP_TIMEOUT=120 PYTHONPATH=cvat-cli/src:cvat-sdk \
  uv run python scripts/sam2/benchmark_tracker.py \
    --server http://localhost:8080 \
    --host-header 192.168.10.190 \
    --username admin --password admin \
    --job 7 --function 3 --track 43 \
    --start-frame 0 --target-frame 499 \
    --batch-sizes 16 --repeat 3 \
    --tracker-preload-chunks --include-fetch-metrics --include-server-timing \
    --compose-cmd "docker compose -f docker-compose.yml -f docker-compose.dev.yml" \
    --output tasks/sam2_tracker_job7_500f_fetch_after_p2_3.json
```

Job30（1000 frames, task_id=4/job_id=4, track=9）:

```bash
UV_HTTP_TIMEOUT=120 PYTHONPATH=cvat-cli/src:cvat-sdk \
  uv run python scripts/sam2/benchmark_tracker.py \
    --server http://localhost:8080 \
    --host-header 192.168.10.190 \
    --username admin --password admin \
    --job 4 --function 3 --track 9 \
    --start-frame 0 --target-frame 999 \
    --batch-sizes 16 --repeat 5 \
    --tracker-preload-chunks --include-fetch-metrics --include-server-timing \
    --compose-cmd "docker compose -f docker-compose.yml -f docker-compose.dev.yml" \
    --output tasks/sam2_tracker_job30_fetch_after_p2_3.json
```

### 7.2 Agent ログの解析

Job1（任意の run_id に合わせて変更）:

```bash
UV_HTTP_TIMEOUT=120 uv run python scripts/sam2/analyze_tracker_log.py \
  logs/sam2_tracker/sam2_tracker_run_d08f95f1-b914-4091-9165-5f745259ef96.log
```

Job7:

```bash
UV_HTTP_TIMEOUT=120 uv run python scripts/sam2/analyze_tracker_log.py \
  logs/sam2_tracker/sam2_tracker_run_4f2ba431-f957-4b16-8b67-9cc7cd136e15.log
```

Job30:

```bash
UV_HTTP_TIMEOUT=120 uv run python scripts/sam2/analyze_tracker_log.py \
  logs/sam2_tracker/sam2_tracker_run_b0132a34-575b-4d1c-a5fe-e9377f840d7e.log
```

### 7.3 fetch メトリクスとログの突合

任意の Job / run に対して、測定 JSON と agent ログを突合する例:

```bash
UV_HTTP_TIMEOUT=120 uv run python scripts/sam2/check_dataset_fetch_logs.py \
  --json tasks/sam2_tracker_job30_fetch_after_p2_3.json \
  --log  logs/sam2_tracker/sam2_tracker_run_b0132a34-575b-4d1c-a5fe-e9377f840d7e.log
```

### 7.4 sam2-tracker-agent の再ビルドと再起動

コード変更を agent に反映する際の標準コマンド（共通）:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml build sam2-tracker-agent
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d sam2-tracker-agent
```
