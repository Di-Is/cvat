"""Utility to cross-check dataset fetch metrics JSON with SAM2 agent logs."""

from __future__ import annotations

import argparse
import ast
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple


@dataclass
class VerificationResult:
    run_label: str
    total_frames: int
    matched_frames: int
    missing_frames: List[int]
    extra_frames: List[int]
    mismatches: List[str]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate that dataset fetch metrics JSON matches SAM2 tracker verbose logs."
        )
    )
    parser.add_argument("--json", dest="json_path", type=Path, required=True)
    parser.add_argument("--log", dest="log_path", type=Path, required=True)
    parser.add_argument(
        "--tolerance",
        type=float,
        default=1e-3,
        help="Tolerance when comparing floating point durations (ms).",
    )
    return parser.parse_args()


def load_measurement(json_path: Path) -> Dict[str, Any]:
    with json_path.open() as jf:
        return json.load(jf)


def parse_log_value(raw: str) -> Any:
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


def parse_log_file(log_path: Path) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    marker = "SAM2_TRACKER_LOG"
    with log_path.open() as lf:
        for line in lf:
            if marker not in line:
                continue
            segment = line.split(marker, 1)[1].strip()
            try:
                data = json.loads(segment)
            except json.JSONDecodeError:
                data = {}
                for token in segment.split():
                    if "=" not in token:
                        continue
                    key, value = token.split("=", 1)
                    data[key] = parse_log_value(value)
            if data:
                entries.append(data)
    return entries


def group_entries_by_run(entries: Iterable[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for entry in entries:
        run_id = entry.get("run")
        key = "__all__" if not run_id else str(run_id)
        grouped.setdefault(key, []).append(entry)
    return grouped


def build_frame_map(log_entries: Iterable[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
    frame_map: Dict[int, Dict[str, Any]] = {}
    for entry in log_entries:
        phase = entry.get("phase")
        if phase == "dataset_fetch":
            frames = entry.get("frames", [])
            if isinstance(frames, (int, float)):
                frame_iter: Iterable[int] = [int(frames)]
            elif isinstance(frames, str):
                try:
                    parsed = ast.literal_eval(frames)
                    if isinstance(parsed, list):
                        frame_iter = [int(f) for f in parsed]
                    else:
                        frame_iter = [int(parsed)]
                except Exception:
                    continue
            else:
                frame_iter = [int(f) for f in frames]
            download_ms = entry.get("download_ms")
            decode_ms = entry.get("decode_ms")
            cached = entry.get("cached")
            if isinstance(cached, str):
                cached = cached.lower() == "true"
            for frame in frame_iter:
                frame_map[int(frame)] = {
                    "chunk_id": entry.get("chunk_id"),
                    "cached": cached,
                    "download_ms": download_ms,
                    "decode_ms": decode_ms,
                }
        elif phase == "frame_fetch":
            frame = entry.get("frame_index")
            if frame is None:
                continue
            try:
                frame_int = int(frame)
            except (TypeError, ValueError):
                continue
            cached = entry.get("cached")
            if isinstance(cached, str):
                cached = cached.lower() == "true"
            frame_map[frame_int] = {
                "chunk_id": entry.get("chunk_id"),
                "cached": cached,
                "download_ms": entry.get("wall_ms"),
                "decode_ms": entry.get("decode_ms"),
            }
    return frame_map


def almost_equal(a: float, b: float, tolerance: float) -> bool:
    return abs(a - b) <= tolerance


def verify_run(
    run_label: str,
    run_payload: Dict[str, Any],
    log_entries: Iterable[Dict[str, Any]],
    tolerance: float,
) -> VerificationResult:
    dataset_fetch = run_payload.get("dataset_fetch") or {}
    frames = dataset_fetch.get("frames") or []
    chunk_ids = dataset_fetch.get("chunk_ids") or []
    cached_flags = dataset_fetch.get("cached") or []
    download_ms = dataset_fetch.get("download_ms") or []
    decode_ms = dataset_fetch.get("decode_ms") or []

    frame_map = build_frame_map(log_entries)
    missing: List[int] = []
    extra: List[int] = []
    mismatches: List[str] = []
    matched = 0

    for idx, frame in enumerate(frames):
        expected = {
            "chunk_id": chunk_ids[idx] if idx < len(chunk_ids) else None,
            "cached": cached_flags[idx] if idx < len(cached_flags) else None,
            "download_ms": download_ms[idx] if idx < len(download_ms) else None,
            "decode_ms": decode_ms[idx] if idx < len(decode_ms) else None,
        }
        actual = frame_map.pop(int(frame), None)
        if actual is None:
            missing.append(int(frame))
            continue
        matched += 1
        if expected["chunk_id"] is not None and actual.get("chunk_id") != expected["chunk_id"]:
            mismatches.append(
                f"frame {frame}: chunk_id expected {expected['chunk_id']} got {actual.get('chunk_id')}"
            )
        if expected["cached"] is not None and actual.get("cached") != expected["cached"]:
            mismatches.append(
                f"frame {frame}: cached expected {expected['cached']} got {actual.get('cached')}"
            )
        actual_download = actual.get("download_ms")
        if expected["download_ms"] is not None and actual_download is not None:
            if not almost_equal(float(expected["download_ms"]), float(actual_download), tolerance):
                mismatches.append(
                    f"frame {frame}: download_ms expected {expected['download_ms']} got {actual_download}"
                )
        actual_decode = actual.get("decode_ms")
        if expected["decode_ms"] is not None and actual_decode is not None:
            if not almost_equal(float(expected["decode_ms"]), float(actual_decode), tolerance):
                mismatches.append(
                    f"frame {frame}: decode_ms expected {expected['decode_ms']} got {actual_decode}"
                )

    return VerificationResult(
        run_label=run_label,
        total_frames=len(frames),
        matched_frames=matched,
        missing_frames=missing,
        extra_frames=[],
        mismatches=mismatches,
    )


def main() -> None:
    args = parse_args()
    measurement = load_measurement(args.json_path)
    log_entries = parse_log_file(args.log_path)
    logs_by_run = group_entries_by_run(log_entries)

    if isinstance(measurement, list):
        runs = measurement
    else:
        runs = measurement.get("runs") or []
    if not runs:
        raise SystemExit("No runs found in measurement JSON.")

    errors: List[str] = []
    for run in runs:
        run_id = run.get("run_id")
        repeat = run.get("repeat")
        label = str(run_id or repeat)
        if run_id:
            entries = logs_by_run.get(str(run_id), [])
            if not entries:
                entries = logs_by_run.get("__all__", [])
        else:
            if len(runs) > 1:
                errors.append(
                    f"run repeat={repeat} missing run_id; cannot disambiguate logs when multiple runs exist"
                )
                continue
            entries = logs_by_run.get("__all__", log_entries)
        result = verify_run(label, run, entries, args.tolerance)
        print(
            f"run {label}: matched {result.matched_frames}/{result.total_frames} frames"
        )
        if result.missing_frames:
            errors.append(f"run {label}: missing frames {result.missing_frames}")
        if result.extra_frames:
            errors.append(f"run {label}: extra frames in logs {result.extra_frames}")
        if result.mismatches:
            errors.extend(result.mismatches)

    if errors:
        for err in errors:
            print(f"ERROR: {err}")
        raise SystemExit(1)

    print("All runs verified successfully.")


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    main()
