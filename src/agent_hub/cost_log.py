"""LLM usage and cost logging for Agent Hub."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from .config import LLM_COST_CATALOG_FILE, USAGE_LOG_FILE

logger = logging.getLogger(__name__)
MAX_RECENT_RUNS = 100


@dataclass(frozen=True)
class UsageSnapshot:
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cached_input_tokens: int = 0


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_cost_catalog(path: Path | None = None) -> dict[str, Any]:
    resolved = path or LLM_COST_CATALOG_FILE
    if not resolved.exists():
        return {"updated_at": None, "source": None, "models": {}}
    data = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Cost catalog must be a JSON object: {resolved}")
    data.setdefault("models", {})
    return data


def load_usage_log(path: Path | None = None) -> dict[str, Any]:
    resolved = path or USAGE_LOG_FILE
    if not resolved.exists():
        return _empty_usage_log()
    data = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Usage log must be a JSON object: {resolved}")
    data.setdefault("totals", _empty_usage_totals())
    data.setdefault("by_model", {})
    data.setdefault("recent_runs", [])
    return data


def record_llm_run(
    *,
    operation: str,
    requested_model: str | None,
    effective_model: str | None,
    status: str,
    duration_seconds: float,
    usage_by_model: Mapping[str, UsageSnapshot] | None = None,
    error: str | None = None,
    thread_id: str | None = None,
    request_kind: str | None = None,
    result_preview: str | None = None,
    usage_log_path: Path | None = None,
    cost_catalog_path: Path | None = None,
) -> dict[str, Any]:
    resolved_usage_log = usage_log_path or USAGE_LOG_FILE
    resolved_usage_log.parent.mkdir(parents=True, exist_ok=True)

    catalog = load_cost_catalog(cost_catalog_path)
    normalized_usage = _normalise_usage_map(usage_by_model or {})
    cost_breakdown = _estimate_cost_breakdown(normalized_usage, catalog)
    total_tokens = sum(item.total_tokens for item in normalized_usage.values())
    input_tokens = sum(item.input_tokens for item in normalized_usage.values())
    output_tokens = sum(item.output_tokens for item in normalized_usage.values())

    run_record = {
        "timestamp": utc_now_iso(),
        "operation": operation,
        "request_kind": request_kind,
        "status": status,
        "thread_id": thread_id,
        "requested_model": requested_model,
        "effective_model": effective_model,
        "duration_ms": _round_ms(duration_seconds),
        "usage": [item for item in cost_breakdown["models"]],
        "totals": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
        },
        "cost": cost_breakdown["cost"],
    }
    if error:
        run_record["error"] = error
    if result_preview:
        run_record["result_preview"] = result_preview

    usage_log = load_usage_log(resolved_usage_log)
    _update_usage_log(usage_log, run_record)
    resolved_usage_log.write_text(
        json.dumps(usage_log, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    logger.info(_format_run_summary(run_record))
    return run_record


def extract_usage_metadata(usage_source: Any) -> dict[str, UsageSnapshot]:
    if usage_source is None:
        return {}

    if hasattr(usage_source, "usage_metadata"):
        usage_source = getattr(usage_source, "usage_metadata")

    if not isinstance(usage_source, Mapping):
        return {}

    normalized: dict[str, UsageSnapshot] = {}
    for model_name, payload in usage_source.items():
        snapshot = _snapshot_from_payload(payload)
        if snapshot is not None:
            normalized[str(model_name)] = snapshot
    return normalized


def canonical_model_name(model_name: str | None) -> str | None:
    if not model_name:
        return None
    if ":" in model_name:
        return model_name.split(":", 1)[1].strip()
    return model_name.strip()


def _normalise_usage_map(
    usage_map: Mapping[str, UsageSnapshot],
) -> dict[str, UsageSnapshot]:
    normalized: dict[str, UsageSnapshot] = {}
    for model_name, snapshot in usage_map.items():
        if isinstance(snapshot, UsageSnapshot):
            normalized[str(model_name)] = snapshot
    return normalized


def _snapshot_from_payload(payload: Any) -> UsageSnapshot | None:
    if payload is None:
        return None
    if isinstance(payload, UsageSnapshot):
        return payload
    if not isinstance(payload, Mapping):
        return None

    input_tokens = int(payload.get("input_tokens", 0) or 0)
    output_tokens = int(payload.get("output_tokens", 0) or 0)
    total_tokens = int(payload.get("total_tokens", input_tokens + output_tokens) or 0)

    cached_input_tokens = 0
    input_details = payload.get("input_token_details")
    if isinstance(input_details, Mapping):
        cached_input_tokens = int(input_details.get("cache_read", 0) or 0)

    return UsageSnapshot(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cached_input_tokens=cached_input_tokens,
    )


def _estimate_cost_breakdown(
    usage_map: Mapping[str, UsageSnapshot],
    catalog: Mapping[str, Any],
) -> dict[str, Any]:
    models_catalog = catalog.get("models", {})
    line_items: list[dict[str, Any]] = []
    known_cost = Decimal("0")
    unknown_models: list[str] = []

    for raw_model_name, snapshot in usage_map.items():
        canonical_name = canonical_model_name(raw_model_name) or raw_model_name
        pricing = _lookup_pricing(models_catalog, raw_model_name, canonical_name)
        line_item = {
            "model": raw_model_name,
            "canonical_model": canonical_name,
            "input_tokens": snapshot.input_tokens,
            "output_tokens": snapshot.output_tokens,
            "total_tokens": snapshot.total_tokens,
            "cached_input_tokens": snapshot.cached_input_tokens,
        }

        if pricing is None or pricing.get("status") == "unknown":
            line_item["cost_usd"] = None
            line_item["cost_status"] = "unknown"
            unknown_models.append(raw_model_name)
        else:
            item_cost = _estimate_model_cost(snapshot, pricing)
            line_item["cost_usd"] = item_cost
            line_item["cost_status"] = "estimated"
            known_cost += Decimal(str(item_cost))
        line_items.append(line_item)

    if not line_items:
        return {
            "models": [],
            "cost": {"status": "unknown", "known_usd": None, "unknown_models": []},
        }

    if unknown_models and len(unknown_models) == len(line_items):
        status = "unknown"
        known_usd: float | None = None
    elif unknown_models:
        status = "partial"
        known_usd = _decimal_to_usd(known_cost)
    else:
        status = "estimated"
        known_usd = _decimal_to_usd(known_cost)

    return {
        "models": line_items,
        "cost": {
            "status": status,
            "known_usd": known_usd,
            "unknown_models": unknown_models,
        },
    }


def _lookup_pricing(
    catalog: Any,
    raw_model_name: str,
    canonical_name: str,
) -> Mapping[str, Any] | None:
    if not isinstance(catalog, Mapping):
        return None

    candidates = [
        raw_model_name,
        canonical_name,
        raw_model_name.lower(),
        canonical_name.lower(),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        entry = catalog.get(candidate)
        if isinstance(entry, Mapping):
            return entry
    return None


def _estimate_model_cost(snapshot: UsageSnapshot, pricing: Mapping[str, Any]) -> float:
    input_rate = Decimal(str(pricing.get("input_per_1m", 0)))
    cached_input_rate = Decimal(str(pricing.get("cached_input_per_1m", input_rate)))
    output_rate = Decimal(str(pricing.get("output_per_1m", 0)))

    uncached_input_tokens = max(snapshot.input_tokens - snapshot.cached_input_tokens, 0)
    input_cost = (Decimal(uncached_input_tokens) / Decimal(1_000_000)) * input_rate
    cached_input_cost = (
        Decimal(snapshot.cached_input_tokens) / Decimal(1_000_000)
    ) * cached_input_rate
    output_cost = (Decimal(snapshot.output_tokens) / Decimal(1_000_000)) * output_rate

    return _decimal_to_usd(input_cost + cached_input_cost + output_cost)


def _update_usage_log(usage_log: dict[str, Any], run_record: dict[str, Any]) -> None:
    usage_log.setdefault("updated_at", utc_now_iso())
    usage_log.setdefault("totals", _empty_usage_totals())
    usage_log.setdefault("by_model", {})
    usage_log.setdefault("recent_runs", [])
    usage_log["updated_at"] = run_record["timestamp"]

    totals = usage_log["totals"]
    run_totals = run_record["totals"]
    totals["input_tokens"] += run_totals["input_tokens"]
    totals["output_tokens"] += run_totals["output_tokens"]
    totals["total_tokens"] += run_totals["total_tokens"]

    known_cost = run_record["cost"].get("known_usd")
    if known_cost is not None:
        totals["known_cost_usd"] = _decimal_to_usd(
            Decimal(str(totals["known_cost_usd"])) + Decimal(str(known_cost))
        )

    for line_item in run_record["usage"]:
        model_name = line_item["canonical_model"]
        by_model = usage_log["by_model"].setdefault(
            model_name,
            {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "cached_input_tokens": 0,
                "known_cost_usd": 0.0,
            },
        )
        by_model["input_tokens"] += line_item["input_tokens"]
        by_model["output_tokens"] += line_item["output_tokens"]
        by_model["total_tokens"] += line_item["total_tokens"]
        by_model["cached_input_tokens"] += line_item["cached_input_tokens"]
        if line_item["cost_usd"] is not None:
            by_model["known_cost_usd"] = _decimal_to_usd(
                Decimal(str(by_model["known_cost_usd"])) + Decimal(str(line_item["cost_usd"]))
            )

    recent_runs = usage_log["recent_runs"]
    recent_runs.insert(0, run_record)
    del recent_runs[MAX_RECENT_RUNS:]


def _empty_usage_log() -> dict[str, Any]:
    return {
        "updated_at": None,
        "totals": _empty_usage_totals(),
        "by_model": {},
        "recent_runs": [],
    }


def _empty_usage_totals() -> dict[str, Any]:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "known_cost_usd": 0.0,
    }


def _round_ms(duration_seconds: float) -> int:
    return int(Decimal(str(duration_seconds * 1000)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _decimal_to_usd(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP))


def _format_run_summary(run_record: Mapping[str, Any]) -> str:
    tokens = run_record["totals"]["total_tokens"]
    model = run_record.get("effective_model") or run_record.get("requested_model") or "unknown"
    cost = run_record["cost"].get("known_usd")
    if cost is None:
        cost_text = "cost=unknown"
    else:
        cost_text = f"cost=${cost:.6f}"
    return (
        f"LLM run {run_record['operation']} status={run_record['status']} "
        f"model={model} tokens={tokens} {cost_text}"
    )
