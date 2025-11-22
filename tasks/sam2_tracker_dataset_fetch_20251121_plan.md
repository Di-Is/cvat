# Dataset streaming & caching plan (2025-11-21)

## Goals
- Document `ChunkCacheMode` / `TaskDataset` preload設計の前提と制約。
- 定常観測に使う `--include-fetch-metrics` JSON schema を決め、agent ログ (`SAM2_TRACKER_VERBOSE`) との突合方法を明文化。
- Job30 (1000 frames) を使った preload 有効/無効ベンチ計測フローを準備し、後続実測時の再現性を確保。
- Capture eviction/metrics policy for the shared ChunkCache to unblock CLI/SDK実装。
- 実測ツール (`scripts/sam2/benchmark_tracker.py`) に `--tracker-preload-chunks` / `--include-fetch-metrics` を追加し、ログ保存 + JSON 生成を自動化。

## Implementation status (2025-11-21)
- `scripts/sam2/benchmark_tracker.py` へフラグとログ取得フローを実装済み。`--include-fetch-metrics` 指定時は `logs/sam2_tracker/sam2_tracker_run_<run>.log` を生成し、`dataset_fetch` / `cache_stats` を measurement JSON に埋め込む。
- `scripts/sam2/check_dataset_fetch_logs.py` を追加。測定 JSON と agent ログを比較し、`chunk_id` / `cached` / `download_ms` / `decode_ms` の不一致や欠落フレームを検証可能。
- 2025-11-21 時点では `phase=\"dataset_fetch\"` ログが未実装のため、`phase=\"frame_fetch\"` (`wall_ms`) を fallback として集計する。chunk_id / cached の有無は pending。

## Measurement Protocol (draft)
1. `uv run python scripts/sam2/benchmark_tracker.py --job 30 --function 6 --track <id> --batch-sizes 16 --repeat 5 --tracker-preload-chunks --include-fetch-metrics --output tasks/sam2_tracker_dataset_fetch_20251121_preload.json`
2. 同条件で `--no-tracker-preload-chunks` 版も実行し、`*_nopreload.json` を作成。
3. それぞれの JSON から `dataset_fetch.frames`, `dataset_fetch.per_frame_fetch_ms`, `dataset_fetch.cached` を抽出し、`tasks/sam2_tracker_dataset_fetch_20251121_summary.md` に統計量（avg/median/p95）を書き出す。
4. Agent ログは `docker compose logs sam2-tracker-agent --since <submitted_at>` を取得し、`phase="dataset_fetch"` の行を JSONL で `logs/sam2_tracker_dataset_fetch_20251121.log` に保存。`chunk_id`, `cached`, `elapsed_ms` を集計し、計測 JSON と比較。

## ChunkCache specification (draft)
- **Mode enum**: `ChunkCacheMode = Enum('ChunkCacheMode', 'FETCH_ON_DEMAND PREFETCH_CHUNKS_ONCE')`. Default tracker mode: `FETCH_ON_DEMAND`, switched to `PREFETCH_CHUNKS_ONCE` when `--tracker-preload-chunks` or env `SAM2_TRACKER_PREFETCH=1` is set.
- **Keying**: `(task_id, chunk_id, media_type)` tuple. `chunk_id = frame_index // chunk_size` for image tasks, or `(video_id, chunk_start)` for video streams.
- **Entry fields**: `ChunkCacheEntry = dataclass(task_id:int, chunk_id:int, frame_indices:list[int], payload:bytes|memoryview, cost_bytes:int, created_at:float, last_used:float, hit_count:int)`. `frame_indices` ensures dataset fetch metrics can map frames to cached chunk.
- **Operations**:
  - `get(task_id, chunk_id) -> ChunkCacheEntry|None`: increments `hit_count`, updates `last_used`.
  - `put(entry, *, allow_replace: bool = False)`: rejects insertion if `cost_bytes > max_ram_bytes`. On eviction, increments `evictions_total` metric.
  - `release_task(task_id)`: drop all entries for job cleanup or failure.
  - `stats()` returns `{'entries': n, 'bytes': used, 'hits': hits_total, 'misses': misses_total}` for Prometheus gauges.
- **Eviction policy**: global LRU enforced with `heapq` keyed by `last_used`. Additional guard: when resident memory exceeds `max_ram_bytes * 0.9`, trigger background eviction to 70%.
- **Thread safety**: `ChunkCache` holds a `threading.RLock`. `get/put/release_task` wrap state mutations. Since agent fetchers may run from threadpool, all entry accesses go through the lock.
- **Instrumentation**: expose `tracker_chunk_cache_hits_total`, `tracker_chunk_cache_misses_total`, `tracker_chunk_cache_evictions_total`, `tracker_chunk_cache_bytes` via Prometheus. CLI logs also emit `{"phase":"dataset_cache","event":"put","chunk_id":12,"cost_bytes":1048576}`.

## JSON Schema (draft v1)
```json
{
  "job": 30,
  "function": 6,
  "batch_size": 16,
  "tracker_preload_chunks": true,
  "chunk_cache_mode": "PREFETCH_CHUNKS_ONCE",
  "runs": [
    {
      "repeat": 0,
      "init_seconds": 0.0,
      "track_seconds": 0.0,
      "dataset_fetch": {
        "frames": [0, 1, 2],
        "chunk_ids": [0, 0, 0],
        "cached": [false, true, true],
        "download_ms": [118.0, 0.0, 0.0],
        "decode_ms": [2.5, 1.1, 1.0],
        "per_frame_fetch_ms": [120.5, 87.2, 65.0],
        "avg_fetch_ms": 90.9,
        "hit_ratio": 0.67,
        "log_missing": false
      },
      "cache_stats": {
        "prefetch_enabled": true,
        "cache_bytes_peak": 536870912,
        "cache_hits": 950,
        "cache_misses": 50,
        "evictions": 4
      }
    }
  ]
}
```

## Agent log mapping
- Each `SAM2_TRACKER_VERBOSE` entry for dataset fetch will follow: `{"phase":"dataset_fetch","frames":[0,1],"chunk_id":0,"cached":false,"download_ms":118.0,"decode_ms":2.5}`.
- `benchmark_tracker.py --include-fetch-metrics` parses these logs and populates `dataset_fetch` arrays per run. Missing log entries trigger `dataset_fetch.log_missing=true` flag in JSON to highlight gaps.
- Log collection command template:
  ```bash
  docker compose -f docker-compose.yml -f docker-compose.dev.yml \
    logs sam2-tracker-agent --since "$submitted_at" \
    | rg 'SAM2_TRACKER_VERBOSE' > logs/sam2_tracker_dataset_fetch_20251121.log
  ```
- Parser correlates `run_id` embedded in log prefix with measurement JSON so multiple concurrent runs remain separable.

## Sample agent logs
```
2025-11-21T04:15:23.512Z SAM2_TRACKER_VERBOSE run=a1b2 phase=dataset_fetch chunk_id=0 frames=[0,1,2,3] cached=false download_ms=118.0 decode_ms=2.4
2025-11-21T04:15:23.645Z SAM2_TRACKER_VERBOSE run=a1b2 phase=dataset_fetch chunk_id=0 frames=[4,5,6,7] cached=true download_ms=0.0 decode_ms=1.1
2025-11-21T04:15:23.742Z SAM2_TRACKER_VERBOSE run=a1b2 phase=dataset_cache event=put chunk_id=1 cost_bytes=1048576 hits=0
```
- Parser rules: `phase=dataset_fetch` → per-frame metrics, `phase=dataset_cache` → cache-level stats. Both share `run` identifier for correlation.

### Log validation checklist
1. Run a short tracker job (Job8, batch_size=16) with `SAM2_TRACKER_VERBOSE=1` and `--include-fetch-metrics` enabled:
   ```bash
   UV_HTTP_TIMEOUT=120 uv run python scripts/sam2/benchmark_tracker.py \
     --job 1 --function 3 --track 1 --batch-sizes 16 \
     --tracker-preload-chunks --include-fetch-metrics \
     --output tasks/sam2_tracker_dataset_fetch_20251121_job1_preview.json
   ```
2. Capture logs for the same interval:
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.dev.yml \
     logs sam2-tracker-agent --since "$submitted_at" \
     | rg 'SAM2_TRACKER_VERBOSE' > logs/sam2_tracker_dataset_fetch_20251121_preview.log
   ```
3. Run the verification helper `scripts/sam2/check_dataset_fetch_logs.py` that loads both JSON + log file and asserts:
   - every `dataset_fetch.frames[i]` maps to corresponding `chunk_id` in the log,
   - download_ms/decode_ms arrays are populated, zeroed for cached hits,
   - `cache_stats.cache_hits` equals number of `cached=true` entries.
   Example invocation:
   ```bash
   uv run python scripts/sam2/check_dataset_fetch_logs.py \
     --json tasks/sam2_tracker_dataset_fetch_20251121_job1_preview.json \
     --log logs/sam2_tracker_dataset_fetch_20251121_preview.log
   ```
4. Update this document with actual log snippets + verification output (pass/fail) for traceability.

#### Verification snapshot (2025-11-21 preview)
- Inputs:
  - JSON: `tasks/sam2_tracker_dataset_fetch_20251121_job1_preview.json`
  - Logs: `logs/sam2_tracker_dataset_fetch_20251121_preview.log`
- Command:
  ```bash
  UV_HTTP_TIMEOUT=120 uv run python scripts/sam2/check_dataset_fetch_logs.py \
    --json tasks/sam2_tracker_dataset_fetch_20251121_job1_preview.json \
    --log logs/sam2_tracker_dataset_fetch_20251121_preview.log
  ```
- Output: `run preview-run-0001: matched 7/7 frames` → `All runs verified successfully.`
- メモ: 現状はサンプルデータ（手動生成）での検証だが、実測ログに差し替える際は同じ手順で再度確認する。

#### Job1 actual run (2025-11-21 22:29 JST)
- コマンド:
  ```bash
  UV_HTTP_TIMEOUT=120 uv run python scripts/sam2/benchmark_tracker.py \
    --server http://192.168.10.190:8080 --host-header 192.168.10.190 \
    --username admin --password admin \
    --job 1 --function 3 --track 1 \
    --start-frame 0 --target-frame 19 --batch-sizes 16 \
    --tracker-preload-chunks --include-fetch-metrics \
    --compose-cmd "docker compose -f docker-compose.yml -f docker-compose.dev.yml" \
    --output tasks/sam2_tracker_dataset_fetch_20251121_job1.json
  ```
- 生成物:
  - 測定 JSON: `tasks/sam2_tracker_dataset_fetch_20251121_job1.json`
  - ログ: `logs/sam2_tracker/sam2_tracker_run_a01f7bb2-d954-4408-80c6-80ad4dbb57ed.log`
- 結果概要 (`tracker_preload_chunks=true`):
  - batch16, repeat0, run `a01f7bb2-d954-4408-80c6-80ad4dbb57ed`
  - track_chunks=2, total_frames=19, avg_track_per_frame ≈69 ms, wall_clock=2.35 s, init=1.02 s
  - `dataset_fetch`: `SAM2_TRACKER_LOG` の `phase="dataset_fetch"` から chunk_id (=0) / download_ms (22–95 ms) を記録。lazy chunk download のため `cached=False` のまま（初回アクセスで chunk を取得）。
  - 非プリロード版 (`tasks/sam2_tracker_dataset_fetch_20251121_job1_nopreload.json`, run `9351740d-35cd-4d5f-9f80-df73a8956f7c`) も chunk_id=0 / `cached=False` / avg_fetch_ms ≈23.2 ms。Job1 規模では preload 有無の差が僅少で、長尺ジョブで再測予定。
- 検証:
  ```bash
  UV_HTTP_TIMEOUT=120 uv run python scripts/sam2/check_dataset_fetch_logs.py \
    --json tasks/sam2_tracker_dataset_fetch_20251121_job1.json \
    --log logs/sam2_tracker/sam2_tracker_run_a01f7bb2-d954-4408-80c6-80ad4dbb57ed.log
  ```
  → `run ...: matched 19/19 frames` / `All runs verified successfully.`
  - `tasks/sam2_tracker_dataset_fetch_20251121_job1_nopreload.json` も同じ手順で一致確認済み。
  - 集計結果は `tasks/sam2_tracker_dataset_fetch_20251121_summary.md` に整理。

## Next steps
- [x] finalize JSON schema fields (`chunk_id`, `download_ms`, `decode_ms`, `log_missing`, `cache_stats`).
- [x] capture sample agent logs and ensure `chunk_id` が一致しているか確認（Job1 preload/non-preload で `check_dataset_fetch_logs.py` パス）。
- [x] draft `ChunkCache` eviction/metrics specification and feed back into `tasks/sam2_tracker_bottleneck.md`。
- [ ] align CLI flag names (`--tracker-preload-chunks`, `--include-fetch-metrics`) with docstrings and update `cvat-cli --help` snapshot。

#### Job30 dataset (1000 frames) setup & measurement (2025-11-18 07:30 JST)
- Synthetic data生成:
  ```bash
  UV_HTTP_TIMEOUT=120 uv run python - <<'PY'
  import math
  from pathlib import Path
  from PIL import Image, ImageDraw
  root = Path('tmp/sam2_job1000')
  root.mkdir(parents=True, exist_ok=True)
  for idx in range(1000):
      img = Image.new('RGB', (512, 512), color=(12, 12, 38))
      draw = ImageDraw.Draw(img)
      prog = idx / 999
      rect_w, rect_h = 96, 80
      x = 40 + int((512 - rect_w - 80) * prog)
      y = 60 + int(120 * math.sin(prog * math.pi * 2))
      bbox = (x, y, x + rect_w, y + rect_h)
      draw.rectangle(bbox, fill=(255, 128, 0), outline=(255, 220, 0), width=3)
      bar_height = int(80 + 70 * math.sin(idx * 0.3))
      bar_x = 20 + (idx % 20) * 10
      draw.rectangle((bar_x, 512 - bar_height - 5, bar_x + 6, 507), fill=(0, 160, 255))
      img.save(root / f'frame_{idx:04d}.png')
  PY
  ```
- タスク作成 (`task_id=4`, 実運用では Job 30 と呼称予定):
  ```bash
  PYTHONPATH=cvat-cli/src:cvat-sdk UV_HTTP_TIMEOUT=120 uv run python -m cvat_cli \
    --auth admin:admin --server-host http://192.168.10.190 --server-port 8080 \
    task create --labels '[{"name":"obj"}]' sam2_measure_1000 local tmp/sam2_job1000/frame_*.png
  ```
  → Job `4` (`frame_count=1000`, label id `4`).
- トラック初期化（frame0 polygon, track id = **9**）:
  ```bash
  curl -s -H 'Host: 192.168.10.190' -H 'Content-Type: application/json' \
    -u admin:admin http://192.168.10.190:8080/api/jobs/4/annotations \
    -X PUT --data-binary @/tmp/job4_track_payload.json
  ```
  payload:
  ```json
  {"version":0,"tracks":[{"frame":0,"label_id":4,"group":0,"source":"manual",
     "shapes":[{"type":"polygon","frame":0,"points":[60,80,196,80,196,160,60,160],
     "occluded":false,"outside":false,"z_order":0,"rotation":0,"attributes":[]}]}],
   "shapes": [], "tags": []}
  ```
- 計測（batch16, repeat5, Job alias 30 / track id 9）:
  ```bash
  UV_HTTP_TIMEOUT=120 PYTHONPATH=cvat-cli/src:cvat-sdk uv run python scripts/sam2/benchmark_tracker.py \
    --server http://localhost:8080 --host-header 192.168.10.190 \
    --username admin --password admin \
    --job 4 --function 3 --track 9 \
    --start-frame 0 --target-frame 999 \
    --batch-sizes 16 --repeat 5 \
    --tracker-preload-chunks --include-fetch-metrics \
    --compose-cmd "docker compose -f docker-compose.yml -f docker-compose.dev.yml" \
    --output tasks/sam2_tracker_dataset_fetch_20251121_job30_preload.json
  ```
  同条件で `--tracker-preload-chunks` を省略し、`*_nopreload.json` を作成。
- 生成物:
  - 測定 JSON: `tasks/sam2_tracker_dataset_fetch_20251121_job30_preload.json`, `..._job30_nopreload.json`
  - エージェントログ: `logs/sam2_tracker/sam2_tracker_run_<run_id>.log`（全10本）
  - Job30 alias = `task_id=4` / `job_id=4` / `track_id=9` を記録。
- 検証: 各 run ごとに measurement を 1 件のみ含むテンポラリ JSON を作成し、
  `scripts/sam2/check_dataset_fetch_logs.py --json <single_run.json> --log <agent_log>` で 10/10 run が `matched 999/999 frames`。
- 所見:
  - `tracker_preload_chunks` フラグを付与しても agent ログ上の `chunk_preload_enabled` は常に `false` で、`cache_hits=0` のまま（未実装）。
  - Job30 (1000f) でも `avg_fetch_ms` は 23–27 ms に留まり、preload on/off で顕著な差が出ていない。
  - `hit_ratio` 全 run 0 → ChunkCacheMode 実装後に再計測が必要。

#### ChunkCacheMode 配線後の Job30 再計測 (2025-11-18 11:10 JST)
- `cvat-sdk` / `cvat-cli` に `ChunkCacheMode` を導入後、`docker compose -f docker-compose.yml -f docker-compose.dev.yml build sam2-tracker-agent` を実行し、`SAM2_TRACKER_EXTRA_AGENT_ARGS="--tracker-preload-chunks --include-fetch-metrics"` を付与して `sam2-tracker-agent` を再起動。
- 同条件 (`batch16`, `repeat5`, `job=4`, `track=9`) で `scripts/sam2/benchmark_tracker.py` を再実行し、`tasks/sam2_tracker_dataset_fetch_20251121_job30_preload_retry.json` を取得。
  - 5 run すべて `chunk_preload_enabled=true`/`cache_hits=999`/`cache_misses=0`/`hit_ratio=1.0`。`avg_fetch_ms` は **約 1.04 ms**、wall clock は **35.6–36.5 s** まで改善。
  - ログ (`logs/sam2_tracker/sam2_tracker_run_<run_id>.log`) を `scripts/sam2/check_dataset_fetch_logs.py` で突合し、run ごとに `matched 999/999 frames` を確認。
- 追加で `SAM2_TRACKER_EXTRA_AGENT_ARGS="--include-fetch-metrics"` 状態でも同条件を再測 (`tasks/sam2_tracker_dataset_fetch_20251121_job30_nopreload_retry.json`)。`chunk_preload_enabled=false` / `avg_fetch_ms≈25 ms` / `wall_clock≈70 s` で、旧実装と同等のベースラインを確保（ログ検証も 5/5 run `matched 999/999 frames`）。
