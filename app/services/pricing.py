"""Versioned standard text-token price estimates, never a provider billing statement."""

import json
import math

from app.config import settings

# USD / 1M tokens; verified 2026-09-12.
# https://developers.openai.com/api/docs/models/gpt-5.4-mini
DEFAULT_PRICES = {
    "gpt-5.4-mini": {"input": 0.75, "cached_input": 0.075, "output": 4.50},
    "gpt-5.4-mini-2026-03-17": {"input": 0.75, "cached_input": 0.075, "output": 4.50},
}


def model_price(model):
    try:
        overrides = json.loads(settings.llm_prices_json)
        if not isinstance(overrides, dict):
            return None
        price = overrides.get(model, DEFAULT_PRICES.get(model))
        if not isinstance(price, dict):
            return None
        rates = {key: float(price[key]) for key in ("input", "cached_input", "output")}
        return rates if all(math.isfinite(rate) and rate >= 0 for rate in rates.values()) else None
    except (ValueError, TypeError, KeyError):
        return None
