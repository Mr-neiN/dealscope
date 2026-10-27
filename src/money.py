"""Money helpers.

All monetary values inside the scanner are integer minor units (euro cents).
Binary floating point is never used for financial arithmetic.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


class MoneyError(ValueError):
    """Raised when an amount cannot be parsed safely."""


def parse_amount_minor(value: object) -> int:
    """Convert a decimal string/number to minor units, rounding half up."""
    if value is None:
        raise MoneyError("amount is missing")
    if isinstance(value, bool):
        raise MoneyError(f"invalid amount: {value!r}")
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise MoneyError(f"invalid amount: {value!r}") from exc
    if not decimal_value.is_finite():
        raise MoneyError(f"invalid amount: {value!r}")
    minor = (decimal_value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(minor)


def parse_optional_minor(value: object) -> int | None:
    if value is None or value == "":
        return None
    return parse_amount_minor(value)


def parse_eur_minor(value: object) -> int:
    return parse_amount_minor(value)


def format_eur(minor: int | None, *, currency: bool = True) -> str:
    if minor is None:
        return "unknown"
    sign = "-" if minor < 0 else ""
    absolute = abs(minor)
    euros, cents = divmod(absolute, 100)
    body = f"{euros:,}".replace(",", ".")
    if cents:
        body = f"{body},{cents:02d}"
    return f"€{sign}{body}" if currency else f"{sign}{body}"


def format_amount(minor: int | None, currency_code: str) -> str:
    if currency_code.upper() == "EUR":
        return format_eur(minor)
    if minor is None:
        return "unknown"
    euros, cents = divmod(abs(minor), 100)
    sign = "-" if minor < 0 else ""
    return f"{sign}{euros:,}.{cents:02d} {currency_code.upper()}".replace(",", " ")


def pct(part: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return (part / total) * 100.0
