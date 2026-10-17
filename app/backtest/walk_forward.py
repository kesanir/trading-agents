"""
walk_forward.py — Walk-forward stability analysis of signal edge.

Splits resolved signal_outcomes into a training window (first 70%) and
an out-of-sample test window (last 30%). Checks whether winning
(bias, confidence, day_type) combinations identified in-sample persist
out-of-sample.

Output: stability_score per setup — 1.0 means perfectly stable,
0.0 means the edge did not persist out-of-sample.

Usage:
  python -c "from app.backtest.walk_forward import run_walk_forward; import pprint; pprint.pprint(run_walk_forward())"
"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from app.services.trade_logger import read_signal_outcomes
from app.services.signal_quality_service import summarize_outcomes

_TRAIN_RATIO = 0.70
_MIN_SAMPLE  = 5  # minimum signals per group to include in analysis


def _group_by(outcomes: List[Dict[str, Any]], keys: Tuple[str, ...]) -> Dict[str, List[Dict[str, Any]]]:
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for o in outcomes:
        key = "|".join(str(o.get(k, "?")) for k in keys)
        groups[key].append(o)
    return dict(groups)


def _stability_score(train_acc: Optional[float], test_acc: Optional[float]) -> Optional[float]:
    """
    Score how well in-sample accuracy predicted out-of-sample accuracy.
    Score of 1.0 = perfect stability, 0.0 = complete collapse.
    Returns None if either value is missing.
    """
    if train_acc is None or test_acc is None:
        return None
    if train_acc == 0:
        return None
    # Ratio of test to train — capped at 1.0 (can't do better than baseline)
    return round(min(1.0, test_acc / train_acc), 3)


def run_walk_forward(
    outcomes: Optional[List[Dict[str, Any]]] = None,
    train_ratio: float = _TRAIN_RATIO,
    group_keys: Tuple[str, ...] = ("bias", "confidence", "day_type"),
) -> Dict[str, Any]:
    """
    Walk-forward analysis of signal edge stability.

    Args:
        outcomes: Pre-loaded outcomes (reads from JSONL if None)
        train_ratio: Fraction of data used for in-sample period
        group_keys: Dimensions to group by for edge identification

    Returns dict with:
        train_period    — date range of training window
        test_period     — date range of test window
        stable_combos   — groups where edge persisted (stability >= 0.8)
        unstable_combos — groups where edge did not persist
        all_results     — full detail per group
    """
    if outcomes is None:
        outcomes = read_signal_outcomes()

    if len(outcomes) < (_MIN_SAMPLE * 2):
        return {
            "error": f"Need at least {_MIN_SAMPLE * 2} resolved signals for walk-forward. "
                     f"Currently have {len(outcomes)}."
        }

    # Sort by timestamp
    sorted_outcomes = sorted(outcomes, key=lambda o: o.get("signal_ts", o.get("resolved_at", "")))

    split = int(len(sorted_outcomes) * train_ratio)
    train = sorted_outcomes[:split]
    test  = sorted_outcomes[split:]

    train_start = train[0].get("signal_ts", "")[:10] if train else "?"
    train_end   = train[-1].get("signal_ts", "")[:10] if train else "?"
    test_start  = test[0].get("signal_ts", "")[:10] if test else "?"
    test_end    = test[-1].get("signal_ts", "")[:10] if test else "?"

    train_groups = _group_by(train, group_keys)
    test_groups  = _group_by(test, group_keys)

    all_results: List[Dict[str, Any]] = []

    for key, train_rows in train_groups.items():
        if len(train_rows) < _MIN_SAMPLE:
            continue

        train_stats = summarize_outcomes(train_rows)
        train_acc   = train_stats.get("accuracy_30m")

        test_rows   = test_groups.get(key, [])
        test_stats  = summarize_outcomes(test_rows) if len(test_rows) >= 2 else {}
        test_acc    = test_stats.get("accuracy_30m")

        stability = _stability_score(train_acc, test_acc)

        # Parse combo label back into fields
        parts  = key.split("|")
        labels = dict(zip(group_keys, parts))

        all_results.append({
            **labels,
            "train_signals":   len(train_rows),
            "test_signals":    len(test_rows),
            "train_acc_30m":   train_acc,
            "test_acc_30m":    test_acc,
            "stability_score": stability,
            "train_avg_mfe":   train_stats.get("avg_mfe"),
            "test_avg_mfe":    test_stats.get("avg_mfe"),
        })

    all_results.sort(key=lambda x: x.get("stability_score") or 0, reverse=True)

    stable   = [r for r in all_results if (r.get("stability_score") or 0) >= 0.8]
    unstable = [r for r in all_results if (r.get("stability_score") or 0) < 0.5]

    return {
        "train_period":  f"{train_start} — {train_end}  ({len(train)} signals)",
        "test_period":   f"{test_start} — {test_end}  ({len(test)} signals)",
        "group_keys":    list(group_keys),
        "stable_combos":   stable,
        "unstable_combos": unstable,
        "all_results":     all_results,
    }
