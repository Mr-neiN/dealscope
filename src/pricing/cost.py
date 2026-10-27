"""Total acquisition cost.

The total is deliberately explicit: item price, shipping, import charges and
fees. VAT already included in an EU eBay price is never added a second time.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from src.config import AlertConfig, CategorySpec
from src.ebay.models import EU_COUNTRIES

# Conservative fallback for sellers outside the EU when eBay does not supply
# an import charge. Italian import VAT is 22%; duty varies by goods and is not
# applied to used personal computer hardware in this model.
IMPORT_VAT_PCT = 22.0
IMPORT_DUTY_PCT = 0.0


@dataclass(frozen=True)
class CostBreakdown:
    price_eur_minor: int
    shipping_eur_minor: int
    import_charges_eur_minor: int
    tax_eur_minor: int
    fees_eur_minor: int
    total_eur_minor: int
    shipping_known: bool
    import_estimated: bool
    notes: tuple[str, ...] = ()

    @property
    def non_price_costs_minor(self) -> int:
        return self.total_eur_minor - self.price_eur_minor


def compute_cost(
    *,
    price_eur_minor: int,
    shipping_eur_minor: int | None,
    import_charges_eur_minor: int | None,
    seller_country: str | None,
    delivery_country: str,
    category: CategorySpec,
    alerts: AlertConfig,
    fees_eur_minor: int = 0,
) -> CostBreakdown:
    notes: list[str] = []
    shipping_known = shipping_eur_minor is not None
    if shipping_known:
        shipping = max(0, int(shipping_eur_minor or 0))
    else:
        fallback = max(
            category.default_shipping_eur * 100,
            alerts.unknown_shipping_estimate_eur * 100,
        )
        shipping = fallback
        notes.append(f"shipping unknown; {fallback / 100:.2f} EUR estimated")

    import_estimated = False
    import_known = import_charges_eur_minor is not None
    import_charges = max(0, int(import_charges_eur_minor or 0))
    seller = (seller_country or "").upper()
    if (
        not import_known
        and seller
        and seller not in EU_COUNTRIES
        and delivery_country.upper() in EU_COUNTRIES
    ):
        taxable = price_eur_minor + shipping
        estimate = (Decimal(taxable) * Decimal(str(IMPORT_VAT_PCT + IMPORT_DUTY_PCT)) / 100).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
        import_charges = int(estimate)
        import_estimated = True
        notes.append(
            "seller outside the EU; import VAT estimated at "
            f"{IMPORT_VAT_PCT:.0f}% (eBay charges may differ)"
        )
    elif import_charges > 0:
        notes.append("import charges as quoted by eBay")

    # EU prices already include VAT for private sellers. eBay's checkout total
    # is the source of truth; adding VAT again would double count it.
    tax = 0
    total = price_eur_minor + shipping + import_charges + tax + max(0, fees_eur_minor)
    return CostBreakdown(
        price_eur_minor=price_eur_minor,
        shipping_eur_minor=shipping,
        import_charges_eur_minor=import_charges,
        tax_eur_minor=tax,
        fees_eur_minor=max(0, fees_eur_minor),
        total_eur_minor=total,
        shipping_known=shipping_known,
        import_estimated=import_estimated,
        notes=tuple(notes),
    )
