"""Optional delivery text in the public product's delivery widget."""

import re
from typing import Any

from ozon_mcp.parsing.visible_cards import _parse, _text, _walk


def delivery_from_document(document: dict[str, Any]) -> str | None:
    """Keep a unique labeled declaration; absence is not free/instant delivery."""
    root = _parse(document.get("html") or "")
    declarations = {
        text
        for node in _walk(root)
        if node.attrs.get("data-widget") == "webDelivery"
        and (text := _text(node))
        and re.search(r"\b(?:Доставим|Доставка|Послезавтра|Завтра|Сегодня)\b", text, re.IGNORECASE)
    }
    return next(iter(declarations)) if len(declarations) == 1 else None
