from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV = {
    "EBAY_CLIENT_ID": "test-id",
    "EBAY_CLIENT_SECRET": "test-secret",
    "TELEGRAM_BOT_TOKEN": "123:testtoken",
    "TELEGRAM_CHAT_ID": "42",
    "DATABASE_PATH": "data/test-scanner.db",
    "CONFIG_DIR": str(PROJECT_ROOT / "config"),
}


@pytest.fixture(scope="session")
def config():
    from src.config import load_config

    return load_config(ENV, base_dir=PROJECT_ROOT)


@pytest.fixture(scope="session")
def matcher(config):
    from src.products.matcher import ProductMatcher

    return ProductMatcher(config.products, config.variant_suffixes)


@pytest.fixture(scope="session")
def classifier(config):
    from src.products.exclusions import ListingClassifier

    return ListingClassifier(
        config.exclusions,
        low_seller_feedback_pct=config.ranking.low_seller_feedback_pct,
    )
