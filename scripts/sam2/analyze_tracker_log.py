#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import re
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class Stat:
    """Basic descriptive statistics for a numeric series."""

    count: int
    mean: float
    p50: float
    p95: float
    max: float


def _parse_log(path: Path, *, marker: str) -> list[dict]:
    """Return all JSON records matching the given SAM2 tracker marker."""

    pattern = re.compile(rf"{re.escape(marker)} (\{{.*}})")
    records: list[dict] = []
    for line in path.read_text().splitlines():
        match = pattern.search(line)
        if not match:
            continue
        try:
            records.append(json.loads(match.group(1)))
        except json.JSONDecodeError:
            continue
    return records


def _percentile(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        return float("nan")
    idx = max(0, min(len(sorted_values) - 1, int(p * len(sorted_values)) - 1))
    return sorted_values[idx]


def _reduce(values: Iterable[float]) -> Stat:
    series = sorted(values)
    if not series:
        return Stat(0, float("nan"), float("nan"), float("nan"), float("nan"))
    return Stat(
        count=len(series),
        mean=statistics.fmean(series),
        p50=statistics.median(series),
        p95=_percentile(series, 0.95),
        max=series[-1],
    )


def _summaries(records: Iterable[dict], *, field: str = "wall_ms") -> dict[str, Stat]:
    by_phase: dict[str, list[float]] = {}
    for rec in records:
        phase = rec.get("phase")
        val = rec.get(field)
        if phase is None or val is None:
            continue
        try:
            numeric = float(val)
        except (TypeError, ValueError):
            continue
        by_phase.setdefault(str(phase), []).append(numeric)
    return {phase: _reduce(vals) for phase, vals in sorted(by_phase.items())}


def _normalize_run_id(record: dict[str, Any]) -> str | None:
    run_id = record.get("run_id") or record.get("function_run_id")
    if isinstance(run_id, str) and run_id:
        return run_id.lower()
    return None


def _build_timeline(
    agent_records: list[dict[str, Any]],
    server_records: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Build a lightweight combined timeline of agent/server events per run id.

    The resulting structure is intended for downstream tools (e.g. notebooks)
    to visualise overlaps; it deliberately keeps only commonly useful fields.
    """

    events: list[dict[str, Any]] = []

    def append_events(source: str, records: list[dict[str, Any]]) -> None:
        for index, rec in enumerate(records):
            run_id = _normalize_run_id(rec)
            if not run_id:
                continue
            phase = rec.get("phase")
            event: dict[str, Any] = {
                "source": source,
                "run_id": run_id,
                "phase": phase,
                "index": index,
            }
            # Frame information, if available.
            if "frame_idx" in rec:
                event["frame_idx"] = rec["frame_idx"]
            if "frame" in rec:
                event["frame"] = rec["frame"]
            if "chunk_id" in rec:
                event["chunk_id"] = rec["chunk_id"]

            # Common numeric timing fields.
            for key in ("wall_ms", "gpu_ms", "queue_wait_ms", "db_wall_ms", "t_ms", "frame_t_ms"):
                value = rec.get(key)
                try:
                    if value is not None:
                        event[key] = float(value)
                except (TypeError, ValueError):
                    continue

            events.append(event)

    append_events("agent", agent_records)
    append_events("server", server_records)

    return {"events": events}


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Parse SAM2_TRACKER_LOG / SAM2_TRACKER_SERVER_LOG lines and print phase-wise stats."
        ),
    )
    parser.add_argument("log", type=Path, help="Path to a tracker-agent log file.")
    parser.add_argument(
        "--server-log",
        type=Path,
        help="Optional path to a server log file containing SAM2_TRACKER_SERVER_LOG records.",
    )
    parser.add_argument(
        "--warmup-frames",
        type=int,
        default=16,
        help="Frame index threshold; frames below this are treated as warmup.",
    )
    parser.add_argument(
        "--timeline-json",
        type=Path,
        help="Optional path to write a combined agent/server timeline JSON.",
    )
    args = parser.parse_args()

    agent_records = _parse_log(args.log, marker="SAM2_TRACKER_LOG")
    warm = [
        r for r in agent_records if (idx := r.get("frame_idx")) is not None and idx < args.warmup_frames
    ]
    steady = [
        r for r in agent_records if (idx := r.get("frame_idx")) is not None and idx >= args.warmup_frames
    ]

    def emit(label: str, items: dict[str, Stat]) -> None:
        print(f"\n[{label}]")
        print(f"{'phase':24s} {'count':>5s} {'mean':>8s} {'p50':>8s} {'p95':>8s} {'max':>8s}")
        for phase, stat in items.items():
            mean = stat.mean if not math.isnan(stat.mean) else float("nan")
            p50 = stat.p50 if not math.isnan(stat.p50) else float("nan")
            p95 = stat.p95 if not math.isnan(stat.p95) else float("nan")
            vmax = stat.max if not math.isnan(stat.max) else float("nan")
            print(f"{phase:24s} {stat.count:5d} {mean:8.3f} {p50:8.3f} {p95:8.3f} {vmax:8.3f}")

    print(f"Parsed {len(agent_records)} SAM2_TRACKER_LOG records from {args.log}")
    emit("overall (wall_ms)", _summaries(agent_records, field="wall_ms"))
    emit("warmup (wall_ms)", _summaries(warm, field="wall_ms"))
    emit("steady (wall_ms)", _summaries(steady, field="wall_ms"))

    # GPU-side timing if available.
    gpu_records = [r for r in agent_records if "gpu_ms" in r]
    if gpu_records:
        emit("overall (gpu_ms)", _summaries(gpu_records, field="gpu_ms"))
        emit("warmup (gpu_ms)", _summaries([r for r in warm if "gpu_ms" in r], field="gpu_ms"))
        emit("steady (gpu_ms)", _summaries([r for r in steady if "gpu_ms" in r], field="gpu_ms"))

    # Dataset fetch is often of special interest.
    dataset_fetch = [r for r in agent_records if r.get("phase") == "dataset_fetch"]
    chunk0 = [r for r in dataset_fetch if r.get("chunk_id") == 0]
    other = [r for r in dataset_fetch if r.get("chunk_id") not in (None, 0)]
    if dataset_fetch:
        print("\n[dataset_fetch wall_ms by chunk]")
        print(f"chunk0: {_reduce(r['wall_ms'] for r in chunk0)}")
        print(f"chunk>0: {_reduce(r['wall_ms'] for r in other)}")

    server_records: list[dict[str, Any]] = []
    if args.server_log:
        server_records = _parse_log(args.server_log, marker="SAM2_TRACKER_SERVER_LOG")
        print(f"\nParsed {len(server_records)} SAM2_TRACKER_SERVER_LOG records from {args.server_log}")
        emit("server (wall_ms)", _summaries(server_records, field="wall_ms"))
        # Queue wait is usually logged as queue_wait_ms under phase=queue_acquire.
        queue_summaries = _summaries(server_records, field="queue_wait_ms")
        if queue_summaries:
            emit("server (queue_wait_ms)", queue_summaries)

    if args.timeline_json:
        timeline = _build_timeline(agent_records, server_records)
        args.timeline_json.parent.mkdir(parents=True, exist_ok=True)
        args.timeline_json.write_text(json.dumps(timeline, indent=2), encoding="utf-8")
        print(f"\nWrote combined agent/server timeline to {args.timeline_json}")


if __name__ == "__main__":
    main()
