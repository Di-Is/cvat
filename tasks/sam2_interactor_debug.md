# SAM2 Interactor: ブラウザ無出力の調査ログ

Web UI 上で SAM2 Interactor のセグメンテーションが描画されなくなった件について、原因を切り分けるための調査計画と進捗ログをまとめる。調査は以下の優先度で進める。

## TODO

- [x] **T1. API レイヤのレスポンス検証** — `docker compose logs` と簡易 CLI を使って `mask_rle`/`bounds`/`mask` が正しく返っているか確認する。
- [ ] **T2. UI 復号処理の動作確認** — Network レスポンスを保存し、`rle2Mask` と `bounds` の扱いが想定通りかブラウザ DevTools で確認する。
- [ ] **T3. フレームキャッシュ挙動の確認** — `_frame_cache` がヒットしているか、`low_res_mask_input` が次リクエストに繋がっているかをデバッグ出力やログで調べる。
- [ ] **T4. 旧実装との差分検証** — 旧 `func.py` に一時的に戻して再テストし、マスクが描画されるか比較する。
- [ ] **T5. UI フォールバック (`mask` フィールド) の利用状況確認** — `mask` 返却時に UI が適切に描画へフォールバックしているか console で確認する。

## T1. API レイヤのレスポンス検証

- **実施ログ (2025-11-15 22:15 JST)**:
  - `docker exec -i cvat_server python - <<'PY' ...` で `requests.Session()` を使い、`POST /api/jobs/3/functions/5/interactions`（`frame=0`, `pos_points=[[400,400]]`, `label_id=5`）を直接実行。
  - 応答は `400 {"detail":"Worker process crashed"}` で、`mask_rle`/`bounds`/`mask` は一切返ってこない。
  - 同時刻の `sam2-interactor-agent` ログには `ModuleNotFoundError: No module named 'PIL.JpegImagePlugin'` → `concurrent.futures.process.BrokenProcessPool` が出力され、AR `9c15cc2b-7536-43f2-86af-8b534456ecab` の失敗として記録されている (`docker logs sam2-interactor-agent --tail 2000 | sed -n '1700,1780p'`)。
  - したがって API 層では SAM2 推論結果以前に worker 自体がクラッシュしていることを確認。`PIL` プラグインの lazy import が `multiprocessing` の spawn プロセスで解決できていない可能性が高い。
- **次アクション**:
  - worker を複数回再起動してもクラッシュが再現しないため、`PIL.JpegImagePlugin` 事前 import はいったんロールバック済み。再発時のみ再適用し、原因切り分けを進める。
  - `docker compose -f docker-compose.yml -f docker-compose.dev.yml build sam2-interactor-agent` → `up -d sam2-interactor-agent` を都度実施し、`POST /api/jobs/3/functions/5/interactions` の 200 / `mask_rle` / `bounds` / `mask` を監視。
  - worker が安定したことを確認でき次第、ブラウザ側の T2/T5 調査に進む。

- **実施ログ (2025-11-16 10:05 JST)**:
  - `ai-models/interactor/sam2/func.py` と `ai-models/interactor/sam2/build/lib/func.py` へ `import PIL.JpegImagePlugin  # noqa: F401` と `PIL.Image.init()` を追加し、ワーカー起動前に JPEG プラグインをロードするよう修正。
  - これにより zipapp ビルド時にも `PIL/JpegImagePlugin.py` がバンドルされ、spawn 直後の `Image.open()` が `ModuleNotFoundError` を起こさない想定。
  - `docker compose -f docker-compose.yml -f docker-compose.dev.yml build sam2-interactor-agent` → `up -d sam2-interactor-agent` でコンテナを再生成（NVIDIA GPU 1枚構成、ビルド所要 ~20s）。

- **実施ログ (2025-11-15 22:28 JST)**:
  - ホスト側から `uv run --with requests python - <<'PY'` を実行し、`http://192.168.10.190:8080/api/auth/login` で `csrftoken` を取得。
  - そのまま `POST /api/jobs/3/functions/5/interactions`（frame=0, pos_points=[[400,400]], obj_bbox=[[200,200],[600,600]], label_id=5, start_with_box=True）を送信すると `200` が返り、レスポンスには `bounds=[196,186,594,596]`, `len(mask_rle)=3429`, `mask` (800x800) が含まれることを確認。
  - `docker logs sam2-interactor-agent --tail 400` に `ModuleNotFoundError` 等のスタックトレースは出力されず、起動メッセージのみで安定稼働を確認。

- **実施ログ (2025-11-15 22:33 JST)**:
  - 上記の `PIL.JpegImagePlugin` 事前 import / `PIL.Image.init()` 追加を削除し、`ai-models/interactor/sam2/func.py` と `build/lib/func.py` を元の import 群へ戻した。
  - `docker compose -f docker-compose.yml -f docker-compose.dev.yml build sam2-interactor-agent` → `up -d sam2-interactor-agent` を再実行。
  - 再び `uv run --with requests ... POST /api/jobs/3/functions/5/interactions` を呼ぶと `200` が返り、`bounds=[45,23,764,757]`, `len(mask_rle)=10969`, `mask` (800x800) が取得できた。
  - `docker logs sam2-interactor-agent --tail 200` には `ModuleNotFoundError` ではなく `AR 'd4fe41f9-...` 完了ログのみが残り、 worker 安定を確認。

## T2. UI 復号処理の動作確認

- **手順**:
  1. ブラウザ DevTools の Network タブで該当 API コールを開きレスポンス JSON をコピー。
  2. Console で `const bounds = [...]; const width = (bounds[2]-bounds[0])+1;` 等を手動計算し、`rle2Mask(response.mask_rle, width, height)` を実行して結果長さ・1 の総和を確認。
  3. `mask` フォールバックが走っているか (`response.mask` の truthy チェック) を `console.log` で監視。
- **メモ**:
  - `cvat-core/src/session-implementation.ts` の `mask_rle` 復号で例外が出ていないか要確認。例外があればブラウザ console に stack trace が出るためキャプチャする。

- **実施ログ (2025-11-16 11:10 JST)**:
  - `cvat-ui/src/components/annotation-page/standard-workspace/controls-side-bar/tools-control.tsx` を確認したところ、`convertMasksToPolygons=false`（デフォルト）の場合でも `canvasInstance.interact()` / `constructFromPoints()` の実行可否を `latestApproximatedPoints.length` のみで判定しており、SAM2 のようにポリゴン頂点を返さないインタラクタでは常に 0 のままになるため、マスクが描画されない / クリック完了時にオブジェクトが生成されないことを特定。
  - ガード条件を `convertMasksToPolygons` と `latestResponse.rle` の有無で分岐するよう修正し、マスクのみ返却されるケースでも RLE を使ってプレビュー/確定処理が走るよう更新した（`hasMaskRLE` → mask モードでも `canvasInstance.interact()`/`constructFromPoints()` が実行される）。
  - `yarn` コマンドが環境に存在せず `yarn workspace cvat-ui test tools-control.spec.ts` を実行できなかったため、UI テストは未実施。Node ランタイムが整い次第、`yarn install` → `yarn workspace cvat-ui test` でリグレッション確認を行う。
- **実施ログ (2025-11-16 11:55 JST)**:
  - マスクが右下にずれるのは SAM2 Interactor 固有の挙動であり、UI 側の `mask2Rle` ではなく SAM2 実装がフルフレームの `mask_rows` を返していたためであることを特定。`mask_rle` はバウンディング領域のみを対象にしているのに `mask_rows` が 800x800 全体を含むため、UI で再エンコードした際に先頭ゼロが膨大に含まれて `bounds` との整合が崩れていた。
  - `ai-models/interactor/sam2/func.py` の `_predict()` にて、`encode_mask` から得た `bounds` を使って `best_mask[top:bottom+1, left:right+1]` へクロップし、その領域のみを `mask_rows` として返すよう修正。これにより `mask` / `mask_rle` / `bounds` が同一座標系となり、UI での復元位置も一致する。
  - `mask_rows_from_bounds` ヘルパーを `mask_utils.py` として切り出し、モジュール単体テスト (`ai-models/interactor/sam2/tests/test_mask_rows.py`) を追加。ただし agent zipapp ではパッケージ解決が働かないため、`func.py` 実行時に `sys.path` へ自身のディレクトリを追加してから `mask_utils` を import するよう調整し、コンテナ内での `ModuleNotFoundError` を解消。
  - `uv run --with numpy --with pytest python -m pytest ai-models/interactor/sam2/tests/test_mask_rows.py` で 2 ケースともパス。`yarn workspace cvat-ui test tools-control.spec.ts` は引き続き未実行。Node/Yarn 追加後に UI 側の挙動確認を行う予定。

## T3. フレームキャッシュ挙動の確認

- **狙い**: `_frame_cache` がヒットせず毎回 `set_image()` している場合、`mask_input` が `None` のままになり得る。
- **手順**:
  1. `ai-models/interactor/sam2/func.py` の `_make_cache_key` 直後に `logging.debug` を仮追加し、`frame_key` と cache のヒット/ミスを出力。
  2. Agent コンテナ再ビルド後、連続クリックで `cache hit` が見られるか確認。
  3. `low_res_mask_input` が `None` → `array` に変わる瞬間をログで記録し、2回目以降のリクエストに `mask_input` が渡っているかを確認。
- **メモ**:
  - `.env` で `SAM2_INTERACTOR_CACHE_FRAMES=1` のままでもヒットしない場合、`context.frame_index` と UI からの `frame` が一致しているか疑う。

## T4. 旧実装との差分検証

- **狙い**: 直近のリファクタが原因か、もともと別レイヤに問題があるのか切り分ける。
- **手順**:
  1. `git checkout -- ai-models/interactor/sam2/func.py@{<commit>}` で既知の安定版に戻す（※ローカル退避推奨）。
  2. エージェントを再ビルドし、ブラウザで再度操作。
  3. マスクが描画される場合は新実装に絞り込む。描画されない場合は UI 側の問題なので別タスクへ切り分け。
- **メモ**:
  - 差分は `mask_rle`/`bounds` の扱いと `_frame_cache` 周り。再度新実装へ戻す前に `git diff` のバックアップを残す。

## T5. UI フォールバック (`mask`) の利用状況確認

- **狙い**: API から `mask` が渡っているのに UI が描画しない場合を検知。
- **手順**:
  1. ブラウザで `session-implementation.ts` の該当箇所に `debugger` か `console.log(response.mask)` を差し込み、`mask` が truthy かチェック。
  2. truthy なのに描画されない場合は `interactor-helpers.ts` で `mask` → canvas 変換に失敗していないか調査。
  3. `mask` が falsy の場合、Agent 側でフォールバックがセットされていないため `func.py` の `mask_rows` 作成処理 (`line 138` 付近) を再確認。
- **メモ**:
  - `mask` が巨大になり過ぎるとレスポンス圧縮や UI の JSON parse に時間がかかることがあるので、ファイルサイズも観測する。

## 進捗まとめ

- 現時点では各タスクの事前調査方針を整理した段階。次ステップでは T1 を実行し、得られた JSON と `bounds`/`mask` の整合性結果をここに追記する。
