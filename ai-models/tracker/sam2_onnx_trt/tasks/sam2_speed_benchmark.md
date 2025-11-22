# SAM2 処理時間計測タスク

## 対象
- モデル: SAM2（現状の PyTorch 実装）
- 入力データ: `experiments/data/XXXX225-02_frames_500` 配下のフレーム画像

## 作業計画
- 現状の推論パイプラインを確認し、1フレームあたりの処理フローを整理する
- `experiments/data/XXXX225-02_frames_500` を用いて、現行実装の処理時間を計測する
  - 画像エンコーダ処理時間
  - マスクデコーダ処理時間（ポリゴン指定＋VOS 追跡を想定）
  - 動画全体（500フレーム）に対する総処理時間
- 計測結果からボトルネックとなるパーツを特定する
- ONNX 化候補（エンコーダ／デコーダなど）を決定する

## 実行コマンド（作業時に追加）
- 既存のコンポーネントプロファイラを利用して、XXXX225-02 の500フレームを計測する  
  - GPU (CUDA) 想定:
    - `PYTHONPATH=. SAM2_TRACKER_VOS_OPTIMIZED=0 uv run experiments/profile_components.py --frames-dir experiments/data/XXXX225-02_frames_500 --limit 500 --device cuda --model-id facebook/sam2.1-hiera-small --output experiments/data/profile_copg225_cuda.json`
  - GPU が使えない場合（CPU計測）:
    - `PYTHONPATH=. SAM2_TRACKER_VOS_OPTIMIZED=0 uv run experiments/profile_components.py --frames-dir experiments/data/XXXX225-02_frames_500 --limit 500 --device cpu --model-id facebook/sam2.1-hiera-small --output experiments/data/profile_copg225_cpu.json`
- 前提のワークフローは「初期フレームでポリゴン指定によりオブジェクトを初期化し、その後 499 フレームを自動追跡する VOS セットアップ」とする

## 作業結果（作業完了後に記載）
- 計測条件:
  - 使用マシン・GPU: NVIDIA GeForce RTX 4080 (16 GB)
  - バッチサイズ: 1 フレーム / 呼び出し
  - 精度設定（FP32/FP16 等）: 自動混合精度 (GPU bfloat16, TF32 許可) / `vos_optimized=False`
- 計測結果（1フレームあたり / 動画全体）:
  - 前処理＋画像エンコーダ平均時間（`preprocess_total` / `vision_backbone`）: 約 26.2 ms / 10.0 ms （1フレームあたり）
  - メモリエンコーダ平均時間（`memory_encoder`）: 約 1.55 ms （1フレームあたり）
  - メモリアテンション平均時間（`memory_attention`）: 約 9.33 ms （1フレームあたり）
  - プロンプトエンコーダ平均時間（`prompt_encoder`、1フレームあたり）: 約 0.60 ms
  - マスクデコーダ平均時間（`mask_decoder`、1フレームあたり）: 約 2.73 ms
  - 総処理時間（500フレーム全体、`preprocess_total` / `track_step_total` ベース）: 約 21.1 秒（≒ 42.3 ms / フレーム, 約 23.6 FPS 相当）
- ボトルネック分析と今後の改善方針:
  - `preprocess_total` 中の `vision_backbone`（約 10 ms/フレーム）と、`track_step_total` 中の `memory_attention`（約 9.3 ms/フレーム）が主要なボトルネック
  - 「1枚目のエンコード＋その後の 499 フレーム追跡」という VOS パイプラインでは、画像エンコーダ（backbone）と動画メモリ関連（memory_attention, memory_encoder）を優先して ONNX 化候補とする
  - ユーザ入力は初期フレームでのポリゴン指定 1 回のみであり、その後の各フレームでは `prompt_encoder`＋`mask_decoder` が 1 回ずつ実行される形のため、体感速度への影響はバックボーンとメモリアテンションが支配的と見なせる
