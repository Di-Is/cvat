# ADR: SAM2.1 Interactorの座標・マスク整合性の確立

- Status: Accepted
- Date: 2025-11-15
- Owner: OSS SAM2 チーム

## 背景
- 現行のエージェント (`ai-models/interactor/sam2/func.py`) は `SAM2ImagePredictor` を直接呼び出し、クリック座標や矩形を UI/CLI から受け取って推論している。
- SAM2 本体の `SAM2Transforms` は「入力画像を長辺=1024へリサイズし、座標は `normalize_coords=True` のとき 0-1 正規化を想定する」実装になっている（`sam2/sam2_image_predictor.py`, `sam2/utils/transforms.py`）。
- UI 側では `mask_rle` の末尾に境界情報が含まれているとは想定しておらず、bounds 配列を使って `rle2Mask` を復元している（`cvat-core/src/session-implementation.ts:685-714`）。
- PR [#8610](https://github.com/cvat-ai/cvat/pull/8610) の serverless 版 SAM2 実装は上記前提を守り、クリック前処理やマスク復号の手順が整っている。

## 課題
1. **座標とマスクの二重スケーリング**: 現在の agent では `normalize_coords` を False に書き換えたり、`SAM2ImagePredictor` が既に元解像度へアップスケール済みのマスクに対して PIL で再リサイズしている箇所が残っており（`ai-models/interactor/sam2/func.py:75-118`）、SAM2Transforms の前提と二重に衝突している。
2. **RLE/bounds の契約不一致**: `encode_mask` は run-length 配列の末尾に `[x1, y1, x2-1, y2-1]` を追加する仕様だが（`cvat-sdk/cvat_sdk/masks.py:13-52`）、agent はこの末尾4要素を取り除かずに `mask_rle` として返し、さらに `bounds` には独自計算の (x_max+1, y_max+1) を送っている。結果として UI 側の復号がズレ、クリック位置と離れたマスクが描画される。
3. **前処理と応答キャッシュの欠如**: PR #8610 では 1024 スケールに合わせたクリック正規化や low-res mask のキャッシュがあり、連続クリック時に encoder を再実行しない。現行 agent は毎リクエストで `set_image()` をやり直すため、挙動の再現性とレスポンスが不安定になりがち。

## 改善案 / 決定
1. **SAM2 標準の座標正規化に戻す**
   - `normalize_coords=True` を固定し、入力座標は UI からのピクセル値をそのまま渡す（`SAM2Transforms` が `orig_hw` から自動的に 0-1 正規化し 1024 スケールに変換する）。
   - `_resize_mask` のような追加リサイズを削除し、`SAM2ImagePredictor._predict()` が返す元解像度マスクをそのまま encode する。
   - 併せて docstring/コメントで「座標はピクセル値」「マスクは predictor が元解像度に戻す」と仕様を明記する。
2. **`mask_rle`/`bounds` の整合性を取る**
   - `encode_mask` の戻り値から末尾4要素を取り外し run-length 部だけを `mask_rle` に設定する。
   - 取り外した `[x1, y1, x2-1, y2-1]` をそのまま `bounds` として返すことで、UI 側の `width = (right - left) + 1` 計算と一致させる（`cvat-core/src/session-implementation.ts:696-705` に準拠）。
   - 既存の `_calculate_bounds` は不要になるため削除するか、`encode_mask` の結果から復元する単純ヘルパーに置き換える。
3. **レスポンスデータのフォールバック**
   - RLE 経路の修正が完了するまでは、`mask`（2D list）も option で返せるようにし、UI 側の復号が失敗した際に備える。
4. **前処理／キャッシュの強化**
   - フレーム単位で `SAM2ImagePredictor.set_image()` の結果を LRU キャッシュし、同じフレームに対する連続クリックでは埋め込みを再利用する。PR #8610 と同様に low-res mask も保持すれば連続リクエストの品質と速度向上が見込める。
   - `mask_threshold`, `max_hole_area`, `max_sprinkle_area` のデフォルト値は PR #8610 の `SAM2Transforms(..., mask_threshold=0.0)` に合わせ、CLI から上書きできるよう README に明記する。
5. **テストとドキュメント**
   - `tasks/sam2_interactor_e2e.md` に単点/矩形プロンプトの再現手順を追加し、RLE + bounds が一致するかを Playwright/E2E で検証する。
   - ADR（本書）と README に、UI とのデータ契約（座標系、RLE/bounds、キャッシュ戦略）を追記する。

## 影響と次のアクション
- 既存 agent のインターフェイスは変わらないため、UI 側は追加変更不要。ただし `mask_rle` の形式が変わるため、互換性のために agent 側でのみ実装する。
- LRU キャッシュは GPU メモリを消費するため、デフォルトサイズは 1フレーム程度とし、環境変数で調整できるようにする。
- 実装完了後、`docker compose --profile sam2-agent up -d --build` → AI Tools 手動確認 → `tasks/sam2_interactor_e2e.md` 更新という検証フローを必須にする。

## 実装状況メモ (2025-11-15)
- `ai-models/interactor/sam2/func.py` / `build/lib/func.py` にて `encode_mask` の run-length と bounds を分離し、UI 復号向けの `bounds`/`mask` フィールドを返却。`normalize_coords=True` 前提に戻し、`SAM2ImagePredictor` のアップスケール結果をそのまま利用するよう `_resize_mask` を撤廃。
- クリックごとの `set_image()` / 低解像度マスクを LRU キャッシュ化（`SAM2_INTERACTOR_CACHE_FRAMES` で枠数変更可能）。同じフレームの連続操作でも encoder の再実行を避ける。
- `ai-models/interactor/sam2/README.md` と `tasks/sam2_interactor_e2e.md` に UI とのデータ契約（`mask_rle`/`bounds`/`mask`）と手動検証・キャッシュ設定手順を追記。
