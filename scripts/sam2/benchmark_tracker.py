#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import json
import shlex
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests


@dataclass
class _TrackerRunMeta:
    batch_size: int
    run_id: str
    initial_request_id: str
    submitted_at: float
    submit_latency: float


@dataclass
class _RunCompletion:
    status: str
    payload: dict[str, object]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fire tracker actions with multiple batch sizes and summarize durations.",
    )
    parser.add_argument("--server", default="http://localhost:8080", help="CVAT server base URL.")
    parser.add_argument(
        "--host-header",
        default="192.168.10.190",
        help="Value for the Host header (omit to skip overriding).",
    )
    parser.add_argument("--username", default="admin", help="CVAT username.")
    parser.add_argument("--password", default="admin", help="CVAT password.")
    parser.add_argument("--job", type=int, required=True, help="Job id to run tracker against.")
    parser.add_argument(
        "--function",
        type=int,
        required=True,
        help="Function id (tracker) that the agent listens to.",
    )
    parser.add_argument("--track", type=int, required=True, help="Track id to extend.")
    parser.add_argument(
        "--start-frame",
        type=int,
        default=0,
        help="Starting frame index that already contains a keyframe.",
    )
    parser.add_argument(
        "--target-frame",
        type=int,
        required=True,
        help="Target frame index (inclusive) to stop tracking at.",
    )
    parser.add_argument(
        "--batch-sizes",
        type=int,
        nargs="+",
        default=[1, 8, 16, 32],
        help="Batch size candidates to test.",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=2.0,
        help="Seconds to wait between run status polls.",
    )
    parser.add_argument(
        "--compose-cmd",
        default="docker compose",
        help="Command used to invoke docker compose (default: 'docker compose').",
    )
    parser.add_argument(
        "--agent-service",
        default="sam2-tracker-agent",
        help="Compose service name for the tracker agent (used with --include-fetch-metrics).",
    )
    parser.add_argument(
        "--server-log-service",
        default="cvat_server",
        help="Compose service name for the CVAT server (used with --include-server-timing).",
    )
    parser.add_argument(
        "--psql-service",
        default="cvat_db",
        help="Compose service name for Postgres.",
    )
    parser.add_argument("--psql-db", default="cvat", help="Postgres database name.")
    parser.add_argument("--psql-user", default="root", help="Postgres username.")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("tasks/sam2_tracker_batch_measurements.json"),
        help="Path to store the JSON measurements.",
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="Number of times to repeat each batch size measurement.",
    )
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help="Do not hit the REST API. Instead, emit the planned annotation-request chunks for each batch size.",
    )
    parser.add_argument(
        "--tracker-preload-chunks",
        action="store_true",
        help="Annotate measurements as having tracker chunk preload enabled (for documentation only).",
    )
    parser.add_argument(
        "--include-fetch-metrics",
        action="store_true",
        help="Capture SAM2 tracker agent logs and embed dataset fetch metrics into the measurement JSON.",
    )
    parser.add_argument(
        "--include-server-timing",
        action="store_true",
        help="Capture SAM2 tracker server logs and embed queue/apply timing into the measurement JSON.",
    )
    parser.add_argument(
        "--agent-log-dir",
        type=Path,
        default=Path("logs/sam2_tracker"),
        help="Directory to store raw tracker agent logs when --include-fetch-metrics is set.",
    )
    parser.add_argument(
        "--server-log-dir",
        type=Path,
        default=Path("logs/sam2_tracker_server"),
        help="Directory to store raw server logs when --include-server-timing is set.",
    )
    return parser.parse_args()


def _safe_uuid(value: str) -> str:
    allowed = set("0123456789abcdef-")
    lowered = value.lower()
    if not set(lowered) <= allowed:
        raise ValueError(f"Unexpected characters in UUID {value!r}")
    return lowered


def _start_tracker_run(
    *,
    session: requests.Session,
    server: str,
    headers: dict[str, str],
    job_id: int,
    function_id: int,
    track_id: int,
    start_frame: int,
    target_frame: int,
    batch_size: int,
) -> _TrackerRunMeta:
    submitted_at = time.time()
    response = session.post(
        f"{server}/api/jobs/{job_id}/functions/{function_id}/tracker-actions",
        json={
            "frame": start_frame,
            "target_frame": target_frame,
            "track_ids": [track_id],
            "batch_size": batch_size,
        },
        headers=headers,
        timeout=120,
    )
    response.raise_for_status()
    payload = response.json()
    return _TrackerRunMeta(
        batch_size=batch_size,
        run_id=payload["run_id"],
        initial_request_id=payload["initial_request_id"],
        submitted_at=submitted_at,
        submit_latency=response.elapsed.total_seconds(),
    )


def _wait_for_completion(
    *,
    session: requests.Session,
    server: str,
    headers: dict[str, str],
    run_id: str,
    poll_interval: float,
    timeout_s: float = 600,
) -> _RunCompletion:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        response = session.get(
            f"{server}/api/functions/runs/{run_id}",
            headers=headers,
            timeout=120,
        )
        response.raise_for_status()
        payload = response.json()
        status = str(payload.get("status", "")).lower()
        if status in {"done", "failed", "cancelled"}:
            return _RunCompletion(status=status, payload=payload)
        time.sleep(poll_interval)
    raise TimeoutError(f"Run {run_id} did not complete within {timeout_s} seconds")


def _fetch_annotation_requests(
    *,
    compose_cmd: list[str],
    psql_service: str,
    psql_db: str,
    psql_user: str,
    run_id: str,
) -> list[dict]:
    safe_run_id = _safe_uuid(run_id)
    sql = f"""
        SELECT row_to_json(t)
        FROM (
            SELECT
                id::text AS id,
                type,
                parameters->>'type' AS ar_type,
                (parameters->>'frame')::int AS frame,
                parameters->'frames' AS frames,
                parameters->'pending_frames' AS pending_frames,
                COALESCE((parameters->>'batch_size')::int, NULL) AS batch_size,
                CASE WHEN parameters ? 'frames'
                     THEN jsonb_array_length(parameters->'frames')
                     ELSE NULL
                END AS frame_count,
                EXTRACT(EPOCH FROM (updated_at - created_at)) AS duration_s,
                EXTRACT(EPOCH FROM created_at) AS created_ts,
                EXTRACT(EPOCH FROM updated_at) AS updated_ts
            FROM functions_annotationrequest
            WHERE parameters->>'function_run_id' = '{safe_run_id}'
            ORDER BY created_at
        ) AS t;
    """
    cmd = [
        *compose_cmd,
        "exec",
        "-T",
        psql_service,
        "psql",
        "-q",
        "-t",
        "-A",
        "-d",
        psql_db,
        "-U",
        psql_user,
        "-c",
        sql,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    rows: list[dict] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    if not rows:
        raise RuntimeError(f"No annotation requests were found for run {run_id}")
    return rows


def _summarize_requests(rows: Iterable[dict]) -> dict[str, object]:
    rows = list(rows)
    track_rows = [row for row in rows if row.get("type") == "track"]
    total_frames = sum(int(row.get("frame_count") or 0) for row in track_rows)
    total_track_duration = sum(float(row["duration_s"]) for row in track_rows)
    chunks = []
    for row in track_rows:
        frame_count = int(row.get("frame_count") or 0)
        per_frame = (float(row["duration_s"]) / frame_count) if frame_count else None
        chunks.append(
            {
                "request_id": row["id"],
                "frames": row.get("frames"),
                "frame_count": frame_count,
                "duration_s": float(row["duration_s"]),
                "per_frame_s": per_frame,
            }
        )
    init_request = next((row for row in rows if row.get("type") == "init_tracking"), None)
    wall_clock = max(float(row["updated_ts"]) for row in rows) - min(
        float(row["created_ts"]) for row in rows
    )
    summary: dict[str, object] = {
        "track_chunks": len(track_rows),
        "total_frames": total_frames,
        "total_track_duration_s": total_track_duration,
        "avg_track_per_frame_s": total_track_duration / total_frames if total_frames else None,
        "chunks": chunks,
        "wall_clock_s": wall_clock,
    }
    if init_request:
        summary["init_duration_s"] = float(init_request["duration_s"])
    return summary


def _format_compose_cmd(raw: str) -> list[str]:
    tokens = shlex.split(raw)
    if not tokens:
        raise ValueError("compose command must not be empty")
    return tokens


def _format_timestamp(ts: float) -> str:
    dt = datetime.fromtimestamp(ts, timezone.utc)
    return dt.isoformat()


def _collect_agent_logs(
    *,
    compose_cmd: list[str],
    agent_service: str,
    since_ts: float,
    output_path: Path,
) -> str:
    since_arg = _format_timestamp(max(since_ts, 0))
    cmd = [*compose_cmd, "logs", agent_service, "--since", since_arg]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(result.stdout, encoding="utf-8")
    return result.stdout


def _collect_server_logs(
    *,
    compose_cmd: list[str],
    server_service: str,
    since_ts: float,
    output_path: Path,
) -> str:
    since_arg = _format_timestamp(max(since_ts, 0))
    cmd = [*compose_cmd, "logs", server_service, "--since", since_arg]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(result.stdout, encoding="utf-8")
    return result.stdout


def _parse_tracker_log_value(raw: str) -> Any:
    lowered = raw.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if raw.startswith("[") and raw.endswith("]"):
        try:
            return ast.literal_eval(raw)
        except Exception:
            return raw
    try:
        if "." in raw or "e" in lowered:
            return float(raw)
        return int(raw)
    except ValueError:
        return raw


def _parse_tracker_logs(raw_text: str, *, marker: str = "SAM2_TRACKER_LOG") -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for line in raw_text.splitlines():
        if marker not in line:
            continue
        payload = line.split(marker, 1)[1].strip()
        try:
            entry = json.loads(payload)
        except json.JSONDecodeError:
            # fall back to simple key=value parsing
            entry = {}
            for token in payload.split():
                if "=" not in token:
                    continue
                key, value = token.split("=", 1)
                entry[key] = _parse_tracker_log_value(value)
        if entry:
            entries.append(entry)
    return entries


def _coerce_frame_list(value: Any) -> list[int]:
    if value is None:
        return []
    if isinstance(value, list):
        return [int(v) for v in value]
    if isinstance(value, (int, float)):
        return [int(value)]
    if isinstance(value, str):
        try:
            parsed = ast.literal_eval(value)
        except Exception:
            return []
        if isinstance(parsed, list):
            return [int(v) for v in parsed]
        if isinstance(parsed, (int, float)):
            return [int(parsed)]
    return []


def _extract_dataset_fetch_metrics(
    *,
    log_entries: list[dict[str, Any]],
    run_id: str,
    chunk_frames: list[int],
    tracker_preload: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    per_frame: dict[int, dict[str, Any]] = {}
    cache_stats = {
        "prefetch_enabled": tracker_preload,
        "cache_bytes_peak": None,
        "cache_hits": 0,
        "cache_misses": 0,
        "evictions": None,
    }
    for entry in log_entries:
        entry_run = entry.get("run")
        if run_id and entry_run and str(entry_run) != run_id:
            continue
        if entry.get("phase") == "dataset_fetch":
            frames = _coerce_frame_list(entry.get("frames"))
            chunk_id = entry.get("chunk_id")
            cached = entry.get("cached")
            if isinstance(cached, str):
                cached = cached.lower() == "true"
            download_ms = float(entry.get("download_ms") or 0.0)
            decode_ms = float(entry.get("decode_ms") or 0.0)
            for frame in frames:
                per_frame[int(frame)] = {
                    "chunk_id": chunk_id,
                    "cached": cached,
                    "download_ms": download_ms,
                    "decode_ms": decode_ms,
                }
        elif entry.get("phase") == "frame_fetch":
            frame_index = entry.get("frame_index")
            if frame_index is None:
                continue
            try:
                frame_int = int(frame_index)
            except (TypeError, ValueError):
                continue
            cached_flag = entry.get("cached")
            if isinstance(cached_flag, str):
                cached_flag = cached_flag.lower() == "true"
            download_ms = float(entry.get("wall_ms") or 0.0)
            decode_ms = float(entry.get("decode_ms") or 0.0)
            per_frame[frame_int] = {
                "chunk_id": entry.get("chunk_id"),
                "cached": cached_flag,
                "download_ms": download_ms,
                "decode_ms": decode_ms,
            }
        elif entry.get("phase") == "dataset_cache":
            event = str(entry.get("event") or "").lower()
            if event in {"evict", "eviction"}:
                cache_stats["evictions"] = (cache_stats["evictions"] or 0) + 1
            cache_bytes = entry.get("cache_bytes") or entry.get("cost_bytes")
            if cache_bytes is not None:
                try:
                    cache_value = int(float(cache_bytes))
                except (TypeError, ValueError):
                    cache_value = None
                if cache_value is not None:
                    current_peak = cache_stats["cache_bytes_peak"] or 0
                    cache_stats["cache_bytes_peak"] = max(current_peak, cache_value)

    frames_seq = chunk_frames
    dataset = {
        "frames": [],
        "chunk_ids": [],
        "cached": [],
        "download_ms": [],
        "decode_ms": [],
        "per_frame_fetch_ms": [],
        "avg_fetch_ms": None,
        "hit_ratio": None,
        "log_missing": False,
    }

    for frame in frames_seq:
        dataset["frames"].append(frame)
        record = per_frame.get(frame)
        if record is None:
            dataset["chunk_ids"].append(None)
            dataset["cached"].append(None)
            dataset["download_ms"].append(None)
            dataset["decode_ms"].append(None)
            dataset["per_frame_fetch_ms"].append(None)
            dataset["log_missing"] = True
            continue
        dataset["chunk_ids"].append(record.get("chunk_id"))
        cached_flag = record.get("cached")
        dataset["cached"].append(cached_flag)
        download_ms = float(record.get("download_ms") or 0.0)
        decode_ms = float(record.get("decode_ms") or 0.0)
        dataset["download_ms"].append(download_ms)
        dataset["decode_ms"].append(decode_ms)
        dataset["per_frame_fetch_ms"].append(download_ms + decode_ms)

    valid_fetch = [value for value in dataset["per_frame_fetch_ms"] if value is not None]
    if valid_fetch:
        dataset["avg_fetch_ms"] = sum(valid_fetch) / len(valid_fetch)
    cached_flags = [flag for flag in dataset["cached"] if isinstance(flag, bool)]
    if cached_flags:
        hits = sum(1 for flag in cached_flags if flag)
        cache_stats["cache_hits"] = hits
        cache_stats["cache_misses"] = len(cached_flags) - hits
        dataset["hit_ratio"] = hits / len(cached_flags)
    else:
        cache_stats["cache_hits"] = 0
        cache_stats["cache_misses"] = 0

    return dataset, cache_stats


def _plan_chunks(
    *,
    start_frame: int,
    target_frame: int,
    batch_size: int,
) -> dict[str, object]:
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    total_frames = target_frame - start_frame
    if total_frames <= 0:
        raise ValueError("--target-frame must be greater than --start-frame")
    frames = list(range(start_frame + 1, target_frame + 1))
    chunks = []
    for idx in range(0, total_frames, batch_size):
        chunk_frames = frames[idx : idx + batch_size]
        chunks.append(
            {
                "chunk_index": len(chunks),
                "frame_count": len(chunk_frames),
                "frames": chunk_frames,
            }
        )
    return {
        "batch_size": batch_size,
        "plan_only": True,
        "total_frames": total_frames,
        "track_chunks": len(chunks),
        "chunks": chunks,
    }


def _flatten_chunk_frames(chunks: Iterable[dict[str, Any]]) -> list[int]:
    frames: list[int] = []
    for chunk in chunks:
        frames.extend(_coerce_frame_list(chunk.get("frames")))
    return frames


def _summarize_server_timing(
    entries: list[dict[str, Any]],
    *,
    run_id: str,
    total_frames: int,
) -> dict[str, Any]:
    """
    Aggregate server-side SAM2_TRACKER_SERVER_LOG timing by phase.

    Returns queue/apply timing in milliseconds, including per-frame averages
    when total_frames is positive.
    """

    safe_total_frames = max(int(total_frames), 0)
    queue_entries = [
        entry
        for entry in entries
        if entry.get("phase") == "queue_acquire" and str(entry.get("run_id")) == run_id
    ]
    apply_entries = [
        entry
        for entry in entries
        if entry.get("phase") == "apply_results" and str(entry.get("run_id")) == run_id
    ]

    total_queue_wait_ms = 0.0
    for entry in queue_entries:
        try:
            total_queue_wait_ms += float(entry.get("queue_wait_ms") or 0.0)
        except (TypeError, ValueError):
            continue

    total_apply_wall_ms = 0.0
    total_apply_db_ms = 0.0
    for entry in apply_entries:
        try:
            total_apply_wall_ms += float(entry.get("wall_ms") or 0.0)
        except (TypeError, ValueError):
            continue
        try:
            total_apply_db_ms += float(entry.get("db_wall_ms") or 0.0)
        except (TypeError, ValueError):
            continue

    per_frame_queue_ms = None
    per_frame_apply_wall_ms = None
    per_frame_apply_db_ms = None
    if safe_total_frames > 0:
        per_frame_queue_ms = total_queue_wait_ms / safe_total_frames
        per_frame_apply_wall_ms = total_apply_wall_ms / safe_total_frames
        per_frame_apply_db_ms = total_apply_db_ms / safe_total_frames

    return {
        "queue_acquire": {
            "events": len(queue_entries),
            "total_queue_wait_ms": total_queue_wait_ms,
            "per_frame_ms": per_frame_queue_ms,
        },
        "apply_results": {
            "events": len(apply_entries),
            "total_wall_ms": total_apply_wall_ms,
            "total_db_wall_ms": total_apply_db_ms,
            "per_frame_wall_ms": per_frame_apply_wall_ms,
            "per_frame_db_ms": per_frame_apply_db_ms,
        },
    }


def _approximate_breakdown_per_frame_ms(entry: dict[str, Any]) -> dict[str, Any]:
    """
    Approximate a per-frame breakdown (fetch / SAM2 core+rest / server) in milliseconds.
    """

    avg_track_s = entry.get("avg_track_per_frame_s")
    avg_track_ms: float | None
    try:
        avg_track_ms = float(avg_track_s) * 1000.0 if avg_track_s is not None else None
    except (TypeError, ValueError):
        avg_track_ms = None

    dataset_fetch = entry.get("dataset_fetch") or {}
    fetch_ms = dataset_fetch.get("avg_fetch_ms")
    try:
        fetch_ms = float(fetch_ms) if fetch_ms is not None else None
    except (TypeError, ValueError):
        fetch_ms = None

    server_timing = entry.get("server_timing") or {}
    queue_ms = None
    apply_ms = None
    queue_info = server_timing.get("queue_acquire") or {}
    apply_info = server_timing.get("apply_results") or {}
    try:
        if queue_info.get("per_frame_ms") is not None:
            queue_ms = float(queue_info["per_frame_ms"])
    except (TypeError, ValueError):
        queue_ms = None
    try:
        if apply_info.get("per_frame_wall_ms") is not None:
            apply_ms = float(apply_info["per_frame_wall_ms"])
    except (TypeError, ValueError):
        apply_ms = None

    server_ms_components = [value for value in (queue_ms, apply_ms) if isinstance(value, float)]
    server_ms = sum(server_ms_components) if server_ms_components else None

    sam2_core_ms = None
    if avg_track_ms is not None:
        subtract_components = []
        if isinstance(fetch_ms, float):
            subtract_components.append(fetch_ms)
        if isinstance(server_ms, float):
            subtract_components.append(server_ms)
        sam2_core_ms = max(avg_track_ms - sum(subtract_components), 0.0)

    total_ms = None
    components_for_total = []
    for value in (fetch_ms, sam2_core_ms, server_ms):
        if isinstance(value, float):
            components_for_total.append(value)
    if components_for_total:
        total_ms = sum(components_for_total)

    return {
        "avg_track_per_frame_ms": avg_track_ms,
        "fetch_ms": fetch_ms,
        "server_queue_ms": queue_ms,
        "server_apply_ms": apply_ms,
        "sam2_core_plus_rest_ms": sam2_core_ms,
        "approx_total_ms": total_ms,
    }


def main() -> None:
    args = _parse_args()
    if args.target_frame <= args.start_frame:
        raise ValueError("--target-frame must be greater than --start-frame")
    if args.repeat <= 0:
        raise ValueError("--repeat must be positive")

    if args.plan_only:
        results = []
        for batch_size in args.batch_sizes:
            for repeat_index in range(args.repeat):
                plan = _plan_chunks(
                    start_frame=args.start_frame,
                    target_frame=args.target_frame,
                    batch_size=batch_size,
                )
                plan["repeat_index"] = repeat_index
                results.append(plan)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(json.dumps(results, indent=2))
        print(
            f"[plan-only] Wrote {len(results)} chunk plans "
            f"({args.start_frame}->{args.target_frame}) to {args.output}"
        )
        return

    session = requests.Session()
    session.auth = (args.username, args.password)
    headers: dict[str, str] = {}
    if args.host_header:
        headers["Host"] = args.host_header

    compose_cmd = _format_compose_cmd(args.compose_cmd)

    results: list[dict[str, object]] = []
    for batch_size in args.batch_sizes:
        for repeat_index in range(args.repeat):
            meta = _start_tracker_run(
                session=session,
                server=args.server,
                headers=headers,
                job_id=args.job,
                function_id=args.function,
                track_id=args.track,
                start_frame=args.start_frame,
                target_frame=args.target_frame,
                batch_size=batch_size,
            )
            print(
                f"[tracker] Started batch_size={batch_size} "
                f"repeat_index={repeat_index} run_id={meta.run_id}"
            )
            completion = _wait_for_completion(
                session=session,
                server=args.server,
                headers=headers,
                run_id=meta.run_id,
                poll_interval=args.poll_interval,
            )
            print(f"[tracker] Run {meta.run_id} finished with status={completion.status}")
            rows = _fetch_annotation_requests(
                compose_cmd=compose_cmd,
                psql_service=args.psql_service,
                psql_db=args.psql_db,
                psql_user=args.psql_user,
                run_id=meta.run_id,
            )
            summary = _summarize_requests(rows)
            summary.update(
                {
                    "batch_size": batch_size,
                    "repeat_index": repeat_index,
                    "run_id": meta.run_id,
                    "initial_request_id": meta.initial_request_id,
                    "submitted_at": meta.submitted_at,
                    "submit_latency": meta.submit_latency,
                    "run_status": completion.status,
                    "run_progress_final": completion.payload.get("progress"),
                    "tracker_preload_chunks": args.tracker_preload_chunks,
                }
            )
            if args.include_fetch_metrics:
                log_since = meta.submitted_at - 5.0
                log_path = args.agent_log_dir / f"sam2_tracker_run_{meta.run_id}.log"
                log_text = _collect_agent_logs(
                    compose_cmd=compose_cmd,
                    agent_service=args.agent_service,
                    since_ts=log_since,
                    output_path=log_path,
                )
                entries = _parse_tracker_logs(log_text)
                chunk_frames = _flatten_chunk_frames(summary.get("chunks", []))
                dataset_fetch, cache_stats = _extract_dataset_fetch_metrics(
                    log_entries=entries,
                    run_id=meta.run_id,
                    chunk_frames=chunk_frames,
                    tracker_preload=args.tracker_preload_chunks,
                )
                summary["dataset_fetch"] = dataset_fetch
                summary["cache_stats"] = cache_stats
                summary["agent_log_path"] = str(log_path)
            if args.include_server_timing:
                server_log_since = meta.submitted_at - 5.0
                server_log_path = args.server_log_dir / f"sam2_tracker_server_run_{meta.run_id}.log"
                server_log_text = _collect_server_logs(
                    compose_cmd=compose_cmd,
                    server_service=args.server_log_service,
                    since_ts=server_log_since,
                    output_path=server_log_path,
                )
                server_entries = _parse_tracker_logs(
                    server_log_text,
                    marker="SAM2_TRACKER_SERVER_LOG",
                )
                total_frames_value = summary.get("total_frames") or 0
                server_timing = _summarize_server_timing(
                    server_entries,
                    run_id=meta.run_id,
                    total_frames=int(total_frames_value),
                )
                summary["server_timing"] = server_timing
                summary["server_log_path"] = str(server_log_path)

            summary["approximate_breakdown_per_frame_ms"] = _approximate_breakdown_per_frame_ms(summary)
            results.append(summary)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    for entry in results:
        entry["total_track_duration_s"] = round(float(entry["total_track_duration_s"]), 6)
        entry["wall_clock_s"] = round(float(entry["wall_clock_s"]), 6)
        if entry.get("avg_track_per_frame_s") is not None:
            entry["avg_track_per_frame_s"] = round(float(entry["avg_track_per_frame_s"]), 6)
        if entry.get("init_duration_s") is not None:
            entry["init_duration_s"] = round(float(entry["init_duration_s"]), 6)
        for chunk in entry.get("chunks", []):
            chunk["duration_s"] = round(float(chunk["duration_s"]), 6)
            if chunk.get("per_frame_s") is not None:
                chunk["per_frame_s"] = round(float(chunk["per_frame_s"]), 6)
        if entry.get("dataset_fetch"):
            df = entry["dataset_fetch"]
            if df.get("avg_fetch_ms") is not None:
                df["avg_fetch_ms"] = round(float(df["avg_fetch_ms"]), 6)
            if df.get("hit_ratio") is not None:
                df["hit_ratio"] = round(float(df["hit_ratio"]), 6)

    args.output.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps(results, indent=2))
    print(f"Wrote {len(results)} measurement entries to {args.output}")


if __name__ == "__main__":
    main()
