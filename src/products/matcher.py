"""Exact product identification.

This module never guesses. If two different products match a title equally
well, the result is ambiguous and the listing is not valued. Model suffixes
such as "Ti", "Super", "XTX" and "X3D" change identity and are guarded both by
explicit forbidden phrases and by a suffix rule.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from src.config import ProductSpec
from src.products.parser import (
    ParsedTitle,
    contains_phrase,
    find_phrase_starts,
    normalize_phrase,
    parse_title,
)


@dataclass(frozen=True)
class ProductMatch:
    product: ProductSpec
    confidence: float
    matched_phrase: str
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class MatchResult:
    parsed: ParsedTitle
    primary: ProductMatch | None
    components: tuple[ProductMatch, ...] = ()
    ambiguous: bool = False
    candidate_ids: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def product_id(self) -> str | None:
        return self.primary.product.id if self.primary else None

    @property
    def overall_confidence(self) -> float:
        return self.primary.confidence if self.primary else 0.0


@dataclass(frozen=True)
class _CompiledProduct:
    product: ProductSpec
    phrases: tuple[tuple[str, ...], ...]
    forbidden: tuple[tuple[str, ...], ...]
    required: tuple[tuple[str, ...], ...]
    spec: Mapping[str, Any]


class ProductMatcher:
    def __init__(
        self,
        products: Sequence[ProductSpec],
        variant_suffixes: Sequence[str],
    ) -> None:
        self._suffixes = {normalize_phrase(suffix) for suffix in variant_suffixes if suffix}
        self._products = tuple(self._compile(product) for product in products)

    @staticmethod
    def _compile(product: ProductSpec) -> _CompiledProduct:
        raw_phrases = list(product.aliases)
        if product.name and product.name not in raw_phrases:
            raw_phrases.append(product.name)
        phrases: list[tuple[str, ...]] = []
        for phrase in raw_phrases:
            normalized = normalize_phrase(phrase)
            tokens = tuple(normalized.split())
            if tokens and tokens not in phrases:
                phrases.append(tokens)
        forbidden = tuple(
            tuple(normalize_phrase(phrase).split())
            for phrase in product.forbidden_tokens
            if normalize_phrase(phrase)
        )
        required = tuple(
            tuple(normalize_phrase(term).split())
            for term in product.required_tokens
            if normalize_phrase(term)
        )
        return _CompiledProduct(
            product=product,
            phrases=tuple(phrases),
            forbidden=forbidden,
            required=required,
            spec=dict(product.spec),
        )

    def match_title(self, title: str) -> MatchResult:
        parsed = parse_title(title)
        return self.match_parsed(parsed)

    def match_parsed(self, parsed: ParsedTitle) -> MatchResult:
        matches: list[ProductMatch] = []
        notes: list[str] = []
        for compiled in self._products:
            outcome = self._match_product(compiled, parsed)
            if isinstance(outcome, ProductMatch):
                matches.append(outcome)
            elif outcome:
                notes.append(outcome)

        if not matches:
            return MatchResult(
                parsed=parsed,
                primary=None,
                components=(),
                ambiguous=False,
                candidate_ids=(),
                notes=tuple(notes),
            )

        matches.sort(
            key=lambda item: (
                len(item.matched_phrase.split()),
                item.confidence,
                len(item.product.id),
            ),
            reverse=True,
        )
        top = matches[0]
        tied = [
            match
            for match in matches
            if len(match.matched_phrase.split()) == len(top.matched_phrase.split())
            and match.product.id != top.product.id
        ]
        ambiguous = bool(tied)
        components = tuple(matches)
        candidate_ids = tuple(match.product.id for match in matches)
        if ambiguous:
            notes.append("ambiguous_product_identity")
            return MatchResult(
                parsed=parsed,
                primary=None,
                components=components,
                ambiguous=True,
                candidate_ids=candidate_ids,
                notes=tuple(notes),
            )
        return MatchResult(
            parsed=parsed,
            primary=top,
            components=components,
            ambiguous=False,
            candidate_ids=candidate_ids,
            notes=tuple(notes),
        )

    def _match_product(
        self, compiled: _CompiledProduct, parsed: ParsedTitle
    ) -> ProductMatch | str | None:
        tokens = parsed.tokens
        for forbidden in compiled.forbidden:
            if find_phrase_starts(tokens, forbidden):
                return f"rejected:{compiled.product.id}:forbidden_phrase"
        for required in compiled.required:
            if not find_phrase_starts(tokens, required):
                return f"rejected:{compiled.product.id}:missing_required_token"

        best_length = 0
        best_phrase: tuple[str, ...] | None = None
        for phrase in compiled.phrases:
            starts = find_phrase_starts(tokens, phrase)
            valid_starts = [
                start
                for start in starts
                if not self._suffix_guard(tokens, phrase, start)
            ]
            if not valid_starts:
                continue
            if len(phrase) > best_length:
                best_length = len(phrase)
                best_phrase = phrase

        if best_phrase is None:
            return None

        spec_reasons, spec_mismatch = self._check_spec(compiled.spec, parsed.attributes)
        if spec_mismatch:
            return f"rejected:{compiled.product.id}:spec_mismatch"

        confidence = 0.55 + 0.09 * (best_length - 1)
        reasons: list[str] = [f"phrase_tokens={best_length}"]
        if parsed.brand_tokens:
            confidence += 0.04
            reasons.append("brand_present")
        if spec_reasons:
            confidence += 0.05
            reasons.extend(spec_reasons)
        if len(compiled.product.id) > 0 and any(
            match in parsed.normalized for match in ("ti", "super", "xtx", "x3d")
        ):
            reasons.append("variant_token_present")
        confidence = min(confidence, 0.95)

        return ProductMatch(
            product=compiled.product,
            confidence=round(confidence, 3),
            matched_phrase=" ".join(best_phrase),
            reasons=tuple(reasons),
        )

    def _suffix_guard(
        self, tokens: Sequence[str], phrase: Sequence[str], start: int
    ) -> bool:
        end = start + len(phrase)
        if end >= len(tokens):
            return False
        next_token = tokens[end]
        if next_token not in self._suffixes:
            return False
        return next_token not in phrase

    @staticmethod
    def _check_spec(
        spec: Mapping[str, Any], attributes: Mapping[str, Any]
    ) -> tuple[tuple[str, ...], bool]:
        reasons: list[str] = []
        for key, expected in spec.items():
            if key not in attributes:
                continue
            actual = attributes[key]
            if isinstance(expected, bool) or isinstance(actual, bool):
                matches = bool(expected) == bool(actual)
            else:
                try:
                    matches = int(actual) == int(expected)
                except (TypeError, ValueError):
                    matches = str(actual) == str(expected)
            if not matches:
                return (), True
            reasons.append(f"spec_match:{key}")
        return tuple(reasons), False
