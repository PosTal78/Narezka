"""Small persistent benchmark for measuring matching quality on a real project."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Iterable

from narezchik.models import SegmentMatch, VideoFragment


def evaluation_ids(matches: Iterable[SegmentMatch], limit: int = 20) -> list[int]:
    """Choose a deterministic sample spread across the score distribution."""
    values = sorted(matches, key=lambda item: (-1.0 if item.confidence is None else item.confidence,
                                                item.segment_id))
    if len(values) <= limit:
        return [item.segment_id for item in values]
    positions = [round(index * (len(values) - 1) / (limit - 1)) for index in range(limit)]
    return [values[position].segment_id for position in positions]


def read_evaluation(path: Path) -> dict[int, dict[str, object]]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return {int(item["segment_id"]): item for item in payload.get("labels", [])}
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return {}


def save_evaluation(path: Path, labels: dict[int, dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".matching-evaluation-", suffix=".tmp", dir=path.parent, text=True)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump({"version": 1, "labels": [labels[key] for key in sorted(labels)]}, stream,
                      ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n");stream.flush();os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def make_label(segment_id: int, verdict: str, candidate_index: int | None = None,
               fragments: Iterable[VideoFragment] = ()) -> dict[str, object]:
    if verdict not in {"correct", "no_acceptable_candidate"}:
        raise ValueError("unknown evaluation verdict")
    return {"segment_id": segment_id, "verdict": verdict, "candidate_index": candidate_index,
            "fragments": [item.to_dict() for item in fragments]}


def evaluation_report(matches: Iterable[SegmentMatch], labels: dict[int, dict[str, object]]) -> dict[str, int]:
    by_id = {item.segment_id: item for item in matches}
    top1 = alternatives = no_acceptable = strong_wrong = 0
    for segment_id, label in labels.items():
        match = by_id.get(segment_id)
        if not match:
            continue
        verdict = label.get("verdict")
        if verdict == "no_acceptable_candidate":
            no_acceptable += 1
            strong_wrong += int(not match.needs_review)
        elif verdict == "correct":
            candidate = int(label.get("candidate_index") or 0)
            top1 += int(candidate == 0)
            alternatives += int(candidate > 0)
            strong_wrong += int(not match.needs_review and candidate > 0)
    return {"labelled": len(labels), "top1_correct": top1, "correct_in_alternatives": alternatives,
            "no_acceptable_candidate": no_acceptable, "strong_but_wrong": strong_wrong}
