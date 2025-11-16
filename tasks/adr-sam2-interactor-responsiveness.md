# ADR: Improve SAM2 Interactor Responsiveness on OSS

- Status: Proposed
- Date: 2025-11-15
- Owner: CVAT OSS Team

## Context
- OSS 版 AI Tools の SAM2 インタラクタ (`ai-models/interactor/sam2`, `sam2-interactor-agent`) は Enterprise 版より応答が遅いとユーザーから指摘されている。
- サーバーサイドでは `cvat/apps/functions/` による Annotation Request (AR) キューと `FunctionQueueWatchView` / `wait_for_interactor_request` がポーリング主体で実装されており、エージェント側 (`cvat-cli function run-agent`) も SSE が無い場合に 1 秒周期でポーリングするフォールバックしかない。
- エージェントはリクエストごとに `TaskDataset` を新規構築し (`cvat-cli/src/cvat_cli/_internal/agent.py:1007-1052`)、`MediaDownloadPolicy.FETCH_FRAMES_ON_DEMAND` で毎回フレームをダウンロードしている。このため GPU が待たされ、Enterprise 版のような即応性を再現できない。
- さらに `MAX_CONCURRENT_INTERACT_WAIT` の既定値 4 により `cvat/apps/functions/interactors.py` が同時待機数を制限しており、クリックを連打すると 429 を返す。

## Problem
- エージェントが新しい AR を検知するまでの待ち時間が `FunctionQueueWatchView` の 1 秒ポーリング＋3 秒クールダウンに依存しており、UI の操作開始から推論開始まで最大 1〜3 秒遅れてしまう。
- `wait_for_interactor_request` も 1 秒周期で DB を再読込するだけなので、推論完了後もレスポンスが 1 秒遅延する。
- フレームを毎回オリジナル品質でダウンロードするため、ネットワーク帯域と I/O がボトルネックになり GPU 利用率が低下する。特に同じフレームで追加ポイントを打つ場合に顕著となる。
- 同時待機スロット数が少なく、ユーザー体験として「応答がない」ように見えるケースが多発する。

## Decision
1. **イベント駆動のキュー通知を導入する**
   - AnnotationRequest の INSERT/UPDATE で PostgreSQL `LISTEN/NOTIFY` もしくは Redis pub/sub を発火し、`FunctionQueueWatchView` は DB ポーリングをやめて通知をストリームする。
   - CLI エージェントは既存の `_parse_event_stream` を流用できるため、通知間隔を最小限に抑えられる。フォールバックとして現在の 1 秒ポーリングは残す。
   - `QUEUE_WATCH_POLL_INTERVAL` や `QUEUE_WATCH_EVENT_COOLDOWN` は 100–200ms 程度の値に再設定し、SSE が途切れた場合でも遅延が 1 秒未満になるよう調整する。

2. **Interactor レスポンス取得を非ポーリング化する**
   - `wait_for_interactor_request` は DB 通知（`NOTIFY annotation_requests <ar_id>`）か Django Channels を使って完了イベントを待機する。少なくとも `poll_interval` を 0.2 秒に短縮し、`time.sleep` ではなく `condition.wait` + 通知を使って CPU 無駄を削る。
   - タイムアウト (`CVAT_FUNCTION_INTERACT_TIMEOUT`) の既定を 60 秒から 30 秒へ見直しつつ、UI にもリトライ動線を提示する。

3. **エージェント側でフレームキャッシュを導入**
   - `TaskDataset` をリクエストごとに再生成せず、エージェント起動中はタスク単位で LRU キャッシュする。`_TaskCacheLimiter` の `_MAX_TASKS_WITHOUT_CHUNKS` を `with_chunks=True` でも利用できるよう、`MediaDownloadPolicy.PRELOAD_ALL` を選択して zip チャンクを共有する。
   - キャッシュ階層は `/var/cache/huggingface` と同じボリュームを流用し、起動引数でサイズを制御できるようにする。TaskDataset 構築やフレームロードの計測ログを INFO で出力し、I/O がネックになっていないか監視できるようにする。

4. **同時待機数とバックプレッシャーの見直し**
   - `MAX_CONCURRENT_INTERACT_WAIT` を環境変数で調整可能にし、GPU 1 枚でも 8〜12 リクエスト程度を待機させられるようにする。
   - 429 を返すのではなく、サーバー側でリクエストを受理してキューに残し、UI には「処理待ち」を表示する。ユーザーがクリックを止めても順番に処理されるため、体感応答が向上する。

5. **計測と回帰テスト**
   - `FunctionQueueWatchView` と `wait_for_interactor_request` の開始〜完了を OpenTelemetry でロギングし、P95/P99 を Grafana で可視化する。
   - `tests/python/cli/test_cli_misc.py` にインタラクタ用の最小エンドツーエンドテストを追加し、キャッシュ経路でも結果が安定することを検証する。

## Consequences
- DB や Redis の通知導入によりインフラ構成がわずかに複雑化し、migration / rollout 時に追加の監視設定が必要になる。
- キャッシュ導入でディスク使用量が増えるため、`sam2-interactor-agent` 用のボリュームに最低 2〜4GB の空きが求められる。CI でもキャッシュをクリアするジョブを入れておく必要がある。
- 待機スロットを増やすと GPU 使用率が上がるため、単一 GPU で tracker と共有している環境では適切な `SAM2_AGENT_GPU_COUNT` / `CUDA_VISIBLE_DEVICES` の調整が必要。

## Validation Plan
1. `docker compose --profile sam2-agent up` した環境で AI Tools から 10 連続クリックを行い、リクエストからレスポンスまでの時間を UI / agent / server ログで計測する。
2. キャッシュ削除 (`rm -rf /var/cache/cvat-agent`) とあり・なし両方で再テストし、I/O 削減効果を記録する。
3. 429 が出なくなること、タイムアウト時に 504 が戻ることを e2e テスト (`tasks/sam2_interactor_e2e.md` の手順) に追記し Jenkins GPU ノードで実行する。

## Open Questions
- PostgreSQL の `LISTEN/NOTIFY` を採用する場合、マルチインスタンス構成でどの程度の通知遅延が発生するか？
- Frame キャッシュを共有するとジョブのアクセス権チェックを追加で行う必要があるか？
- CarrierWave 等で外部ストレージを使用している場合の帯域要件をどう計測するか？
