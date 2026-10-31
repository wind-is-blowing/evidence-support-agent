"""Apply frozen retrieval calibration; no labels, training or model calls here."""

from __future__ import annotations


def gate_evidence(features: dict, recipe: dict) -> list[str]:
    """None disables one threshold; a finite cutoff is inclusive."""
    kind = recipe["kind"]
    key = {"dense_query": "dense_ids", "rrf_query": "rrf_ids"}.get(kind, "rank_ids")
    ids = features[key]
    dense = recipe.get("dense_min")
    logit = recipe.get("logit_min")

    def supported(i):
        return (dense is None or features["cosine"][i] >= dense) and (
            logit is None or features["logit"][i] >= logit
        )

    if kind == "joint_evidence":
        return [i for i in ids if supported(i)][:5]
    if kind not in {"dense_query", "rrf_query", "rank_query", "joint_query"}:
        raise ValueError(f"Unknown calibration kind: {kind}")
    window = ids[:5]
    return window if any(supported(i) for i in window) else []
