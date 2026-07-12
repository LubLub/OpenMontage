"""Preflight budget governance for a Higgsfield shot list.

Given an episode's shot list, price every shot in credits (via the FREE
`generate cost` probe), and — if the episode blows its credit cap — apply a
deterministic DEGRADE LADDER, re-pricing after each step:

  1. Downgrade video model seedance_2_0 -> kling3_0 (cheaper motion).
  2. Lower resolution / mode (seedance resolution down; kling mode down).
  3. Drop the lowest-priority shots down to a MIN_SHOTS floor.

If still over cap at the floor, decide "abort_ask_human". A month-to-date credit
ledger (JSON file) enforces a separate monthly ceiling across episodes.

Pure and testable: the only IO is the ledger file read/write and the pricing
callback (defaulting to lib.higgsfield_cli.estimate_credits, injectable for
tests). Input shots are never mutated — every step returns fresh dicts.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from lib import higgsfield_cli

MIN_SHOTS = 1

# Ordered resolution/mode ladders (highest -> lowest quality).
_SEEDANCE_RES_LADDER = ["4k", "1080p", "720p", "480p"]
_KLING_MODE_LADDER = ["4k", "pro", "std"]
_SEEDANCE_MODE_LADDER = ["std", "fast"]  # fast is cheaper but caps at 720p

PricerFn = Callable[[str, dict[str, Any]], float]


# --------------------------------------------------------------------------- #
# Shot helpers (immutable)
# --------------------------------------------------------------------------- #

def _shot_model(shot: dict[str, Any]) -> str:
    return shot.get("model", "")


def _shot_params(shot: dict[str, Any]) -> dict[str, Any]:
    return dict(shot.get("params") or {})


def _with(shot: dict[str, Any], *, model: str | None = None, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a new shot dict with model/params replaced (never mutates input)."""
    new = dict(shot)
    if model is not None:
        new["model"] = model
    if params is not None:
        new["params"] = params
    return new


def _price_shot(shot: dict[str, Any], pricer: PricerFn) -> float | None:
    """Price one shot, returning ``None`` when no trustworthy quote exists."""
    model = _shot_model(shot)
    if not model:
        return None
    try:
        credits = float(pricer(model, _shot_params(shot)))
    except Exception:
        return None
    return credits if math.isfinite(credits) and credits >= 0 else None


def _price_all(
    shots: list[dict[str, Any]],
    pricer: PricerFn,
) -> tuple[list[dict[str, Any]], float | None, list[str]]:
    """Return priced shots, total, and IDs lacking a trustworthy live quote."""
    priced: list[dict[str, Any]] = []
    total = 0.0
    unpriced: list[str] = []
    for index, shot in enumerate(shots):
        credits = _price_shot(shot, pricer)
        if credits is None:
            shot_id = str(shot.get("id") or f"shot-{index + 1}")
            unpriced.append(shot_id)
            priced.append({**shot, "credits": None, "quote_status": "unavailable"})
            continue
        priced.append({**shot, "credits": credits, "quote_status": "live"})
        total += credits
    return priced, None if unpriced else round(total, 4), unpriced


def _quote_failure_result(
    *,
    priced: list[dict[str, Any]],
    unpriced: list[str],
    episode_credit_cap: float,
    month_to_date: float,
    monthly_ceiling: float,
) -> dict[str, Any]:
    return {
        "decision": "abort_ask_human",
        "reason": "one or more shots lack a trustworthy live credit quote",
        "quote_status": "unavailable",
        "ledger_status": "valid",
        "unpriced_shot_ids": unpriced,
        "plan": priced,
        "total_credits": None,
        "episode_credit_cap": episode_credit_cap,
        "degrade_steps": [],
        "month_to_date_before": month_to_date,
        "month_projected": None,
        "monthly_ceiling": monthly_ceiling,
        "monthly_ok": False,
    }


# --------------------------------------------------------------------------- #
# Degrade ladder steps (each returns a NEW shot list)
# --------------------------------------------------------------------------- #

def _step_downgrade_video_model(shots: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    """seedance_2_0 -> kling3_0. Drops the seedance-only resolution/audio params.

    Returns (new_shots, changed).
    """
    changed = False
    new_shots: list[dict[str, Any]] = []
    for shot in shots:
        if _shot_model(shot) == "seedance_2_0":
            params = _shot_params(shot)
            # kling3_0 has no resolution / generate_audio params.
            params.pop("resolution", None)
            params.pop("generate_audio", None)
            # seedance 'fast' has no kling equivalent — normalize to std.
            if params.get("mode") == "fast":
                params["mode"] = "std"
            new_shots.append(_with(shot, model="kling3_0", params=params))
            changed = True
        else:
            new_shots.append(dict(shot))
    return new_shots, changed


def _step_lower_quality(shots: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    """Lower resolution (seedance) or mode (kling) by one rung where possible."""
    changed = False
    new_shots: list[dict[str, Any]] = []
    for shot in shots:
        model = _shot_model(shot)
        params = _shot_params(shot)
        if model == "seedance_2_0":
            cur = params.get("resolution", "720p")
            lowered = _next_down(_SEEDANCE_RES_LADDER, cur)
            if lowered is not None:
                params["resolution"] = lowered
                # 'fast' mode only supports <=720p; safe once we're at/below it.
                changed = True
                new_shots.append(_with(shot, params=params))
                continue
        elif model == "kling3_0":
            cur = params.get("mode", "std")
            lowered = _next_down(_KLING_MODE_LADDER, cur)
            if lowered is not None:
                params["mode"] = lowered
                changed = True
                new_shots.append(_with(shot, params=params))
                continue
        new_shots.append(dict(shot))
    return new_shots, changed


def _next_down(ladder: list[str], current: str) -> str | None:
    """Return the next-lower rung in `ladder`, or None if already at the bottom
    or unknown. Ladder is ordered highest->lowest."""
    try:
        idx = ladder.index(current)
    except ValueError:
        # Unknown current value: assume it's already reasonable, no change.
        return None
    if idx >= len(ladder) - 1:
        return None
    return ladder[idx + 1]


def _step_drop_lowest_priority(
    shots: list[dict[str, Any]], min_shots: int
) -> tuple[list[dict[str, Any]], bool]:
    """Drop the single lowest-priority shot, respecting the MIN_SHOTS floor.

    Priority: a shot's `priority` int (higher = keep). Ties broken by original
    order (later shots drop first). Returns (new_shots, changed).
    """
    if len(shots) <= min_shots:
        return [dict(s) for s in shots], False
    # Find index of the lowest-priority shot (drop later ones first on ties).
    worst_idx = 0
    worst_key = None
    for idx, shot in enumerate(shots):
        # (priority ascending, index descending) -> lowest priority, latest shot.
        key = (shot.get("priority", 0), -idx)
        if worst_key is None or key < worst_key:
            worst_key = key
            worst_idx = idx
    new_shots = [dict(s) for i, s in enumerate(shots) if i != worst_idx]
    return new_shots, True


# --------------------------------------------------------------------------- #
# Monthly ledger (JSON file)
# --------------------------------------------------------------------------- #

def _current_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _read_ledger(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise HiggsfieldLedgerError("monthly credit ledger is unavailable") from exc
    if not isinstance(data, dict):
        raise HiggsfieldLedgerError("monthly credit ledger is invalid")
    return data


class HiggsfieldLedgerError(RuntimeError):
    """Raised when prior credit spend cannot be trusted."""


def month_to_date_credits(ledger_path: str | Path | None) -> float:
    """Return credits already spent in the current month per the ledger."""
    if ledger_path is None:
        return 0.0
    ledger = _read_ledger(Path(ledger_path))
    try:
        credits = float(ledger.get(_current_month(), 0.0))
    except (TypeError, ValueError) as exc:
        raise HiggsfieldLedgerError("monthly credit ledger is invalid") from exc
    if not math.isfinite(credits) or credits < 0:
        raise HiggsfieldLedgerError("monthly credit ledger is invalid")
    return credits


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #

def preflight_shotlist(
    shots: list[dict[str, Any]],
    episode_credit_cap: float,
    monthly_ledger_path: str | Path | None,
    monthly_ceiling: float,
    *,
    pricer: PricerFn | None = None,
    min_shots: int = MIN_SHOTS,
    commit: bool = False,
) -> dict[str, Any]:
    """Price a shot list and, if over cap, degrade it deterministically.

    Args:
        shots: list of {"model", "params", optional "priority", "id"} dicts.
        episode_credit_cap: max credits allowed for this episode.
        monthly_ledger_path: JSON file tracking month-to-date credits (or None).
        monthly_ceiling: max credits allowed for the whole month.
        pricer: credit pricer (model, params)->credits. Defaults to the FREE
            higgsfield_cli.estimate_credits. Injected in tests.
        min_shots: floor for shot-dropping.
        commit: retained only to reject legacy callers. Preflight estimates must
            never be recorded as actual spend; execution reconciles real credits.

    Returns a dict:
        {
          "decision": "ok" | "abort_ask_human",
          "reason": str,
          "plan": [priced shots],
          "total_credits": float,
          "episode_credit_cap": float,
          "degrade_steps": [str, ...],
          "month_to_date_before": float,
          "month_projected": float,
          "monthly_ceiling": float,
          "monthly_ok": bool,
        }
    """
    if commit:
        raise ValueError(
            "Preflight cannot commit estimated credits; reconcile actual spend after execution"
        )

    if (
        not math.isfinite(episode_credit_cap)
        or episode_credit_cap < 0
        or not math.isfinite(monthly_ceiling)
        or monthly_ceiling < 0
    ):
        return {
            "decision": "abort_ask_human",
            "reason": "credit ceilings must be finite non-negative values",
            "quote_status": "invalid_limits",
            "ledger_status": "not_run",
            "unpriced_shot_ids": [],
            "plan": [dict(shot) for shot in shots],
            "total_credits": None,
            "episode_credit_cap": episode_credit_cap,
            "degrade_steps": [],
            "month_to_date_before": None,
            "month_projected": None,
            "monthly_ceiling": monthly_ceiling,
            "monthly_ok": False,
        }

    try:
        mtd_before = month_to_date_credits(monthly_ledger_path)
    except HiggsfieldLedgerError:
        return {
            "decision": "abort_ask_human",
            "reason": "monthly credit ledger is unavailable or invalid",
            "quote_status": "not_run",
            "ledger_status": "unavailable",
            "unpriced_shot_ids": [],
            "plan": [dict(shot) for shot in shots],
            "total_credits": None,
            "episode_credit_cap": episode_credit_cap,
            "degrade_steps": [],
            "month_to_date_before": None,
            "month_projected": None,
            "monthly_ceiling": monthly_ceiling,
            "monthly_ok": False,
        }

    price: PricerFn = pricer or higgsfield_cli.estimate_credits
    steps_applied: list[str] = []

    current = [dict(s) for s in shots]  # defensive copy; inputs untouched
    priced, total, unpriced = _price_all(current, price)
    if unpriced:
        return _quote_failure_result(
            priced=priced,
            unpriced=unpriced,
            episode_credit_cap=episode_credit_cap,
            month_to_date=mtd_before,
            monthly_ceiling=monthly_ceiling,
        )
    assert total is not None

    # Degrade ladder — re-price after each step, stop as soon as we're under cap.
    if total > episode_credit_cap:
        # Step 1: seedance -> kling for all video shots.
        current, changed = _step_downgrade_video_model(current)
        if changed:
            steps_applied.append("downgrade_video_model:seedance_2_0->kling3_0")
            priced, total, unpriced = _price_all(current, price)
            if unpriced:
                return _quote_failure_result(
                    priced=priced,
                    unpriced=unpriced,
                    episode_credit_cap=episode_credit_cap,
                    month_to_date=mtd_before,
                    monthly_ceiling=monthly_ceiling,
                )
            assert total is not None

    # Step 2: lower resolution/mode, repeatedly, until it stops helping or passes.
    while total > episode_credit_cap:
        current, changed = _step_lower_quality(current)
        if not changed:
            break
        steps_applied.append("lower_quality")
        priced, total, unpriced = _price_all(current, price)
        if unpriced:
            return _quote_failure_result(
                priced=priced,
                unpriced=unpriced,
                episode_credit_cap=episode_credit_cap,
                month_to_date=mtd_before,
                monthly_ceiling=monthly_ceiling,
            )
        assert total is not None

    # Step 3: drop lowest-priority shots down to the floor.
    while total > episode_credit_cap and len(current) > min_shots:
        current, changed = _step_drop_lowest_priority(current, min_shots)
        if not changed:
            break
        steps_applied.append("drop_lowest_priority_shot")
        priced, total, unpriced = _price_all(current, price)
        if unpriced:
            return _quote_failure_result(
                priced=priced,
                unpriced=unpriced,
                episode_credit_cap=episode_credit_cap,
                month_to_date=mtd_before,
                monthly_ceiling=monthly_ceiling,
            )
        assert total is not None

    month_projected = round(mtd_before + total, 4)
    monthly_ok = month_projected <= monthly_ceiling

    over_episode = total > episode_credit_cap
    if over_episode:
        decision = "abort_ask_human"
        reason = (
            f"still {round(total - episode_credit_cap, 4)} credits over the episode "
            f"cap ({total} > {episode_credit_cap}) after exhausting the degrade ladder"
        )
    elif not monthly_ok:
        decision = "abort_ask_human"
        reason = (
            f"episode fits its cap but would breach the monthly ceiling "
            f"({month_projected} > {monthly_ceiling}; month-to-date {mtd_before})"
        )
    else:
        decision = "ok"
        reason = "priced plan is within the episode cap and monthly ceiling"

    result = {
        "decision": decision,
        "reason": reason,
        "quote_status": "live",
        "ledger_status": "valid",
        "unpriced_shot_ids": [],
        "plan": priced,
        "total_credits": total,
        "episode_credit_cap": episode_credit_cap,
        "degrade_steps": steps_applied,
        "month_to_date_before": mtd_before,
        "month_projected": month_projected,
        "monthly_ceiling": monthly_ceiling,
        "monthly_ok": monthly_ok,
    }

    return result
