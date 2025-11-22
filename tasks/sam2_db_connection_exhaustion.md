# SAM2 Tracker/Interactor: DB Connection Exhaustion

## 概要
- `sam2-tracker-agent` / `sam2-interactor-agent` が `/api/functions/queues/<queue_id>/requests/acquire` を短周期で叩き続けており、サーバーが 500 (`Internal Server Error`) を返している。
- `cvat_server` のアプリログでは `django.db.utils.OperationalError: FATAL: sorry, too many clients already` が連続発生し、PostgreSQL の `max_connections (100)` を使い切っている。
- `FunctionQueueWatchView`（`cvat/apps/functions/views.py:67-138`）が返す SSE ストリームが無期限で走り続ける実装になっており、各リクエストが終わるまで Django の DB コネクションが解放されないため、SSE 再接続を繰り返すエージェントが接続リークを起こしている。
- ドキュメントの計画（`tasks/sam2_oss_summary.md:30-65`）では SSE を 30 秒で強制切断し再接続させる設計だが、現実装は keep-alive と snapshot のみで、明示的な切断や `close_old_connections()` も存在しない。

## 調査ログ
- `docker logs sam2-tracker-agent --tail 2000 | rg "ERROR"`
  - `cvat_sdk.api_client.exceptions.ServiceException: Status Code: 500` が `/api/functions/queues/function:{2|3}/requests/acquire` で多発。
- `docker logs cvat_server --tail 2000 | rg "Internal Server Error"`
  - `/api/functions/queues/function:{2|3}/requests/acquire` / `.../watch` に対して `OperationalError: FATAL: sorry, too many clients already`（`psycopg2` 経由）が記録される。
- `docker exec cvat_server ss -tn | grep 5432 | wc -l` → 100
  - アプリ pod から `cvat_db` への TCP コネクションが 100 本張りっぱなし。
- `docker exec cvat_db ps -ef | grep 'root cvat 172.18.0.4' | wc -l` → 100
  - すべて `cvat_server`（172.18.0.4）からの backend session で塞がっており、他のワーカーからの接続余地がない。
- `docker exec cvat_db sh -c "grep -n 'max_connections' /var/lib/postgresql/data/postgresql.conf"` → `max_connections = 100`
- `docker exec cvat_server ps -T -p 493`
  - `uvicorn` プロセス（PID 493）が 100+ スレッドを抱えており、SSE ストリーム処理が積み上がっている。

## 原因と関連コード
1. **SSE ストリーム継続による DB コネクション占有**
   - `FunctionQueueWatchView.get()`（`cvat/apps/functions/views.py:70-88`）は `StreamingHttpResponse` を返して `_queue_event_stream()` を呼ぶ。
   - `_queue_event_stream()`（同ファイル `cvat/apps/functions/views.py:246-319`）は `AnnotationRequest.objects.filter(...)` など ORM をループ内で実行しつづけるが、ストリームを一定時間で閉じる処理や `close_old_connections()` がない。
   - `notifications.queue_listener()`（`cvat/apps/functions/notifications.py:46-86`）は Redis Pub/Sub を使うが、Django 側のレスポンスは 1 リクエスト＝1 DB コネクションを掴んだまま。
2. **計画仕様との乖離**
   - `tasks/sam2_oss_summary.md:30-65` では「SSE を 30 秒で閉じる」「2 秒ごとの keep-alive」「再接続前提」などが明記されているが、実装は `snapshot_deadline` / `keepalive_deadline` のみで、終了条件が存在しない。
3. **PostgreSQL 側の制限**
   - Compose 既定の `postgres:15-alpine` は `max_connections=100`。`superuser_reserved_connections` も 3 のため、100 本すべて一般接続に消費されると新規コネクションは受け付けられない。

## 影響
- tracker/interactor エージェントはいずれも AR を取得できず、SAM2 トラッキング／インタラクションが停止。
- Django 側の他エンドポイントも DB コネクション取得に失敗し、UI の通常操作にも影響が及ぶ恐れがある。
- ログが 500 エラーで埋まり、他問題の検知が難しくなる。

## 暫定措置案
1. `docker compose restart cvat_server cvat_db` で DB コネクションを解放し、エージェントを一時的に復旧させる。
2. 500 が止まるまで `docker logs cvat_server` / `docker exec cvat_server ss -tn | grep 5432 | wc -l` を監視。

## 本修正候補
1. **SSE の寿命制御**
   - `_queue_event_stream()` で 30 秒程度のタイムアウトを設け、ループを抜ける前に `close_old_connections()`（`django.db`）を呼んで DB コネクションを返す。
   - もしくは `StreamingHttpResponse` を返す View 側で `request` が完了したタイミングで `con.close()` されるよう、ORM を使わないストリーム構造にする。
2. **接続数の上限緩和（バックアップ案）**
   - `postgresql.conf` の `max_connections` を引き上げる（例: 200）ことで緊急時の枯渇を防ぐ。ただし根本解決ではない。
3. **監視**
   - `docker exec cvat_db ps -ef | grep 'root cvat'` や `pg_stat_activity` を定期的に確認できるスクリプトを追加し、接続が異常に増えたら通知する。

## 引き継ぎメモ
- 修正対象コードは `cvat/apps/functions/views.py`（SSE 実装）と関連する `notifications.py`。テストは `cvat/apps/functions/tests/test_api.py` に watch/acquire のシナリオがあるので追加可能。
- 再現方法:
  1. SAM2 tracker/interactor エージェントを稼働させる（`docker logs sam2-*-agent` で確認）。
  2. `docker exec cvat_server ss -tn | grep 5432 | wc -l` を観測。30～40 を超えて徐々に増え、やがて 100 に達すると `cvat_server` ログに `OperationalError` が出始める。
  3. その後 `docker logs sam2-*-agent | rg "Status Code: 500"` でエラーを確認。
- 修正後は以下を再チェックすること:
  - `FunctionQueueWatchView` のレスポンスが 30 秒程度で閉じ、クライアントが自動再接続する。
  - DB コネクション数が安定（常時 100 を消費しない）。
  - tracker/interactor が正常に AR を取得し、`/api/functions/queues/.../requests/acquire` が 200 を返す。

