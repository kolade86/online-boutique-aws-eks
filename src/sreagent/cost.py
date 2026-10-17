"""Estimated cost of the agent's model calls, from token counts and a price file.

The prices are data, not code: model_prices.json (or SREAGENT_PRICES_FILE)
holds USD per million tokens for each model, with the date they were checked
and where. A model missing from the file is never guessed: its tokens are
counted and its cost is reported as unknown.
"""

import json
import os
from dataclasses import dataclass

PRICES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "model_prices.json")
TOKEN_TYPES = ("input", "output", "cache_read", "cache_write")


@dataclass(frozen=True)
class Prices:
    checked: str
    source: str
    models: dict          # model -> {token type: USD per million tokens}

    def cost(self, model: str, tokens: dict):
        """USD for these tokens on this model, or None if the model has no price."""
        rates = self.models.get(model)
        if rates is None:
            return None
        return sum(tokens.get(t, 0) * rates[t] for t in TOKEN_TYPES) / 1_000_000


def load_prices(path: str = PRICES_FILE) -> Prices:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    for model, rates in data["models"].items():
        missing = [t for t in TOKEN_TYPES if not isinstance(rates.get(t), (int, float))]
        if missing:
            raise ValueError(f"{path}: {model} has no price for {', '.join(missing)}")
    return Prices(data["checked"], data["source"], data["models"])


def summarize(usage: list, prices: Prices) -> dict:
    """The usage block for /investigations and the log: token totals, the same per
    model, and the estimated cost. `cost_usd` is None if any model is unpriced."""
    by_model: dict = {}
    for entry in usage:
        tokens = by_model.setdefault(entry["model"], dict.fromkeys(TOKEN_TYPES, 0))
        for t in TOKEN_TYPES:
            tokens[t] += entry.get(t, 0)
    totals = {t: sum(m[t] for m in by_model.values()) for t in TOKEN_TYPES}
    costs = {model: prices.cost(model, tokens) for model, tokens in by_model.items()}
    unpriced = sorted(m for m, c in costs.items() if c is None)
    return {
        **totals,
        "by_model": {m: {**by_model[m], "cost_usd": _round(costs[m])} for m in by_model},
        "cost_usd": None if unpriced else _round(sum(costs.values())),
        "unpriced_models": unpriced,
        "prices_checked": prices.checked,
    }


def _round(value):
    return None if value is None else round(value, 6)
