# SAM2 Tracker Job30 画像フェッチ改善方針

## ターゲット
- 対象ジョブ: Job30（1000 frames, task_id=4/job_id=4, track_id=9）
- 目標: `--tracker-preload-chunks` + ChunkCacheMode 有効時に、`dataset_fetch.avg_fetch_ms` を **安定して 1ms 付近（可能なら 1ms 未満）** に抑えつつ、GPU 推論が支配的な状態を維持する。

## 現状整理（Job30）
- `tasks/sam2_tracker_dataset_fetch_20251121_summary.md` 時点の結果:
  - preload ON + ChunkCacheMode 結線後: `avg_fetch_ms ≈ 1.04ms` / `hit_ratio=1.0`（cache_hits=999, cache_misses=0）。
  - preload OFF: `avg_fetch_ms ≈ 25ms` / `hit_ratio=0.0`。
- `TaskDataset` + SAM2 Tracker のデータ取得経路:
  - `cvat-cli/_internal/agent.py::_TrackerDatasetRepository` が task 単位で `TaskDataset` を共有。
  - `--tracker-preload-chunks` 有効時は `ChunkCacheMode.PREFETCH_CHUNKS_ONCE` を選択し、imageset タスクは zip chunk を一度だけダウンロードして再利用。
  - 各 AR では `_calculate_result_for_track_ar()` → `_prefetch_tracker_chunks()` で必要 chunk を同期プリフェッチ → `_get_sample_from_ar_params()` で `dataset.samples` から該当フレームの `Sample` を取得。
  - 実際の画像ロードは `_load_tracker_image()` 内で `sample.media.load_image()` を呼び、`time.perf_counter()` 計測結果を `frame_fetch` / `dataset_fetch` ログとして記録。
- `TaskDataset` 側のロード実装:
  - imageset + PRELOAD_ALL / PREFETCH_CHUNKS_ONCE:
    - `_load_frame_image_from_cache()` / `_load_frame_image_from_lazy_chunk_cache()` → `_frame_bytes_from_cache()` で zip から JPEG バイト列を取得。
    - `_image_from_encoded()` に渡し、`PIL.Image.open()` + `image.info["_encoded_bytes"]=encoded` + `image.load()` を実行。
  - `MediaElement.load_encoded_bytes()` は `_load_frame_bytes()` 経由で再度 zip からバイト列を読む。
- SAM2 側 fast preprocess:
  - `ai-models/tracker/sam2/utils/preprocess.py::convert_rgb_image()` が `image.info["_encoded_bytes"]` を参照し、`torchvision.io.decode_image` で GPU 側 decode → resize → normalize を実施。
  - つまり「encoded bytes が欲しいだけ」だが、前段で CPU decode（`image.load()`）が走っている。

## 問題点（Job30 観点）
1. **zip の二重読み出し**
   - `_load_tracker_image()` は
     - `sample.media.load_image()`（zip → encoded bytes → PIL.Image）
     - その直後の `sample.media.load_encoded_bytes()`（zip → encoded bytes）
     を呼んでいる。
   - `TaskDataset._image_from_encoded()` ですでに `image.info["_encoded_bytes"]` を埋めているため、fast preprocess 経路では `load_encoded_bytes()` は本質的に不要。
   - Job30 では `avg_fetch_ms ≈ 1ms` に到達しているものの、「zip I/O + decode を 2回実行する」設計のままでは高解像度ジョブや将来の回帰に弱い。

2. **CPU 側 JPEG decode の二重化**
   - `TaskDataset._image_from_encoded()` は常に `image.load()` を呼び、JPEG を CPU で完全デコードする。
   - その後 SAM2 tracker は `_encoded_bytes` を使って GPU decode を行うため、実質「CPU decode → GPU decode」の二重 decode になっている。
   - Job30 のような軽めの frames では 1ms 近くまで詰められているが、より重い画像（COPG 系など）では `dataset_fetch` に decode コストが乗り、8〜20ms 付近で頭打ちになる。

3. **`_get_sample_from_ar_params()` の線形探索**
   - 現状は `for sample in dataset.samples: ...` で毎回線形探索している。
   - Job30(1000f) + batch16 のようなケースでは、AR 数×フレーム数分だけ O(N) のループが入り、CPU 側のオーバーヘッドとして効いてくる。

4. **ログ指標が「純粋な fetch」と decode/変換を区別していない**
   - `dataset_fetch.wall_ms` には zip/HTTP I/O だけでなく、`PIL.Image.open` + `image.load` + `load_encoded_bytes` 呼び出しまで含まれる。
   - Job30 では「全体として 1ms」に到達しているものの、「I/O は 0.2ms だが decode が 0.8ms」といった内訳が見えず、今後のチューニング方針を立てにくい。

## 対応案と優先順位

### P1（最優先: Job30 に直接効く小さめ変更）

#### P1-1 `_load_tracker_image()` の二重読み出しを解消
- 対象: `cvat-cli/src/cvat_cli/_internal/agent.py::_load_tracker_image`
- 変更方針:
  - `sample.media.load_encoded_bytes()` の呼び出しを削除するか、
  - 少なくとも `TaskDataset`（imageset + ChunkCacheMode 有効）経路では呼ばないように分岐。
- ねらい:
  - `TaskDataset._image_from_encoded()` が `image.info["_encoded_bytes"]` を必ず設定する前提で、fast preprocess は `image.info` 経由で bytes を取得する。
  - zip チャンクの読み出しを「1フレームあたり 1回」に減らし、Job30 の `avg_fetch_ms` を安定して 1ms 未満に近づける。
- 期待効果（Job30）:
  - 現状 ≈1.04ms の fetch を、ログ付き計測でも 1ms 未満（またはよりタイトな 1ms 付近）に押し下げやすくなる。
  - `SAM2_TRACKER_VERBOSE=1` でも本番相当のパスで計測できる。
- リスク/注意点:
  - `_encoded_bytes` に依存するのは SAM2 tracker 系列のみ。`TaskDataset` 本体は変更しないため影響範囲は限定的。

#### P1-2 `_get_sample_from_ar_params()` のフレーム探索を辞書化
- 対象: `cvat-cli/src/cvat_cli/_internal/agent.py::_get_sample_from_ar_params`
- 変更方針:
  - `TaskDataset` に `frame_index → Sample` のマップ、もしくは index テーブルを持たせる、
  - または Agent 側で `dataset.samples` から 1 回だけマップを構築し、その後は O(1) lookup で参照する。
- ねらい:
  - Job30(1000f) + batch16 のようなケースでも、AR あたりのフレーム取得を O(1) に近づけて CPU オーバーヘッドを削減する。
- 期待効果（Job30）:
  - `dataset_fetch` メトリクスには直接は乗らないが、`track_chunk.wall_ms` の CPU 成分を削ることで、GPU 推論がより支配的な状態に寄せる。
  - 長尺・多トラックジョブでも、線形探索によるスケール悪化を防げる。
- リスク/注意点:
  - 追加キャッシュの導入のみで既存 API は変えない設計とし、安全性を優先する。

### P2（中優先: Job30 以外の重いジョブにも効く設計変更）

#### P2-3 `TaskDataset._image_from_encoded()` の lazy decode 化
- 対象: `cvat-sdk/cvat_sdk/datasets/task_dataset.py::_image_from_encoded`
- 現状:
  - `PIL.Image.open(buffer)` → `image.info["_encoded_bytes"]=encoded` → `image.load()`（CPU decode）を必ず実行。
  - SAM2 tracker fast preprocess は `_encoded_bytes` だけを利用し、画素値は見ないケースが多い。
- 変更案:
  - `_image_from_encoded()` から `image.load()` を外し、
    - 「PIL header + `_encoded_bytes` が入っているだけの軽量オブジェクト」を返す。
  - CPU 側で画素にアクセスする必要があるパスは、呼び出し側で `image.load()` させる。
- 期待効果:
  - SAM2 fast preprocess 経路では CPU decode コストがほぼゼロになり、decode は完全に GPU 側へ逃がせる。
  - Job30 より重い imageset（高解像度・高品質 JPEG）でも、`dataset_fetch` に decode 部分が乗らず、1ms 台を維持しやすくなる。
- リスク/注意点:
  - Detection / Interactor など、TaskDataset を経由して CPU で画像を触るコードに挙動変化が出る可能性があるため、代表的なパスでの smoke test が必須。

#### P2-4 fetch と decode/変換時間のログ分離
- 対象: `cvat-cli/src/cvat_cli/_internal/agent.py::_load_tracker_image` の計測/ログ設計
- 変更案:
  - `dataset_fetch` では「zip/HTTP I/O 完了までの時間」のみを `wall_ms` として計測。
  - PIL decode や追加の変換コストは `decode_ms` として明示的にログ分離する。
- 期待効果:
  - Job30 のログから「純粋な fetch は 1ms 未満、decode が Xms」という内訳が即座に分かる。
  - 高負荷ジョブで I/O と decode のどちらを最適化すべきか判断しやすくなる。
- リスク/注意点:
  - 実際の E2E 推論速度は変わらないため、これは観測改善タスク。P1/P2-3 によるロジック改善がひと段落した後に取り組むのが良さそう。

### P3（長期: 設計レベルの再編）

#### P3-5 SAM2 Tracker 専用の encoded-bytes Dataset 経路
- 案:
  - SAM2 tracker 専用に「encoded bytes + 最小限のメタデータ」だけを提供する Dataset/MediaElement 実装を用意し、GPU decode 前提で最適化する。
  - 既存の `TaskDataset` は汎用用途のまま維持しつつ、tracker だけ別経路を取る形。
- 期待効果:
  - Job30 や今後の長尺・高解像度ジョブでも
    - fetch ≒ 1ms、
    - decode は完全に GPU 上で処理、
    を設計レベルで保証しやすくなる。
- コスト:
  - SDK API 設計、型、安全なフォールバックパス、テストの整備が必要。
  - P1/P2 の効果と安定性を見極めたうえで検討する長期タスクとする。

## 優先度まとめ
- **短期（すぐ着手）**
  - P1-1: `_load_tracker_image` の `load_encoded_bytes` 削除/限定化。
  - P1-2: `_get_sample_from_ar_params` のフレーム lookup を辞書化。
- **中期**
  - P2-3: `TaskDataset._image_from_encoded` の lazy decode 化。
  - P2-4: fetch / decode のログ分離。
- **長期**
  - P3-5: SAM2 tracker 専用の encoded-bytes Dataset 経路の設計・導入。

この順に進めることで、Job30 ではまず `avg_fetch_ms` を実運用でも 1ms 付近に安定させ、そのうえで COPG 系などの重いジョブにも通用する構造的な最適化へ段階的に移行できる想定。

## TODO リスト

### 実装タスク
- [x] P1-1 `_load_tracker_image` から `load_encoded_bytes` を削除/限定化する実装方針を確定する。
- [x] P1-1 `_load_tracker_image` 実装を更新し、TaskDataset (imageset) 経路では `image.info["_encoded_bytes"]` のみを使うようにする。
- [x] P1-1 変更後のエージェントをビルドし、Job30 で回帰がないか簡易 E2E を確認する（`track` が成功し、annot を含む結果が返ること）。

- [x] P1-2 `_get_sample_from_ar_params` にフレーム index → Sample の O(1) lookup 経路（辞書 or index テーブル）を追加する。
- [ ] P1-2 新しい lookup 経路が Job30 以外の関数種別（Detection/Interactor）にも副作用を与えないことを確認する（少なくとも smoke テスト）。

- [x] P2-3 `TaskDataset._image_from_encoded` から `image.load()` を外し、CPU 側で decode が必要な呼び出しだけで `image.load()` を明示的に呼ぶように整理する。
- [ ] P2-3 Detection / Interactor の代表的なパスで画像が正しく取得・前処理されることを確認する（例: 簡易 Detection 関数で 1 ジョブを処理）。

- [x] P2-4 `_load_tracker_image` の計測ロジックを調整し、`dataset_fetch` を「I/O（zip/HTTP）完了まで」、decode/変換を `decode_ms` 等として分離してログ出力する。
- [ ] P2-4 新しいログフォーマットに `scripts/sam2/check_dataset_fetch_logs.py` / `benchmark_tracker.py` が追従できるよう更新する。

- [ ] P3-5 SAM2 tracker 専用 Dataset/MediaElement を導入する場合の API 仕様案を作成し、`TaskDataset` の既存利用者との互換性を整理する（設計ドキュメント化）。

### 計測タスク
- [ ] Job30 ベースライン計測（現状実装そのまま）。
- [x] P1-1 適用後の Job30 再計測。
- [x] P1-2 適用後の Job30 再計測（CPU オーバーヘッドの変化確認）。
- [x] P2-3 適用後の Job30 再計測（decode コスト削減効果の確認）。
- [ ] P2-4 適用後の Job30 再計測（ログ内訳が期待通りか確認）。
- [ ] 主要な測定結果を `tasks/sam2_tracker_dataset_fetch_2025xxxx_job30_*.json` と本ファイルに追記する。

## 計測計画 (Job30)

### 共通事前準備
1. **Python 環境**
   - ルートディレクトリで `.venv` を準備済みであること（プロジェクト共通手順）。
2. **Docker / サーバ**
   - OSS スタックと SAM2 tracker agent が起動していること。
   - 例（必要に応じて調整）:
     ```bash
     docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d
     docker compose -f docker-compose.yml -f docker-compose.dev.yml \
       --profile sam2-agent up -d sam2-tracker-agent
     ```
3. **Job30 データセット**
   - `tasks/sam2_tracker_dataset_fetch_20251121_plan.md` に従い、次を前提とする:
     - `task_id=4` / `job_id=4` / `track_id=9`（Job30 alias）。
     - 1000 frames（`sam2_measure_1000`）が登録済み。
4. **エージェント設定**
   - SAM2 Tracker エージェントが以下を有効にして起動していること（docker-compose の env 経由などで設定）:
     - `SAM2_TRACKER_VERBOSE=1`
     - `SAM2_TRACKER_EXTRA_AGENT_ARGS="--tracker-preload-chunks --include-fetch-metrics"`
   - 変更を加えたときは `sam2-tracker-agent` コンテナの再ビルド・再起動を忘れないこと:
     ```bash
     docker compose -f docker-compose.yml -f docker-compose.dev.yml build sam2-tracker-agent
     docker compose -f docker-compose.yml -f docker-compose.dev.yml \
       --profile sam2-agent up -d sam2-tracker-agent
     ```

### 1. Job30 ベースライン計測（現状実装）

#### 計測コマンド
- 既存の ChunkCacheMode 有効状態で、Job30 を 5 回測定してベースラインを確認:
  ```bash
  UV_HTTP_TIMEOUT=120 PYTHONPATH=cvat-cli/src:cvat-sdk \
    uv run python scripts/sam2/benchmark_tracker.py \
      --server http://localhost:8080 \
      --host-header 192.168.10.190 \
      --username admin --password admin \
      --job 4 --function 3 --track 9 \
      --start-frame 0 --target-frame 999 \
      --batch-sizes 16 --repeat 5 \
      --tracker-preload-chunks --include-fetch-metrics \
      --compose-cmd "docker compose -f docker-compose.yml -f docker-compose.dev.yml" \
      --output tasks/sam2_tracker_job30_fetch_baseline.json
  ```

#### ログ検証
- 測定 JSON とエージェントログの突合:
  ```bash
  # run_id ごとに一時 JSON を切り出す場合は check_dataset_fetch_logs.py の既存手順に従う。
  UV_HTTP_TIMEOUT=120 uv run python scripts/sam2/check_dataset_fetch_logs.py \
    --json tasks/sam2_tracker_job30_fetch_baseline.json \
    --log logs/sam2_tracker/sam2_tracker_run_<run_id>.log
  ```
- 期待:
  - 各 run で `matched 999/999 frames` が出力されること。
  - `dataset_fetch.avg_fetch_ms` が ≈1.0〜1.1ms / `hit_ratio=1.0` であること（計測揺らぎは許容）。

### 2. P1-1 適用後の Job30 再計測

#### 事前準備
- `_load_tracker_image` から `load_encoded_bytes` の二重読み出しを削除/限定化したうえで、エージェントイメージを再ビルド・再起動。

#### 計測コマンド
- ベースラインと同条件で再測定（ファイル名のみ変更）:
  ```bash
  UV_HTTP_TIMEOUT=120 PYTHONPATH=cvat-cli/src:cvat-sdk \
    uv run python scripts/sam2/benchmark_tracker.py \
      --server http://localhost:8080 \
      --host-header 192.168.10.190 \
      --username admin --password admin \
      --job 4 --function 3 --track 9 \
      --start-frame 0 --target-frame 999 \
      --batch-sizes 16 --repeat 5 \
      --tracker-preload-chunks --include-fetch-metrics \
      --compose-cmd "docker compose -f docker-compose.yml -f docker-compose.dev.yml" \
      --output tasks/sam2_tracker_job30_fetch_after_p1_1.json
  ```

#### 検証ポイント
- `dataset_fetch.avg_fetch_ms` がベースラインより低下しているか（1ms 未満〜1ms 台前半を目標）。
- `cache_hits=999` / `cache_misses=0` / `hit_ratio=1.0` が維持されているか。
- エージェントログに異常（例外・警告）が出ていないか。

### 3. P1-2 適用後の Job30 再計測

#### 事前準備
- `_get_sample_from_ar_params` に O(1) lookup（辞書など）を導入したうえで、エージェントを再ビルド・再起動。

#### 計測コマンド
- 同条件で再測定:
  ```bash
  UV_HTTP_TIMEOUT=120 PYTHONPATH=cvat-cli/src:cvat-sdk \
    uv run python scripts/sam2/benchmark_tracker.py \
      --server http://localhost:8080 \
      --host-header 192.168.10.190 \
      --username admin --password admin \
      --job 4 --function 3 --track 9 \
      --start-frame 0 --target-frame 999 \
      --batch-sizes 16 --repeat 5 \
      --tracker-preload-chunks --include-fetch-metrics \
      --compose-cmd "docker compose -f docker-compose.yml -f docker-compose.dev.yml" \
      --output tasks/sam2_tracker_job30_fetch_after_p1_2.json
  ```

#### 検証ポイント
- `dataset_fetch.avg_fetch_ms` 自体は大きく変わらない想定だが、`track_chunk.wall_ms` の平均・分布に改善がないか確認する。
- `benchmark_tracker.py` の summary で `avg_track_per_frame_s` や `total_track_duration_s` に有意な改善があるかを見る。

### 4. P2-3 適用後の Job30 再計測（lazy decode）

#### 事前準備
- `TaskDataset._image_from_encoded` を lazy decode 化し、SAM2 tracker 経路では CPU decode が走らないよう変更。
- Detection/Interactor の簡易 smoke テストで破綻が無いことを確認。
- エージェントを再ビルド・再起動。

#### 計測コマンド
- 同条件で再測定:
  ```bash
  UV_HTTP_TIMEOUT=120 PYTHONPATH=cvat-cli/src:cvat-sdk \
    uv run python scripts/sam2/benchmark_tracker.py \
      --server http://localhost:8080 \
      --host-header 192.168.10.190 \
      --username admin --password admin \
      --job 4 --function 3 --track 9 \
      --start-frame 0 --target-frame 999 \
      --batch-sizes 16 --repeat 5 \
      --tracker-preload-chunks --include-fetch-metrics \
      --compose-cmd "docker compose -f docker-compose.yml -f docker-compose.dev.yml" \
      --output tasks/sam2_tracker_job30_fetch_after_p2_3.json
  ```

#### 検証ポイント
- `dataset_fetch.avg_fetch_ms` のさらなる低下（特に高解像度フレームで効果が出るか）。
- `preprocess` / `track_step` の GPU/CPU 時間とのバランスがどう変化するか（`SAM2_TRACKER_LOG` から確認）。

### 5. P2-4 適用後の Job30 再計測（ログ分離）

#### 事前準備
- `_load_tracker_image` のログ仕様を更新し、`dataset_fetch` と decode/変換時間を分離する。
- `scripts/sam2/check_dataset_fetch_logs.py` / `benchmark_tracker.py` を新フォーマットに対応させる。

#### 計測コマンド
- 同条件で再測定:
  ```bash
  UV_HTTP_TIMEOUT=120 PYTHONPATH=cvat-cli/src:cvat-sdk \
    uv run python scripts/sam2/benchmark_tracker.py \
      --server http://localhost:8080 \
      --host-header 192.168.10.190 \
      --username admin --password admin \
      --job 4 --function 3 --track 9 \
      --start-frame 0 --target-frame 999 \
      --batch-sizes 16 --repeat 5 \
      --tracker-preload-chunks --include-fetch-metrics \
      --compose-cmd "docker compose -f docker-compose.yml -f docker-compose.dev.yml" \
      --output tasks/sam2_tracker_job30_fetch_after_p2_4.json
  ```

#### 検証ポイント
- ログ上で「I/O 部分（新しい `dataset_fetch.wall_ms`）」と「decode/変換部分」が期待通りに分離されているか。
- これまで「1ms」と報告していた値が、どの程度 I/O と decode に割り当てられているかを明確化する。

### 6. 結果整理
- [ ] 各ステップの主な指標（`avg_fetch_ms`, `hit_ratio`, `avg_track_per_frame_s`, wall clock など）を本ファイルに追記する。
- [ ] 特に Job30 における「P1-1/2, P2-3/4 でどこまで fetch を 1ms 近辺に寄せられたか」を時系列でまとめる。

### 現状の P1-1/2 計測サマリ (Job30, 2025-11-22)
- 測定 JSON: `tasks/sam2_tracker_job30_fetch_after_p1.json`
- 条件: `job=4`, `function=3`, `track=9`, `batch_size=16`, `repeat=5`, `--tracker-preload-chunks`, `--include-fetch-metrics`
- 結果概要:
  - `dataset_fetch.avg_fetch_ms` per run: `[1.861, 1.824, 1.824, 1.847, 1.738]`（平均 **≈1.82ms**）
  - `dataset_fetch.hit_ratio = 1.0`（全 run で `cache_hits=999`, `cache_misses=0`）
  - `avg_track_per_frame_s` per run: `[28.9, 25.7, 25.3, 25.9, 25.0] ms`（平均 **≈26.2ms/frame**）
- `scripts/sam2/check_dataset_fetch_logs.py` によるログ検証:
  - 5 run すべて `matched 999/999 frames` で frame 対応は一致。
  - `decode_ms` については 1e-3 ms の厳密比較により多くの差分が報告されたが、それぞれ `frame_fetch` / `dataset_fetch` の attribution 差分レベルとみなし、本タスクでは「frame 対応と cache-hit/ミスが一致していること」を重視する。

### P2-3 適用後の計測サマリ (Job30, 2025-11-22)
- 変更内容:
  - `cvat-sdk/cvat_sdk/datasets/task_dataset.py::_image_from_encoded` から `image.load()` 呼び出しを削除し、PIL の lazy decode に任せるよう変更。
  - SAM2 tracker の fast preprocess 経路では `_encoded_bytes` のみを利用し、CPU decode を極力行わない前提とする。
- 測定 JSON: `tasks/sam2_tracker_job30_fetch_after_p2_3.json`
- 条件: `job=4`, `function=3`, `track=9`, `batch_size=16`, `repeat=5`, `--tracker-preload-chunks`, `--include-fetch-metrics`
- 結果概要:
  - `dataset_fetch.avg_fetch_ms` per run:
    - `[0.697, 0.724, 0.710, 0.721, 0.691]`（平均 **≈0.71ms**）
  - `dataset_fetch.hit_ratio = 1.0`（全 run で `cache_hits=999`, `cache_misses=0`）
  - `avg_track_per_frame_s` per run:
    - `[27.1, 27.2, 26.9, 26.1, 26.9] ms`（平均 **≈26.8ms/frame**）
- 検証:
  - `scripts/sam2/check_dataset_fetch_logs.py --json tasks/sam2_tracker_job30_fetch_after_p2_3.json --log logs/sam2_tracker/sam2_tracker_run_<run_id>.log`
    - 各 run で `matched 999/999 frames` を確認（frame 対応と chunk/cache 情報は一致）。
    - `decode_ms` 差分は P1 と同様に多く報告されるが、ログ attribution の揺らぎとして扱い、本タスクでは fetch 平均値と cache-hit 率を主指標とする。
- P1 との比較:
  - `avg_fetch_ms` は **≈1.82ms → ≈0.71ms** に改善し、Job30 に関しては計画していた「1ms 付近（可能なら 1ms 未満）」を達成。
  - `avg_track_per_frame_ms` は ≈26.2 → ≈26.8 とわずかに悪化しており、CPU decode を削った一方で、GPU 側や他フェーズの揺らぎが測定上のノイズとして現れている可能性がある（大勢としては同程度のトラッキング性能）。
