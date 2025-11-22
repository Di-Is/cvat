# SAM2 Tracker preprocess hardening plan

## Objectives
- 前処理のフォールバック分岐を極力排除し、GPU fast-preprocess を標準経路として固定化する。
- ダブルバッファ（CPU前処理→H2D→forward_image）をエージェント側で結線し、前処理と推論のオーバーラップを実現する。
- 起動時ウォームアップを必ず走らせ、torch.compile/autotune のスパイクを chunk0 に載せない。
- ログ計測を強化し、GPU ms / cache hit 率 / warmup 所要時間を確実に把握できるようにする。

## TODO
- [ ] `func.py`: fast-preprocess をデフォルト必須化し、CPU Transform への暗黙フォールバックを廃止。`_encoded_bytes` が欠損する場合の扱いを明示（例外 or テスト用フラグでのみ許容）。
- [ ] `func.py`: `_extract_encoded_bytes` を強化し、欠損時はメモリ再エンコードで `_encoded_bytes` を埋めて GPU デコード経路を継続利用する。
- [ ] `func.py`: `SAM2_TRACKER_WARMUP_FRAMES` のデフォルトを >0 にし、起動時 burn-in を必須化。`warmup_preprocess`/`warmup_track` ログを `SAM2_TRACKER_VERBOSE` で出力。
- [x] `func.py`: `SAM2_TRACKER_LOG` に `preprocess_gpu_ms` / `track_step_gpu_ms` を `cuda_event` で記録してオーバーラップ有無を可視化。非同期時は preprocess ストリーム上の Event を差分計測し、同期を増やさない形でログ化。
- [ ] `cvat_cli/_internal/agent.py`: `cpu_preprocess_image` → `forward_preprocessed_tensor` → `track` へのダブルバッファ結線（非同期前提）。`SAM2_TRACKER_DOUBLE_BUFFER` フラグで制御し、同期パスも残す。
- [ ] `.env`/compose: tracker 用のデフォルトを `FAST_PREPROCESS=1`, `ASYNC_PREPROCESS=1`, `WARMUP_FRAMES>=2`, `EXTRA_AGENT_ARGS="--tracker-preload-chunks --include-fetch-metrics"` に揃える。不要なフォールバックフラグを削除。
- [ ] 回帰計測: Job7 500f batch16 preload をダブルバッファ ON/OFF で実行し、chunk0/steady の `preprocess_gpu_ms` / `track_step_gpu_ms` / `avg_fetch_ms` / `hit_ratio` を比較。短尺 Job1 20f で 3 リピートして chunk0 スパイク有無を確認。
- [ ] `func.py`: CUDA イベント計測による同期を壁時計計測から分離し、オーバーラップ効果を阻害しないよう変更（必要時のみ計測）。

## Notes
- 期待する唯一の高速パス: `_encoded_bytes` 付き入力 → fast-preprocess (GPU decode) → async stream → wait_event → track_step（vos_optimized + cudagraph ON）→結果適用。これ以外の CPU Transform/非 preload 経路は極力禁止。
- 計測コマンド例（500f, batch16 preload）: `UV_HTTP_TIMEOUT=120 PYTHONPATH=cvat-cli/src:cvat-sdk uv run python scripts/sam2/benchmark_tracker.py --server http://192.168.10.190:8080 --host-header 192.168.10.190 --username admin --password admin --job 7 --function 3 --track <track_id> --start-frame 0 --target-frame 499 --batch-sizes 16 --tracker-preload-chunks --include-fetch-metrics --agent-log-dir logs/sam2_tracker --output tasks/<output>.json`.

## Latest measurements (post GPU-event logging without sync)
- 条件: GPU fast-preprocess ダブルバッファON、async preprocess、イベント記録で同期なし、Job7/500f/batch16 preload。
- Run ID `aab559be-2a81-4add-87f8-20d36b223f1f` (`tasks/sam2_tracker_COPG225_500_batch16_gpu_double_buffer_events2.json`, `logs/sam2_tracker/sam2_tracker_run_aab559be-2a81-4add-87f8-20d36b223f1f.log`)
  - wall 37.75s (init 16.41s / track 21.10s), avg_track_per_frame 42.3ms。
  - track_step GPU (steady): mean ≈12.25ms / p95 ≈12.75ms, wall p50 ≈18.29ms → CPU/周辺オーバーヘッド ~6ms。
  - preprocess wall p50 ≈5.0ms（asyncのため GPU ms はログ対象外、必要なら ready_event 後計測を追加予定）。
  - dataset fetch avg ≈8.04ms, hit_ratio=1.0。
- 備考: CUDAイベント計測を再導入しても同期を増やさない構成で、ダブルバッファのオーバーラップは維持。initスパイクは ~16s まで縮小、さらなる短縮は burn-in/compile 前倒しが必要。

## CPU オーバーヘッド内訳（定常, run 9d84dc5b…）
- track_step: wall ≈17.9 ms / GPU ≈11.9 ms → CPU/周辺 ≈6 ms/フレーム
- track_postprocess: 平均 ~0.64 ms
- mask_to_shape: 平均 ~0.77 ms
- 残り ~4–5 ms は track_step 前後の Python オーケストレーション（wait_event、state更新、入出力整形、ログ生成など）が主因と推定
- ログOFF(run b9bc988e…)では init が大きく短縮するが、定常 per-frame はログONとほぼ同等 → 定常の差分は小さく、主に init に効いている
- 改善優先度: (1) track_step 周辺の Python 更新を軽量化（OrderedDict→固定長など）、(2) 不要同期の削減、(3) ログは計測ラン限定・実運用は verbose=0

## Measurement checklist / commands
- 事前準備: `.env` で `SAM2_TRACKER_FAST_PREPROCESS=1`, `SAM2_TRACKER_ASYNC_PREPROCESS=1`, `SAM2_TRACKER_DOUBLE_BUFFER=1`, `SAM2_TRACKER_GPU_DOUBLE_BUFFER=1`, `SAM2_TRACKER_WARMUP_FRAMES>=2`, `SAM2_TRACKER_VERBOSE=1`（計測時のみ）を設定し、`docker compose --profile sam2-agent restart sam2-tracker-agent` で反映。
- 計測コマンド例（500f, batch16 preload, verbose=1）:  
  `PYTHONPATH=cvat-cli/src:cvat-sdk UV_HTTP_TIMEOUT=120 uv run python scripts/sam2/benchmark_tracker.py --server http://192.168.10.190:8080 --host-header 192.168.10.190 --username admin --password admin --job 7 --function 3 --track 42 --start-frame 0 --target-frame 499 --batch-sizes 16 --tracker-preload-chunks --include-fetch-metrics --agent-log-dir logs/sam2_tracker --output tasks/<output>.json`
- ログOFFでオーバーヘッドを確認する場合は `SAM2_TRACKER_VERBOSE=0` を一時的に設定して同コマンドを実行（init 短縮の影響が大きいので、定常 per-frame の比較に使う）。
- 注意点: verbose=1 は壁時計/初期化に大きく影響するため、性能測定用のランと実運用を分ける。非同期 preprocess で GPU ms を取りたい場合は ready_event 待機後の計測経路を使う（現状は track_step ログに preprocess_gpu_ms が未出力のため追加実装が必要）。
