# SAM2.1 Interactor E2E 検証ログ（WIP）

`tasks/adr-sam2-ai-tools-interactor.md` の「CLI→agent→REST→UI」の検証TODOを完了させるための手順メモ。GPU リソース確保後、このファイルに実行ログを逐次追記する。

## 0. 前提 / 環境

- CVAT backend/frontend は `docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d` で起動済み。
- `.env` に下記の SAM2 系変数が設定されている（PAT は `CVAT_AGENT_TOKEN` に保存）。

```dotenv
CVAT_AGENT_TOKEN=<PAT>
SAM2_AGENT_CVAT_URL=http://cvat-server:8080
SAM2_AGENT_GPU_COUNT=1

SAM2_TRACKER_FUNCTION_ID=<後述の登録ID>
SAM2_TRACKER_MODEL_ID=facebook/sam2.1-hiera-small
SAM2_TRACKER_DEVICE=cuda

SAM2_INTERACTOR_FUNCTION_ID=<後述の登録ID>
SAM2_INTERACTOR_MODEL_ID=facebook/sam2.1-hiera-small
SAM2_INTERACTOR_DEVICE=cuda
SAM2_INTERACTOR_GPU_COUNT=1
```

- Python コマンドは `uv` ベースで実行する。リポジトリ直下で `UV_PROJECT_ENV=.venv` を export しておくと、`uv run` が `.venv` を使い回すので便利。
- GPU 非搭載環境では `SAM2_*_DEVICE=cpu` に変えたうえで `docker-compose.yml` の `deploy.resources.reservations.devices` をコメントアウトする必要がある。その構成は推論時間が極端に長くなるため、本ログでは GPU を前提にする。

## 1. 関数登録 (CLI)

```bash
export UV_PROJECT_ENV=.venv

# Tracker → ID=1
uv run --with cvat-cli --with ./ai-models/tracker/sam2 python -m cvat_cli \
  --server-host http://localhost --server-port 8080 --auth admin:Admin123! \
  function create-native "AI Tracker: SAM2" \
  --function-file ai-models/tracker/sam2/func.py \
  -p model_id=str:facebook/sam2.1-hiera-small \
  -p device=str:cuda

# Interactor → ID=2
PYTHONPATH="cvat-cli/src:cvat-sdk" uv run --with cvat-cli \
  --with ./ai-models/interactor/sam2 python -m cvat_cli \
  --server-host http://localhost --server-port 8080 --auth admin:Admin123! \
  function create-native "AI Interactor: SAM2" \
  --function-file ai-models/interactor/sam2/func.py \
  -p model_id=str:facebook/sam2.1-hiera-small \
  -p device=str:cuda
```

- 上記 2 コマンドの標準出力末尾に ID (1 / 2) が表示されたので `.env` に反映済み。
- Interactor 側は未リリースの `cvat-cli` / `cvat-sdk` を使うため、`cvat-sdk/gen/generate.sh` で API クライアントを再生成した後に `PYTHONPATH` を上書きして実行。

## 2. エージェント起動

- `ai-models/{tracker,interactor}/sam2/Dockerfile` を multi-stage にして pyproject/uv.lock/README + `cvat-cli`/`cvat-sdk` だけを依存レイヤへコピーし、`uv sync --frozen` を cache mount 付きで一度だけ実行。最後にソース一式と `.venv` をコピーするため、`func.py` などを編集しても `torch` 等の重量依存を再インストールせずに済む。
- `.dockerignore` から `cvat-cli` を除外し、`.venv/bin` を `PATH` へ追加。`dev/sam2-agent/entrypoint.sh`/`SAM2_AGENT_CVAT_URL` のデフォルトも `http://cvat-server:8080` へ揃え、Django の `DisallowedHost` を回避した。
- `.env` に `CVAT_AGENT_TOKEN` / `SAM2_*` を設定した状態で `docker compose --profile sam2-agent up -d --build` を実行すると、`sam2-(tracker|interactor)-agent` がGPU付きで常駐し、ログに `Connected to the queue event stream` が出力される。
- Compose 起動後はホスト側で追加の CLI を立ち上げずとも、Agent コンテナの `cvat-cli function run-agent` が常にキューを監視する。
- SAM2 Interactor は `SAM2_INTERACTOR_CACHE_FRAMES`（デフォルト `1`）フレーム分の `set_image()` 結果と低解像度マスクを LRU キャッシュする。連続クリック時の再現性が悪い場合は `.env` で `SAM2_INTERACTOR_CACHE_FRAMES=2` などへ引き上げ、GPU メモリ利用量とトレードオフする。

## 3. CLI → agent → REST の疎通確認

1. テスト用タスク作成（任意の小さな画像セット）:

   ```bash
   uv run --with cvat-cli python -m cvat_cli \
     --server-host http://localhost --auth <USER>:<PASS> \
     task create "sam2_e2e" --labels '[{"name":"obj"}]' local ./tests/assets/images/*
   ```

2. 生成された Task ID / Job ID をメモ。`cvat-cli task ls` で確認可能。

3. `docker compose logs sam2-interactor-agent --tail=20` で `Connected to the queue event stream` / `Trying to acquire an annotation request of category 'interactive'...` が繰り返されていることを確認。tracker 側も同様。

4. Agent が常駐した状態で、job=3 / function=2 へのインタラクション API を直接叩いて疎通を確認。

   ```bash
   uv run --with requests python - <<'PY'
   import requests
   sess = requests.Session()
   login = sess.post('http://localhost:8080/api/auth/login',
                     json={'username': 'admin', 'password': 'Admin123!'})
   login.raise_for_status()
   payload = {
       'frame': 0,
       'pos_points': [[400, 400]],
       'neg_points': [],
       'obj_bbox': [[200, 200], [600, 600]],
       'label_id': 5,
       'start_with_box': True,
   }
   resp = sess.post(
       'http://localhost:8080/api/jobs/3/functions/2/interactions',
       json=payload,
       headers={'X-CSRFToken': login.cookies['csrftoken']},
   )
   print(resp.status_code, resp.text[:120])
   resp.raise_for_status()

   data = resp.json()
   assert data.get('bounds'), 'mask_rle decoding window is missing'
   left, top, right, bottom = data['bounds']
   width = (right - left) + 1
   height = (bottom - top) + 1
   decoded = rle2Mask(data['mask_rle'], width, height)
   assert len(decoded) == width * height
   print('Decoded mask cells:', sum(decoded))
   PY
   ```

   200 が返り、レスポンスには `mask_rle` / `bounds` / `points` が含まれる。Agent コンテナ側のログにも `Annotation request completed` が残る。

   - `bounds` は `[left, top, right, bottom]` (inclusive) で、`width = (right-left)+1` / `height = (bottom-top)+1` を使って `rle2Mask` を復号できる。
   - UI との互換性を維持するため、`mask_rle` は run-length 数列のみで、`encode_mask` の末尾4要素は `bounds` に移した状態になっている。
   - 念のため `mask` (2D list) も返却しているので、`rle2Mask` の復号結果と `mask` の flatten を `allclose` で比較すると契約ずれを早期検知できる。

## 4. UI での Interactor 動作

- `.env` に `CVAT_SERVERLESS=yes` を追加して `docker compose up -d cvat_server` を実行。`GET /api/server/plugins` のレスポンスが `{'MODELS': True, ...}` になり、AI Tools プラグインが有効になる。
- `CVAT_UI_TASK_ID=5 CVAT_UI_JOB_ID=3 SAM2_UI_SCREENSHOT=tasks/screenshots/sam2_ui.png SAM2_UI_LOG=tasks/sam2_ui_playwright.log uv run --with playwright --with requests python scripts/sam2_interactor_ui.py`
  を実行し、AI Tools ボタンの展開と `Interactors` タブを自動操作。スクリーンショット（`tasks/screenshots/sam2_ui.png`）と `Interactors`/`Detectors`/`Trackers` などのタブ一覧を `tasks/sam2_ui_playwright.log` に保存。
- Playwright 実行時の Network ログには `GET /api/functions?page_size=all` が 200 で返り、`SAM2.1 Interactor (Native)` が Redux state に登録される。以降は UI 上のポジ/ネガポイント指定で `sam2-interactor-agent` 経由の推論が実行できる。

## 5. ログ記録テンプレート

| 日付 | 手順 | 結果 | メモ / ログ |
| --- | --- | --- | --- |
| 2025-11-15 | 関数登録 | ✅ Tracker ID=1 / Interactor ID=2 | `uv run --with cvat-cli ... function create-native ...` の stdout を `/tmp/sam2_native_function_ids.log` に保存。`.env` へ反映済み。 |
| 2025-11-15 | エージェント起動 (compose) | ✅ multi-stage + cache | `docker compose --profile sam2-agent up -d --build` 後、`sam2-(tracker|interactor)-agent` のログに `Connected to the queue event stream`。Dockerfile multi-stage 化で `uv sync` レイヤが再利用され、`SAM2_AGENT_CVAT_URL` も `http://cvat-server:8080` へ切替済み。 |
| 2025-11-15 | REST (UI相当) | ✅ `POST /api/jobs/3/functions/2/interactions` → 200 | `uv run --with requests ...` で job=3 / label_id=5 / bbox=[[200,200],[600,600]] を送信。`mask_rle` が返り、Agent ログにも `Annotation request completed` を確認。 |
| 2025-11-15 | UI (AI Tools) | ✅ Playwright で Interactors 表示 | `CVAT_SERVERLESS=yes` で `/api/server/plugins` -> `MODELS: true`。`scripts/sam2_interactor_ui.py` を Playwright で実行し、`tasks/screenshots/sam2_ui.png` / `tasks/sam2_ui_playwright.log` にタブ情報（Interactors/Detectors/Trackers）を保存。 |

## 6. 今後のタスク

- [ ] SAM2.1 Interactor の操作動画 (GIF) と CLI ログを `tasks/sam2_oss_summary.md` / ドキュメントへ転載。
- [ ] Compose プロファイル (`sam2-agent`) の README/サイト向け手順を更新し、`CVAT_SERVERLESS` / `SAM2_AGENT_CVAT_URL` 変更点を明記。
- [ ] Agent イメージのビルド成果物を CI に追加し、キャッシュヒット率や `uv sync` 所要時間をメトリクス化。
