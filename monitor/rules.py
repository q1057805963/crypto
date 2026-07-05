DEFAULT_TRIGGER_CONDITIONS: dict[str, dict[str, float | bool]] = {
    "score": {"enabled": True, "threshold": 60.0},
    "quote_volume_1m": {"enabled": False, "threshold": 300000.0},
    "volume_multiplier": {"enabled": False, "threshold": 2.2},
    "price_move_pct_1m_abs": {"enabled": False, "threshold": 0.6},
    "oi_change_pct_5m_abs": {"enabled": False, "threshold": 0.8},
    "liquidation_total_quote_1m": {"enabled": False, "threshold": 75000.0},
    "depth_imbalance_abs": {"enabled": False, "threshold": 22.0},
    "depth_drop_pct_1m": {"enabled": False, "threshold": 15.0},
    "spread_bps": {"enabled": False, "threshold": 3.0},
}


def normalize_trigger_rules(
    raw_rules: dict | None,
    *,
    default_score: float = 60.0,
    score_enabled: bool = True,
) -> dict:
    rules = raw_rules if isinstance(raw_rules, dict) else {}
    conditions = rules.get("conditions") if isinstance(rules.get("conditions"), dict) else {}
    normalized = {}

    for key, default in DEFAULT_TRIGGER_CONDITIONS.items():
        value = conditions.get(key) if isinstance(conditions.get(key), dict) else {}
        threshold = float(value.get("threshold", default["threshold"]))
        enabled = bool(value.get("enabled", default["enabled"]))
        if key == "score":
            threshold = float(value.get("threshold", default_score))
            enabled = bool(value.get("enabled", score_enabled))
        normalized[key] = {
            "enabled": enabled,
            "threshold": threshold,
        }

    return {
        "mode": "all" if rules.get("mode") == "all" else "any",
        "conditions": normalized,
    }


def enabled_trigger_count(rules: dict | None) -> int:
    normalized = normalize_trigger_rules(rules, score_enabled=False)
    return sum(
        1 for cfg in normalized.get("conditions", {}).values() if bool(cfg.get("enabled"))
    )


def metric_value(key: str, data: dict | None) -> float | None:
    payload = data or {}
    if key == "score":
        return float(payload.get("score", 0) or 0)
    if key == "quote_volume_1m":
        return float(payload.get("quote_volume_1m", 0) or 0)
    if key == "volume_multiplier":
        return float(payload.get("volume_multiplier", 0) or 0)
    if key == "price_move_pct_1m_abs":
        return abs(float(payload.get("price_move_pct_1m", 0) or 0))
    if key == "oi_change_pct_5m_abs":
        return abs(float(payload.get("oi_change_pct_5m", 0) or 0))
    if key == "liquidation_total_quote_1m":
        return float(payload.get("liquidation_total_quote_1m", 0) or 0)
    if key == "depth_imbalance_abs":
        return abs(float(payload.get("depth_imbalance", 0) or 0)) * 100
    if key == "depth_drop_pct_1m":
        return float(payload.get("depth_drop_pct_1m", 0) or 0)
    if key == "spread_bps":
        return float(payload.get("spread_bps", 0) or 0)
    return None


def evaluate_trigger_rules(rules: dict | None, data: dict | None) -> bool:
    status = trigger_rule_status(rules, data)
    checks = [bool(item.get("matched")) for item in status.get("checks", [])]
    if not checks:
        return False
    if status.get("mode") == "all":
        return all(checks)
    return any(checks)


def trigger_rule_status(rules: dict | None, data: dict | None) -> dict:
    normalized = normalize_trigger_rules(rules, score_enabled=False)
    checks = []
    for key, cfg in normalized.get("conditions", {}).items():
        if not cfg.get("enabled"):
            continue
        value = metric_value(key, data)
        if value is None:
            continue
        threshold = float(cfg.get("threshold", 0))
        checks.append(
            {
                "key": key,
                "value": value,
                "threshold": threshold,
                "matched": value >= threshold,
            }
        )

    matched_values = [bool(item["matched"]) for item in checks]
    matched = False
    if matched_values:
        matched = all(matched_values) if normalized.get("mode") == "all" else any(matched_values)
    return {
        "mode": normalized.get("mode", "any"),
        "matched": matched,
        "checks": checks,
    }
