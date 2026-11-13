"""Review fields trimmed from public DOM captures made on 2026-11-13."""

import json
from html import escape
from pathlib import Path
from typing import Any


def review_states(sku: str) -> dict[str, Any]:
    return json.loads(Path(f"tests/fixtures/public-reviews-{sku}.json").read_text(encoding="utf-8"))


def review_markup(states: dict[str, Any]) -> str:
    nodes = "".join(
        f'<div id="state-{key}" data-state="{escape(json.dumps(value, ensure_ascii=False), quote=True)}"></div>'
        for key, value in states.items()
    )
    return (
        '<meta charset="utf-8"><title>Отзывы о товаре</title><div data-widget="webListReviews"></div>'
        f'<div class="client-state" style="display:none">{nodes}</div>'
    )
