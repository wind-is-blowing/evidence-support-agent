"""Apply serialized linear calibration; no gold labels or fitting in runtime code."""

from __future__ import annotations

import math

SCORE_FEATURES = ["cosine_max", "rerank_top", "rerank_margin"]
JUDGE_FEATURES = [
    "full",
    "cited_full",
    "supported_fraction",
    "requirement_count",
    "cited_fraction",
    "all_supported",
    "no_supported",
]


def signals(row, with_judge):
    f = row["features"]
    ids = f["rank_ids"][:5]
    logits = [f["logit"][i] for i in ids]
    values = [
        max((f["cosine"][i] for i in ids), default=-1),
        logits[0] if logits else -20,
        logits[0] - logits[1] if len(logits) > 1 else 0,
    ]
    if with_judge:
        v = row["verdict"]
        raw = row["judge"]["judgment"]
        requirements = raw.get("requirements", []) if isinstance(raw, dict) else []
        requirements = (
            [r for r in requirements if isinstance(r, dict)]
            if isinstance(requirements, list)
            else []
        )
        supported = sum(r.get("supported") is True for r in requirements)
        values += [
            float(v["full"]),
            float(v["cited_full"]),
            supported / max(1, len(requirements)),
            len(requirements) / 6,
            len(v["cited_ids"]) / max(1, len(ids)),
            float(bool(requirements) and supported == len(requirements)),
            float(supported == 0),
        ]
    return values


def eligible(row, with_judge):
    if not row["features"]["rank_ids"]:
        return False
    if not with_judge:
        return True
    # At least one valid citation is mandatory; do not override malformed output/quotes.
    return bool(row["verdict"]["cited_ids"]) and not any(
        e != "unsupported_requirement" for e in row["verdict"]["errors"]
    )


def linear_score(row, recipe):
    values = signals(row, recipe["with_judge"])
    return recipe["intercept"] + sum(
        weight * (value - center) / scale
        for value, weight, center, scale in zip(
            values, recipe["weights"], recipe["mean"], recipe["scale"], strict=True
        )
    )


def calibrated_evidence(row, recipe):
    if not eligible(row, recipe["with_judge"]):
        return []
    score = linear_score(row, recipe)
    if not math.isfinite(score) or score < recipe["threshold"]:
        return []
    return row["features"]["rank_ids"][:5]
