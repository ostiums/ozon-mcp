"""Conservative nutrition normalization from labeled, rendered product fields.

Accept explicit g/100g or calculate from explicit serving grams and protein.
Never inspect a title, pack weight, serving count, image, API or account data.
The result is a deterministic string with source/basis, rounded to 2 decimals.
Conflicts, invalid units and missing measurements stay None.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from ozon_mcp.parsing.visible_cards import _Node, _text, _walk

_NUMBER = r"\d{1,6}(?:[.,]\d{1,6})?"
_GRAMS = r"(?:г(?:рамм(?:а|ов)?)?|g(?:rams?)?)"
_HUNDRED = re.compile(r"(?:\b(?:на|в|per)\s*100\s*" + _GRAMS + r"\b)", re.IGNORECASE)
_SERVING = re.compile(r"(?:\b(?:на|в)\s*(?:1\s*)?порци[юияе]\b|\bper\s+serving\b)", re.IGNORECASE)
_PROTEIN = re.compile(
    r"^(?:белок|белки|протеин|protein)(?:\s*[,:(]\s*" + _GRAMS + r"\s*\)?|\s+" + _GRAMS + r")?\s*$", re.IGNORECASE
)
_SERVING_SIZE = re.compile(
    r"(?:\b(?:размер|масса|вес)\s+порции|\bпорци[яию]|\bserving(?:\s+size)?)"
    r"\s*(?:[:=—-]\s*)?\(?\s*(" + _NUMBER + r")\s*" + _GRAMS + r"\b",
    re.IGNORECASE,
)
_SIZE_LABEL = re.compile(
    r"^(?:(?:размер|масса|вес)\s+порции|порция|serving\s+size)(?:\s*[,:(]\s*" + _GRAMS + r"\s*\)?)?$", re.IGNORECASE
)
_OTHER_UNIT = re.compile(r"\b(?:мг|mg|milligrams?|кг|kg|kilograms?|oz|ounces?|мл|ml|ккал|kcal|kj)\b|%", re.IGNORECASE)


def _clean(text: str) -> str:
    return " ".join(text.casefold().replace("ё", "е").split()).strip(" :")


def _basis(text: str) -> str | None:
    hundred, serving = bool(_HUNDRED.search(text)), bool(_SERVING.search(text))
    return "100g" if hundred and not serving else "serving" if serving and not hundred else None


def _grams(value: str, *, bare: bool = True) -> Decimal | None:
    unit = r"(?:\s*" + _GRAMS + r")" + ("?" if bare else "")
    match = re.fullmatch(r"\s*(" + _NUMBER + r")" + unit + r"\s*", value, re.IGNORECASE)
    return Decimal(match[1].replace(",", ".")) if match else None


def _one(values: list[Decimal | None]) -> Decimal | None:
    unique = set(values)
    return next(iter(unique)) if len(unique) == 1 and None not in unique else None


def _fmt(value: Decimal) -> str:
    return (
        format(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f").rstrip("0").rstrip(".")
        if value % 1
        else str(int(value))
    )


def _children(node: _Node) -> list[_Node]:
    return [n for n in node.children if isinstance(n, _Node)]


def _in_table(node: _Node) -> bool:
    parent = node.parent
    while parent is not None:
        if parent.tag == "table":
            return True
        parent = parent.parent
    return False


@dataclass
class _Declarations:
    direct: list[Decimal | None] = field(default_factory=list)
    portion: list[Decimal | None] = field(default_factory=list)
    sizes: list[Decimal | None] = field(default_factory=list)

    def add(self, basis: str | None, value: str) -> None:
        if basis == "100g":
            self.direct.append(_grams(value))
        elif basis == "serving":
            self.portion.append(_grams(value))

    def serving_sizes(self, text: str) -> None:
        self.sizes.extend(Decimal(m[1].replace(",", ".")) for m in _SERVING_SIZE.finditer(text))

    def properties(self, properties: dict[str, list[str]]) -> None:
        for key, values in properties.items():
            label = _clean(key)
            if _SIZE_LABEL.fullmatch(label):
                bare = bool(re.search(r"[,:(]\s*" + _GRAMS + r"\s*\)?$", label))
                self.sizes.extend(_grams(v, bare=bare) for v in values)
            if re.match(r"^(?:белок|белки|протеин|protein)\b", label):
                for value in values:
                    self.add(_basis(label), "" if _OTHER_UNIT.search(label) else value)
            self.serving_sizes(key)

    def cards(self, nutrition: list[_Node]) -> None:
        for widget in nutrition:
            # Observed nutrient cards have a basis heading and paired value/label
            # children. Mixed-basis widgets need labeled table columns instead.
            text = _text(widget)
            basis = _basis(text)
            self.serving_sizes(text)
            for row in _walk(widget):
                if row.tag == "table" or _in_table(row):
                    continue
                cells = _children(row)
                if len(cells) != 2:
                    continue
                left, right = (_clean(_text(n)) for n in cells)
                if _PROTEIN.fullmatch(left):
                    self.add(basis, right)
                elif _PROTEIN.fullmatch(right):
                    self.add(basis, left)

    def table(self, table: _Node) -> None:
        # Col/row spans require a richer layout model; do not guess alignment.
        if any(n.attrs.get(k, "1") != "1" for n in _walk(table) for k in ("colspan", "rowspan")):
            return
        table_basis = _basis(_text(table))
        self.serving_sizes(_text(table))
        headers: list[str | None] = []
        for row in (n for n in _walk(table) if n.tag == "tr"):
            cells = [n for n in _children(row) if n.tag in {"th", "td"}]
            if not cells:
                continue
            texts = [_clean(_text(n)) for n in cells]
            if re.match(r"^(?:белок|белки|протеин|protein)\b", texts[0]):
                for i in range(1, len(cells)):
                    basis = headers[i] if len(headers) == len(cells) else None
                    self.add(basis or _basis(texts[0]) or table_basis, "" if _OTHER_UNIT.search(texts[0]) else texts[i])
            else:
                candidates = [_basis(t) for t in texts]
                if any(candidates):
                    headers = candidates

    def result(self) -> str | None:
        # Explicit per-100g always wins, including its conflicts: an ambiguous
        # explicit declaration must not silently fall back to calculated data.
        if self.direct:
            value = _one(self.direct)
            return f"{_fmt(value)} г/100 г (указано на странице)" if value is not None and 0 <= value <= 100 else None
        protein, serving = _one(self.portion), _one(self.sizes)
        if protein is None or serving is None or serving <= 0 or not 0 <= protein <= serving:
            return None
        result = protein * 100 / serving
        return f"{_fmt(result)} г/100 г (расчёт: {_fmt(protein)} г белка / порция {_fmt(serving)} г)"


def protein_per_100g(properties: dict[str, list[str]], nutrition: list[_Node], descriptions: list[_Node]) -> str | None:
    declarations = _Declarations()
    declarations.properties(properties)
    declarations.cards(nutrition)
    for widget in [*nutrition, *descriptions]:
        for table in (node for node in _walk(widget) if node.tag == "table"):
            declarations.table(table)
    return declarations.result()
