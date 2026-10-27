"""Title normalization and attribute extraction.

Normalization is deliberately deterministic. Matching the same text on both
sides (title and catalogue phrase) avoids most spacing and punctuation
problems, e.g. "RTX3090Ti" and "rtx 3090 ti" become the same token sequence.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

BRANDS = (
    "nvidia",
    "geforce",
    "amd",
    "radeon",
    "intel",
    "asus",
    "msi",
    "gigabyte",
    "evga",
    "zotac",
    "palit",
    "gainward",
    "inno3d",
    "pny",
    "sapphire",
    "powercolor",
    "xfx",
    "asrock",
    "crucial",
    "samsung",
    "western digital",
    "seasonic",
    "corsair",
    "kingston",
)

_APOSTROPHES = ("'", "\u2019", "`")
_GERMAN_REPLACEMENTS = {
    "ß": "ss",
    "ä": "ae",
    "ö": "oe",
    "ü": "ue",
    "Ä": "Ae",
    "Ö": "Oe",
    "Ü": "Ue",
}


def normalize_text(text: str) -> str:
    """Lowercase, transliterate and split letter/digit boundaries."""
    if not text:
        return ""
    value = str(text)
    for source, replacement in _GERMAN_REPLACEMENTS.items():
        value = value.replace(source, replacement)
    for apostrophe in _APOSTROPHES:
        value = value.replace(apostrophe, "")
    value = unicodedata.normalize("NFKD", value.casefold())
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = re.sub(r"[^a-z0-9]+", " ", value)
    value = re.sub(r"(?<=[a-z])(?=[0-9])|(?<=[0-9])(?=[a-z])", " ", value)
    return " ".join(value.split())


def normalize_phrase(phrase: str) -> str:
    return normalize_text(phrase)


def find_phrase_starts(tokens: Sequence[str], phrase_tokens: Sequence[str]) -> list[int]:
    """Return token indexes where the phrase occurs as a whole-token match."""
    if not phrase_tokens or len(phrase_tokens) > len(tokens):
        return []
    size = len(phrase_tokens)
    starts: list[int] = []
    for index in range(len(tokens) - size + 1):
        if list(tokens[index : index + size]) == list(phrase_tokens):
            starts.append(index)
    return starts


def contains_phrase(normalized_text: str, phrase: str) -> bool:
    phrase_tokens = normalize_phrase(phrase).split()
    if not phrase_tokens:
        return False
    return bool(find_phrase_starts(normalized_text.split(), phrase_tokens))


def contains_any(normalized_text: str, phrases: Iterable[str]) -> tuple[str, ...]:
    return tuple(phrase for phrase in phrases if contains_phrase(normalized_text, phrase))


@dataclass(frozen=True)
class ParsedTitle:
    raw: str
    normalized: str
    tokens: tuple[str, ...]
    attributes: Mapping[str, Any] = field(default_factory=dict)

    @property
    def brand_tokens(self) -> tuple[str, ...]:
        token_set = set(self.tokens)
        found: list[str] = []
        for brand in BRANDS:
            brand_tokens = brand.split()
            if all(token in token_set for token in brand_tokens):
                found.append(brand)
        return tuple(found)


def parse_title(title: str) -> ParsedTitle:
    normalized = normalize_text(title)
    tokens = tuple(normalized.split())
    attributes = extract_attributes(normalized)
    return ParsedTitle(raw=title, normalized=normalized, tokens=tokens, attributes=attributes)


def extract_attributes(normalized: str) -> dict[str, Any]:
    attributes: dict[str, Any] = {}
    capacity_gb = _capacity_total_gb(normalized)
    capacity_tb = _capacity_total_tb(normalized)
    if capacity_gb is not None:
        attributes["capacity_gb"] = capacity_gb
    if capacity_tb is not None:
        attributes["capacity_tb"] = capacity_tb
    if "gddr" in normalized.split() and capacity_gb is not None:
        attributes["vram_gb"] = capacity_gb

    ram_gen = re.search(r"\bddr\s*([345])\b", normalized)
    if ram_gen:
        attributes["ram_gen"] = f"ddr{ram_gen.group(1)}"

    socket = re.search(r"\b(am\s*[45]|lga\s*\d{3,4}|strx\s*\d?)\b", normalized)
    if socket:
        attributes["socket"] = socket.group(1).replace(" ", "")

    interfaces = [name for name in ("nvme", "sata", "pcie") if name in normalized.split()]
    if interfaces:
        attributes["interface"] = interfaces[0]

    if "ecc" in normalized.split() or "ecc" in re.findall(r"[a-z]+", normalized):
        attributes["ecc"] = True
    if "registered" in normalized.split() or "rdimm" in normalized.split():
        attributes["registered"] = True

    form_factor = re.search(r"\b(atx|matx|micro atx|itx|mini itx)\b", normalized)
    if form_factor:
        attributes["form_factor"] = form_factor.group(1).replace(" ", "_")

    return attributes


def _capacity_total_gb(normalized: str) -> int | None:
    totals: list[int] = []
    for left, right in re.findall(r"\b(\d{1,3})\s+x\s*(\d{1,3})\s+gb\b", normalized):
        totals.append(int(left) * int(right))
    for single in re.findall(r"\b(\d{1,3})\s+gb\b", normalized):
        totals.append(int(single))
    return max(totals) if totals else None


def _capacity_total_tb(normalized: str) -> int | None:
    totals: list[int] = []
    for left, right in re.findall(r"\b(\d{1,2})\s+x\s*(\d{1,2})\s+tb\b", normalized):
        totals.append(int(left) * int(right))
    for single in re.findall(r"\b(\d{1,2})\s+tb\b", normalized):
        totals.append(int(single))
    return max(totals) if totals else None


def condition_class_from_ebay(label: str | None) -> str:
    """Map an eBay condition label to one of our valuation classes."""
    if not label:
        return "unknown"
    normalized = normalize_text(label)
    if "for parts" in normalized or "not working" in normalized or "parts only" in normalized:
        return "for_parts"
    if "refurb" in normalized:
        return "refurbished"
    if "open box" in normalized:
        return "new"
    if "new" in normalized.split() or "brand new" in normalized or "sealed" in normalized:
        return "new"
    if "used" in normalized.split() or "pre owned" in normalized or "good" in normalized:
        return "used_working"
    if "acceptable" in normalized or "very good" in normalized:
        return "used_working"
    return "unknown"


def condition_flags(label: str | None) -> tuple[str, ...]:
    normalized = normalize_text(label or "")
    flags: list[str] = []
    if "open box" in normalized or "new other" in normalized:
        flags.append("open_box")
    if "for parts" in normalized:
        flags.append("for_parts_condition")
    return tuple(flags)
