# SAM2 tracker dataset fetch metrics (Job 1, 20 frames)

| Mode | Run ID | init [s] | wall clock [s] | avg track [ms/frame] | avg fetch [ms/frame] | `log_missing` | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| preload on (`--tracker-preload-chunks`) | `a01f7bb2-d954-4408-80c6-80ad4dbb57ed` | 1.02 | 2.35 | 69 | **26.8** | false | chunk_id=0 (全フレーム同一チャンク)。`cached` はまだ False（1回目ダウンロードで lazy cache が埋まるため）。 |
| preload off | `9351740d-35cd-4d5f-9f80-df73a8956f7c` | 1.12 | 2.55 | 75 | **23.2** | false | chunk_id=0、`cached=False`。Job1 規模では preload 有無の差は僅少。長尺ジョブで再測予定。 |

- 計測コマンド: `scripts/sam2/benchmark_tracker.py --batch-sizes 16 --include-fetch-metrics ...`。詳細は `tasks/sam2_tracker_dataset_fetch_20251121_plan.md` を参照。
- ログ検証: `scripts/sam2/check_dataset_fetch_logs.py --json <measurement> --log <agent_log>` → いずれも `matched 19/19 frames`。
- エージェントログ: `logs/sam2_tracker/sam2_tracker_run_<run>.log` に `SAM2_TRACKER_LOG {"phase":"dataset_fetch",...}` が出力され、chunk_id / download_ms が採取できるようになった（`cached` は lazy chunk download の初回のみ False のまま）。長尺ジョブで chunk reuse が観測できるか要再計測。

## Job 30 (1000 frames, task_id=4/job_id=4, track_id=9)
エイリアス「Job 30」として扱う 1000 フレームジョブ（`sam2_measure_1000`）。batch16 / repeat5 で preload 有無を計測。

### 2025-11-18 (ChunkCacheMode 未配線時)
| Mode | Runs | Wall clock avg [s] (min–max) | Avg track [ms/frame] | Avg fetch [ms/frame] (min–max) | Hit ratio | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| preload on (`--tracker-preload-chunks`) | 5 (`85e40e45-…` ほか) | **68.5** (67.5–71.1) | 67.4 | **24.4** (23.8–26.6) | 0.0 | `cache_hits=0`, agent ログ上でも `chunk_preload_enabled=false`。 |
| preload off | 5 (`62228f51-…` ほか) | **67.2** (66.7–68.3) | 66.5 | **23.9** (23.9–24.0) | 0.0 | 実測は preload ON と同等。 |

- 測定 JSON: `tasks/sam2_tracker_dataset_fetch_20251121_job30_preload.json` / `..._job30_nopreload.json`
- ログ: `logs/sam2_tracker/sam2_tracker_run_<run_id>.log`。`scripts/sam2/check_dataset_fetch_logs.py` にて全 run `matched 999/999 frames`。

### 2025-11-18 (ChunkCacheMode 結線後)
`cvat-cli` / `cvat-sdk` を再ビルドし、`sam2-tracker-agent` を `SAM2_TRACKER_EXTRA_AGENT_ARGS="--tracker-preload-chunks --include-fetch-metrics"` 付きで再起動。`ChunkCacheMode.PREFETCH_CHUNKS_ONCE` を選択できるようになったため、再計測で `chunk_preload_enabled=true` と `cache_hits=999/999` を確認。

| Mode | Runs | Wall clock avg [s] (min–max) | Avg track [ms/frame] | Avg fetch [ms/frame] (min–max) | Hit ratio | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| preload on (`--tracker-preload-chunks`) | 5 (`a97b515a-…` ほか) | **36.04** (35.58–36.47) | 35.39 | **1.04** (1.04–1.05) | **1.0** | `tasks/sam2_tracker_dataset_fetch_20251121_job30_preload_retry.json`。ログ上も `chunk_preload_enabled=true` / `cached=true` のみ、`cache_misses=0`。 |
| preload off | 5 (`39dd4fd2-…` ほか) | **70.24** (63.73–94.90) | 69.29 | **25.04** (24.96–25.14) | 0.0 | `tasks/sam2_tracker_dataset_fetch_20251121_job30_nopreload_retry.json`。`cache_hits=0` / `cache_misses=999` で、各フレームが HTTP 取得になっている。 |

- ログ: `logs/sam2_tracker/sam2_tracker_run_<run_id>.log`。各 run を `scripts/sam2/check_dataset_fetch_logs.py` で突合し `matched 999/999 frames`。preload ON では `dataset_fetch.cached=true`、preload OFF では `cached=false` のみで `cache_stats.prefetch_enabled` も false。
- 観測: `avg_fetch_ms` が ~24 ms → **~1 ms** に改善し、wall clock も ~36 s まで短縮。対照群（preload off）は従来同様 ~25 ms / ~70 s となり、ChunkCacheMode 実装効果が定量化できた。
