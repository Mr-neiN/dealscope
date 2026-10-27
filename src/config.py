"""Application configuration loaded from environment variables and YAML.

The YAML files under config/ are the operator-facing interface. This module
validates them at startup and fails with an actionable message instead of
running the scanner with a half-valid catalogue.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Mapping

import yaml

from src.money import MoneyError, parse_eur_minor

CONDITION_CLASSES = ("new", "used_working", "refurbished", "for_parts", "unknown")
PRIORITIES = ("high", "normal", "low")


class ConfigError(ValueError):
    """Configuration is missing or inconsistent."""


@dataclass(frozen=True)
class Settings:
    ebay_client_id: str
    ebay_client_secret: str
    ebay_environment: str
    telegram_bot_token: str
    telegram_chat_id: str
    ship_to_country: str
    delivery_postal_code: str | None
    database_path: Path
    config_dir: Path
    log_level: str
    timezone: str
    shadow_mode: bool
    daily_call_budget: int | None
    health_max_age_seconds: int
    http_timeout_seconds: float
    http_max_attempts: int

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def ebay_configured(self) -> bool:
        return bool(self.ebay_client_id and self.ebay_client_secret)


@dataclass(frozen=True)
class DeliveryConfig:
    country: str
    postal_code: str | None


@dataclass(frozen=True)
class BudgetConfig:
    daily_calls: int
    reserve_calls: int


@dataclass(frozen=True)
class ScanConfig:
    new_listings_minutes: int
    auctions_minutes: int
    variant_searches_hours: int
    bundle_searches_hours: int
    discovery_hours: int
    broad_discovery_enabled: bool
    max_pages: int
    item_detail_daily_cap: int
    stale_after_hours: int
    unknown_after_hours: int
    taxonomy_cache_days: int


@dataclass(frozen=True)
class AlertConfig:
    max_per_day: int
    material_price_drop_pct: float
    unknown_shipping_estimate_eur: int
    min_confidence: str
    allow_provisional: bool
    auction_alert_hours: int


@dataclass(frozen=True)
class RankingConfig:
    full_discount_score_pct: float
    full_savings_score_eur: int
    auction_safety_margin_pct: float
    bundle_haircut_pct: float
    bundle_min_discount_pct: float
    bundle_min_savings_eur: int
    untested_value_haircut_pct: float
    low_seller_feedback_pct: float


@dataclass(frozen=True)
class RetentionConfig:
    observation_days: int
    alert_days: int
    api_call_days: int
    backup_days: int


@dataclass(frozen=True)
class CategorySpec:
    id: str
    label: str
    taxonomy_hint: str | None
    min_discount_pct: float
    min_savings_eur: int
    full_savings_score_eur: int
    default_shipping_eur: int


@dataclass(frozen=True)
class ProductSpec:
    id: str
    name: str
    category: str
    priority: str
    condition_class: str
    aliases: tuple[str, ...]
    required_tokens: tuple[str, ...]
    forbidden_tokens: tuple[str, ...]
    spec: Mapping[str, Any]
    queries: tuple[str, ...]
    variant_queries: tuple[str, ...]
    auction_queries: tuple[str, ...]


@dataclass(frozen=True)
class BundleSpec:
    id: str
    name: str
    category: str
    priority: str
    queries: tuple[str, ...]
    use_category_filter: bool


@dataclass(frozen=True)
class BroadSearch:
    query: str
    category: str
    marketplaces: tuple[str, ...]
    use_category_filter: bool


@dataclass(frozen=True)
class ReferencePrice:
    product_id: str
    condition_class: str
    price_eur_minor: int
    sample_size: int
    verified: bool
    source: str
    observed_at: date | None


@dataclass(frozen=True)
class ExclusionRules:
    defect_terms: tuple[str, ...]
    untested_terms: tuple[str, ...]
    box_only_terms: tuple[str, ...]
    accessory_terms: tuple[str, ...]
    accessory_only_terms: tuple[str, ...]
    accessory_override_phrases: tuple[str, ...]
    bundle_terms: tuple[str, ...]
    ambiguous_terms: tuple[str, ...]
    suspicious_terms: tuple[str, ...]

    def all_terms(self) -> tuple[str, ...]:
        return (
            self.defect_terms
            + self.untested_terms
            + self.box_only_terms
            + self.accessory_terms
            + self.accessory_only_terms
            + self.bundle_terms
            + self.ambiguous_terms
            + self.suspicious_terms
        )


@dataclass(frozen=True)
class AppConfig:
    settings: Settings
    marketplaces: tuple[str, ...]
    delivery: DeliveryConfig
    budget: BudgetConfig
    scan: ScanConfig
    alerts: AlertConfig
    ranking: RankingConfig
    retention: RetentionConfig
    categories: Mapping[str, CategorySpec]
    products: tuple[ProductSpec, ...]
    bundles: tuple[BundleSpec, ...]
    broad_searches: tuple[BroadSearch, ...]
    references: tuple[ReferencePrice, ...]
    exclusions: ExclusionRules
    variant_suffixes: tuple[str, ...]
    product_by_id: Mapping[str, ProductSpec] = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "product_by_id", {product.id: product for product in self.products}
        )

    @property
    def effective_daily_budget(self) -> int:
        if self.settings.daily_call_budget is not None:
            return self.settings.daily_call_budget
        return self.budget.daily_calls

    def category(self, category_id: str) -> CategorySpec:
        try:
            return self.categories[category_id]
        except KeyError as exc:
            raise ConfigError(f"unknown category: {category_id}") from exc


def load_config(
    env: Mapping[str, str] | None = None,
    *,
    base_dir: Path | None = None,
) -> AppConfig:
    """Load settings and YAML configuration.

    Environment variables win over values in .env. The function does not
    require eBay credentials so database and reporting commands remain usable
    without them; `run` and `feasibility` validate credentials themselves.
    """

    base = Path(base_dir) if base_dir is not None else Path(__file__).resolve().parent.parent
    environment = dict(os.environ if env is None else env)
    _apply_dotenv(environment, base / ".env")
    settings = _load_settings(environment, base)
    config_dir = settings.config_dir

    config_path = config_dir / "products.yaml"
    references_path = config_dir / "references.yaml"
    exclusions_path = config_dir / "exclusions.yaml"
    for path in (config_path, references_path, exclusions_path):
        if not path.is_file():
            raise ConfigError(f"missing configuration file: {path}")

    raw = _load_yaml(config_path)
    references_raw = _load_yaml(references_path)
    exclusions_raw = _load_yaml(exclusions_path)

    marketplaces = _parse_marketplaces(raw.get("marketplaces"))
    delivery_raw = _section(raw, "delivery")
    delivery_country = _text(
        delivery_raw.get("country", settings.ship_to_country), "delivery.country"
    ).upper()
    if len(delivery_country) != 2:
        raise ConfigError("delivery.country must be a two-letter ISO country code")

    budget_raw = _section(raw, "budget")
    budget = BudgetConfig(
        daily_calls=_positive_int(budget_raw.get("daily_calls", 4000), "budget.daily_calls"),
        reserve_calls=_non_negative_int(
            budget_raw.get("reserve_calls", 500), "budget.reserve_calls"
        ),
    )
    if budget.reserve_calls >= budget.daily_calls:
        raise ConfigError("budget.reserve_calls must be lower than budget.daily_calls")

    scan_raw = _section(raw, "scan")
    scan = ScanConfig(
        new_listings_minutes=_positive_int(
            scan_raw.get("new_listings_minutes", 30), "scan.new_listings_minutes"
        ),
        auctions_minutes=_positive_int(
            scan_raw.get("auctions_minutes", 60), "scan.auctions_minutes"
        ),
        variant_searches_hours=_positive_int(
            scan_raw.get("variant_searches_hours", 6), "scan.variant_searches_hours"
        ),
        bundle_searches_hours=_positive_int(
            scan_raw.get("bundle_searches_hours", 6), "scan.bundle_searches_hours"
        ),
        discovery_hours=_positive_int(
            scan_raw.get("discovery_hours", 24), "scan.discovery_hours"
        ),
        broad_discovery_enabled=_bool(
            scan_raw.get("broad_discovery_enabled", True), "scan.broad_discovery_enabled"
        ),
        max_pages=_positive_int(scan_raw.get("max_pages", 2), "scan.max_pages"),
        item_detail_daily_cap=_non_negative_int(
            scan_raw.get("item_detail_daily_cap", 120), "scan.item_detail_daily_cap"
        ),
        stale_after_hours=_positive_int(
            scan_raw.get("stale_after_hours", 72), "scan.stale_after_hours"
        ),
        unknown_after_hours=_positive_int(
            scan_raw.get("unknown_after_hours", 336), "scan.unknown_after_hours"
        ),
        taxonomy_cache_days=_positive_int(
            scan_raw.get("taxonomy_cache_days", 30), "scan.taxonomy_cache_days"
        ),
    )
    if scan.unknown_after_hours <= scan.stale_after_hours:
        raise ConfigError("scan.unknown_after_hours must be greater than scan.stale_after_hours")

    alerts_raw = _section(raw, "alerts")
    min_confidence = _text(
        alerts_raw.get("min_confidence", "medium"), "alerts.min_confidence"
    ).lower()
    if min_confidence not in ("low", "medium", "high", "provisional"):
        raise ConfigError("alerts.min_confidence must be low, medium, high or provisional")
    alerts = AlertConfig(
        max_per_day=_positive_int(alerts_raw.get("max_per_day", 40), "alerts.max_per_day"),
        material_price_drop_pct=_non_negative_float(
            alerts_raw.get("material_price_drop_pct", 5), "alerts.material_price_drop_pct"
        ),
        unknown_shipping_estimate_eur=_non_negative_int(
            alerts_raw.get("unknown_shipping_estimate_eur", 15),
            "alerts.unknown_shipping_estimate_eur",
        ),
        min_confidence=min_confidence,
        allow_provisional=_bool(
            alerts_raw.get("allow_provisional", False), "alerts.allow_provisional"
        ),
        auction_alert_hours=_positive_int(
            alerts_raw.get("auction_alert_hours", 48), "alerts.auction_alert_hours"
        ),
    )

    ranking_raw = _section(raw, "ranking")
    ranking = RankingConfig(
        full_discount_score_pct=_positive_float(
            ranking_raw.get("full_discount_score_pct", 50),
            "ranking.full_discount_score_pct",
        ),
        full_savings_score_eur=_positive_int(
            ranking_raw.get("full_savings_score_eur", 100), "ranking.full_savings_score_eur"
        ),
        auction_safety_margin_pct=_non_negative_float(
            ranking_raw.get("auction_safety_margin_pct", 20),
            "ranking.auction_safety_margin_pct",
        ),
        bundle_haircut_pct=_non_negative_float(
            ranking_raw.get("bundle_haircut_pct", 30), "ranking.bundle_haircut_pct"
        ),
        bundle_min_discount_pct=_non_negative_float(
            ranking_raw.get("bundle_min_discount_pct", 35),
            "ranking.bundle_min_discount_pct",
        ),
        bundle_min_savings_eur=_non_negative_int(
            ranking_raw.get("bundle_min_savings_eur", 60), "ranking.bundle_min_savings_eur"
        ),
        untested_value_haircut_pct=_non_negative_float(
            ranking_raw.get("untested_value_haircut_pct", 35),
            "ranking.untested_value_haircut_pct",
        ),
        low_seller_feedback_pct=_non_negative_float(
            ranking_raw.get("low_seller_feedback_pct", 95), "ranking.low_seller_feedback_pct"
        ),
    )
    if ranking.bundle_haircut_pct >= 100:
        raise ConfigError("ranking.bundle_haircut_pct must be below 100")

    retention_raw = _section(raw, "retention")
    retention = RetentionConfig(
        observation_days=_positive_int(
            retention_raw.get("observation_days", 180), "retention.observation_days"
        ),
        alert_days=_positive_int(retention_raw.get("alert_days", 365), "retention.alert_days"),
        api_call_days=_positive_int(
            retention_raw.get("api_call_days", 60), "retention.api_call_days"
        ),
        backup_days=_positive_int(
            retention_raw.get("backup_days", 14), "retention.backup_days"
        ),
    )

    categories = _parse_categories(raw.get("categories"))
    products = _parse_products(raw.get("products"), categories)
    bundles = _parse_bundles(raw.get("bundles"), categories)
    broad_searches = _parse_broad_searches(raw.get("broad_searches"), categories, marketplaces)
    references = _parse_references(references_raw.get("references"), products)
    exclusions = _parse_exclusions(exclusions_raw)
    variant_suffixes = _parse_terms(raw.get("variant_suffixes"), "variant_suffixes")
    if not variant_suffixes:
        raise ConfigError("variant_suffixes must not be empty")

    return AppConfig(
        settings=settings,
        marketplaces=marketplaces,
        delivery=DeliveryConfig(delivery_country, delivery_raw.get("postal_code")),
        budget=budget,
        scan=scan,
        alerts=alerts,
        ranking=ranking,
        retention=retention,
        categories=categories,
        products=products,
        bundles=bundles,
        broad_searches=broad_searches,
        references=references,
        exclusions=exclusions,
        variant_suffixes=variant_suffixes,
    )


def _load_settings(environment: Mapping[str, str], base: Path) -> Settings:
    def get(name: str, default: str = "") -> str:
        return str(environment.get(name, default) or "").strip()

    environment_name = get("EBAY_ENVIRONMENT", "production").lower()
    if environment_name not in ("production", "sandbox"):
        raise ConfigError("EBAY_ENVIRONMENT must be 'production' or 'sandbox'")

    shadow_mode = _env_bool(environment, "SHADOW_MODE", True)
    daily_budget_raw = get("DAILY_CALL_BUDGET")
    daily_budget = _positive_int(daily_budget_raw, "DAILY_CALL_BUDGET") if daily_budget_raw else None
    config_dir = Path(get("CONFIG_DIR") or base / "config")
    database_path = Path(get("DATABASE_PATH") or base / "data" / "scanner.db")
    ship_to_country = (get("SHIP_TO_COUNTRY") or "IT").upper()
    if len(ship_to_country) != 2:
        raise ConfigError("SHIP_TO_COUNTRY must be a two-letter ISO country code")

    return Settings(
        ebay_client_id=get("EBAY_CLIENT_ID"),
        ebay_client_secret=get("EBAY_CLIENT_SECRET"),
        ebay_environment=environment_name,
        telegram_bot_token=get("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=get("TELEGRAM_CHAT_ID"),
        ship_to_country=ship_to_country,
        delivery_postal_code=get("DELIVERY_POSTAL_CODE") or None,
        database_path=database_path,
        config_dir=config_dir,
        log_level=get("LOG_LEVEL", "INFO").upper(),
        timezone=get("TZ", "Europe/Rome"),
        shadow_mode=shadow_mode,
        daily_call_budget=daily_budget,
        health_max_age_seconds=_positive_int(
            get("HEALTH_MAX_AGE_SECONDS", "900"), "HEALTH_MAX_AGE_SECONDS"
        ),
        http_timeout_seconds=_positive_float(
            get("HTTP_TIMEOUT_SECONDS", "20"), "HTTP_TIMEOUT_SECONDS"
        ),
        http_max_attempts=_positive_int(get("HTTP_MAX_ATTEMPTS", "4"), "HTTP_MAX_ATTEMPTS"),
    )


def _apply_dotenv(environment: dict[str, str], path: Path) -> None:
    """Minimal .env loader for local runs. Docker env_file is the norm."""
    if not path.is_file():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if not key or key in environment:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        environment[key] = value


def _load_yaml(path: Path) -> Mapping[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, Mapping):
        raise ConfigError(f"{path} must contain a mapping at the top level")
    return loaded


def _section(raw: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = raw.get(key, {})
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigError(f"{key} must be a mapping")
    return value


def _parse_marketplaces(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError("marketplaces must be a non-empty list")
    marketplaces: list[str] = []
    for entry in value:
        if not isinstance(entry, str) or not re.fullmatch(r"EBAY_[A-Z]{2}", entry):
            raise ConfigError(f"invalid marketplace id: {entry!r}")
        if entry not in marketplaces:
            marketplaces.append(entry)
    return tuple(marketplaces)


def _parse_categories(value: object) -> Mapping[str, CategorySpec]:
    if not isinstance(value, Mapping) or not value:
        raise ConfigError("categories must be a non-empty mapping")
    categories: dict[str, CategorySpec] = {}
    for category_id, entry in value.items():
        if not isinstance(category_id, str) or not re.fullmatch(r"[a-z0-9_]+", category_id):
            raise ConfigError(f"invalid category id: {category_id!r}")
        if not isinstance(entry, Mapping):
            raise ConfigError(f"categories.{category_id} must be a mapping")
        categories[category_id] = CategorySpec(
            id=category_id,
            label=_text(entry.get("label", category_id), f"categories.{category_id}.label"),
            taxonomy_hint=(
                _text(entry["taxonomy_hint"], f"categories.{category_id}.taxonomy_hint")
                if entry.get("taxonomy_hint")
                else None
            ),
            min_discount_pct=_non_negative_float(
                entry.get("min_discount_pct", 25), f"categories.{category_id}.min_discount_pct"
            ),
            min_savings_eur=_non_negative_int(
                entry.get("min_savings_eur", 25), f"categories.{category_id}.min_savings_eur"
            ),
            full_savings_score_eur=_positive_int(
                entry.get("full_savings_score_eur", 100),
                f"categories.{category_id}.full_savings_score_eur",
            ),
            default_shipping_eur=_non_negative_int(
                entry.get("default_shipping_eur", 10),
                f"categories.{category_id}.default_shipping_eur",
            ),
        )
    return categories


def _parse_products(
    value: object, categories: Mapping[str, CategorySpec]
) -> tuple[ProductSpec, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError("products must be a non-empty list")
    products: list[ProductSpec] = []
    seen: set[str] = set()
    for entry in value:
        if not isinstance(entry, Mapping):
            raise ConfigError("each product must be a mapping")
        product_id = _text(entry.get("id"), "product.id")
        if product_id in seen:
            raise ConfigError(f"duplicate product id: {product_id}")
        seen.add(product_id)
        category = _text(entry.get("category"), f"products.{product_id}.category")
        if category not in categories:
            raise ConfigError(f"products.{product_id} uses unknown category {category}")
        priority = _text(entry.get("priority", "normal"), f"products.{product_id}.priority")
        if priority not in PRIORITIES:
            raise ConfigError(f"products.{product_id}.priority must be high, normal or low")
        condition_class = _text(
            entry.get("condition_class", "used_working"),
            f"products.{product_id}.condition_class",
        )
        if condition_class not in CONDITION_CLASSES:
            raise ConfigError(
                f"products.{product_id}.condition_class must be one of {CONDITION_CLASSES}"
            )
        aliases = _parse_terms(entry.get("aliases"), f"products.{product_id}.aliases")
        if not aliases:
            raise ConfigError(f"products.{product_id}.aliases must not be empty")
        spec = entry.get("spec") or {}
        if not isinstance(spec, Mapping):
            raise ConfigError(f"products.{product_id}.spec must be a mapping")
        queries = _parse_terms(entry.get("queries"), f"products.{product_id}.queries")
        if not queries:
            raise ConfigError(f"products.{product_id}.queries must not be empty")
        products.append(
            ProductSpec(
                id=product_id,
                name=_text(entry.get("name", product_id), f"products.{product_id}.name"),
                category=category,
                priority=priority,
                condition_class=condition_class,
                aliases=aliases,
                required_tokens=_parse_terms(
                    entry.get("required_tokens"), f"products.{product_id}.required_tokens"
                ),
                forbidden_tokens=_parse_terms(
                    entry.get("forbidden_tokens"), f"products.{product_id}.forbidden_tokens"
                ),
                spec=dict(spec),
                queries=queries,
                variant_queries=_parse_terms(
                    entry.get("variant_queries"),
                    f"products.{product_id}.variant_queries",
                ),
                auction_queries=_parse_terms(
                    entry.get("auction_queries"), f"products.{product_id}.auction_queries"
                ),
            )
        )
    return tuple(products)


def _parse_bundles(
    value: object, categories: Mapping[str, CategorySpec]
) -> tuple[BundleSpec, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigError("bundles must be a list")
    bundles: list[BundleSpec] = []
    seen: set[str] = set()
    for entry in value:
        if not isinstance(entry, Mapping):
            raise ConfigError("each bundle must be a mapping")
        bundle_id = _text(entry.get("id"), "bundle.id")
        if bundle_id in seen:
            raise ConfigError(f"duplicate bundle id: {bundle_id}")
        seen.add(bundle_id)
        category = _text(entry.get("category", "bundle"), f"bundles.{bundle_id}.category")
        if category not in categories:
            raise ConfigError(f"bundles.{bundle_id} uses unknown category {category}")
        priority = _text(entry.get("priority", "normal"), f"bundles.{bundle_id}.priority")
        if priority not in PRIORITIES:
            raise ConfigError(f"bundles.{bundle_id}.priority must be high, normal or low")
        queries = _parse_terms(entry.get("queries"), f"bundles.{bundle_id}.queries")
        if not queries:
            raise ConfigError(f"bundles.{bundle_id}.queries must not be empty")
        bundles.append(
            BundleSpec(
                id=bundle_id,
                name=_text(entry.get("name", bundle_id), f"bundles.{bundle_id}.name"),
                category=category,
                priority=priority,
                queries=queries,
                use_category_filter=_bool(
                    entry.get("use_category_filter", False),
                    f"bundles.{bundle_id}.use_category_filter",
                ),
            )
        )
    return tuple(bundles)


def _parse_broad_searches(
    value: object,
    categories: Mapping[str, CategorySpec],
    marketplaces: tuple[str, ...],
) -> tuple[BroadSearch, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigError("broad_searches must be a list")
    searches: list[BroadSearch] = []
    for index, entry in enumerate(value):
        if not isinstance(entry, Mapping):
            raise ConfigError("each broad search must be a mapping")
        category = _text(entry.get("category"), f"broad_searches[{index}].category")
        if category not in categories:
            raise ConfigError(f"broad_searches[{index}] uses unknown category {category}")
        search_marketplaces = entry.get("marketplaces") or list(marketplaces)
        if not isinstance(search_marketplaces, list):
            raise ConfigError(f"broad_searches[{index}].marketplaces must be a list")
        normalized_marketplaces: list[str] = []
        for marketplace in search_marketplaces:
            if marketplace not in marketplaces:
                raise ConfigError(
                    f"broad_searches[{index}] uses marketplace {marketplace!r} "
                    "that is not enabled"
                )
            normalized_marketplaces.append(str(marketplace))
        searches.append(
            BroadSearch(
                query=_text(entry.get("query"), f"broad_searches[{index}].query"),
                category=category,
                marketplaces=tuple(normalized_marketplaces),
                use_category_filter=_bool(
                    entry.get("use_category_filter", True),
                    f"broad_searches[{index}].use_category_filter",
                ),
            )
        )
    return tuple(searches)


def _parse_references(
    value: object, products: tuple[ProductSpec, ...]
) -> tuple[ReferencePrice, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigError("references must be a list")
    product_ids = {product.id for product in products}
    references: list[ReferencePrice] = []
    seen: set[tuple[str, str]] = set()
    for index, entry in enumerate(value):
        if not isinstance(entry, Mapping):
            raise ConfigError("each reference must be a mapping")
        product_id = _text(entry.get("product_id"), f"references[{index}].product_id")
        if product_id not in product_ids:
            raise ConfigError(f"references[{index}] refers to unknown product {product_id}")
        condition_class = _text(
            entry.get("condition_class", "used_working"),
            f"references[{index}].condition_class",
        )
        if condition_class not in CONDITION_CLASSES:
            raise ConfigError(
                f"references[{index}].condition_class must be one of {CONDITION_CLASSES}"
            )
        key = (product_id, condition_class)
        if key in seen:
            raise ConfigError(f"duplicate reference for {product_id}/{condition_class}")
        seen.add(key)
        try:
            price = parse_eur_minor(entry.get("price_eur"))
        except MoneyError as exc:
            raise ConfigError(f"references[{index}].price_eur is invalid: {exc}") from exc
        if price <= 0:
            raise ConfigError(f"references[{index}].price_eur must be positive")
        verified = _bool(entry.get("verified", False), f"references[{index}].verified")
        sample_size = _non_negative_int(
            entry.get("sample_size", 0), f"references[{index}].sample_size"
        )
        observed_at = _parse_date(
            entry.get("observed_at"), f"references[{index}].observed_at"
        )
        if verified and (observed_at is None or sample_size < 1):
            raise ConfigError(
                f"references[{index}] is verified but has no observed_at/sample_size"
            )
        references.append(
            ReferencePrice(
                product_id=product_id,
                condition_class=condition_class,
                price_eur_minor=price,
                sample_size=sample_size,
                verified=verified,
                source=_text(
                    entry.get("source", "unspecified"), f"references[{index}].source"
                ),
                observed_at=observed_at,
            )
        )
    return tuple(references)


def _parse_exclusions(raw: Mapping[str, Any]) -> ExclusionRules:
    required = (
        "defect_terms",
        "untested_terms",
        "box_only_terms",
        "accessory_terms",
        "accessory_only_terms",
        "accessory_override_phrases",
        "bundle_terms",
        "ambiguous_terms",
        "suspicious_terms",
    )
    values: dict[str, tuple[str, ...]] = {}
    for key in required:
        terms = _parse_terms(raw.get(key), f"exclusions.{key}")
        if not terms:
            raise ConfigError(f"exclusions.{key} must not be empty")
        values[key] = terms
    return ExclusionRules(**values)


def _parse_terms(value: object, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigError(f"{label} must be a list")
    terms: list[str] = []
    for entry in value:
        if not isinstance(entry, (str, int)) or str(entry).strip() == "":
            raise ConfigError(f"{label} contains an invalid entry: {entry!r}")
        text = str(entry).strip()
        if text not in terms:
            terms.append(text)
    return tuple(terms)


def _parse_date(value: object, label: str) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ConfigError(f"{label} must be an ISO date") from exc


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{label} must be a non-empty string")
    return value.strip()


def _bool(value: object, label: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off"):
            return False
    raise ConfigError(f"{label} must be a boolean")


def _env_bool(environment: Mapping[str, str], name: str, default: bool) -> bool:
    raw = environment.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    return _bool(raw, name)


def _positive_int(value: object, label: str) -> int:
    number = _int(value, label)
    if number <= 0:
        raise ConfigError(f"{label} must be positive")
    return number


def _non_negative_int(value: object, label: str) -> int:
    number = _int(value, label)
    if number < 0:
        raise ConfigError(f"{label} must not be negative")
    return number


def _int(value: object, label: str) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"{label} must be an integer")
    try:
        return int(str(value))
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{label} must be an integer") from exc


def _positive_float(value: object, label: str) -> float:
    number = _non_negative_float(value, label)
    if number <= 0:
        raise ConfigError(f"{label} must be positive")
    return number


def _non_negative_float(value: object, label: str) -> float:
    if isinstance(value, bool):
        raise ConfigError(f"{label} must be a number")
    try:
        number = float(str(value))
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{label} must be a number") from exc
    if number < 0 or number != number:
        raise ConfigError(f"{label} must not be negative")
    return number
