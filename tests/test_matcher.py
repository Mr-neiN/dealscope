import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV = {
    "EBAY_CLIENT_ID": "test-id",
    "EBAY_CLIENT_SECRET": "test-secret",
    "TELEGRAM_BOT_TOKEN": "123:testtoken",
    "TELEGRAM_CHAT_ID": "42",
    "DATABASE_PATH": "data/test-scanner.db",
    "CONFIG_DIR": str(PROJECT_ROOT / "config"),
}


@pytest.fixture(scope="module")
def config():
    from src.config import load_config

    return load_config(ENV, base_dir=PROJECT_ROOT)


@pytest.fixture(scope="module")
def matcher(config):
    from src.products.matcher import ProductMatcher

    return ProductMatcher(config.products, config.variant_suffixes)


def test_exact_model_matches_with_high_confidence(matcher):
    result = matcher.match_title("NVIDIA GeForce RTX 3090 24GB GDDR6X")
    assert result.primary is not None
    assert result.primary.product.id == "rtx_3090"
    assert not result.ambiguous
    assert result.overall_confidence >= 0.6


def test_variant_suffix_guard_does_not_match_3090_for_3090_ti(matcher):
    result = matcher.match_title("GeForce RTX 3090 Ti")
    assert result.primary is None or result.primary.product.id != "rtx_3090"


def test_variant_suffix_guard_does_not_match_3080_for_3080_ti(matcher):
    result = matcher.match_title("ZOTAC RTX 3080 Ti 12GB")
    assert result.primary is None or result.primary.product.id != "rtx_3080"


def test_forbidden_phrase_rejects_wrong_variant(matcher):
    result = matcher.match_title("RTX 4080 SUPER 16GB")
    assert result.primary is None or result.primary.product.id != "rtx_4080"


def test_spec_mismatch_rejects_wrong_vram(matcher):
    result = matcher.match_title("GeForce RTX 4090 16GB GDDR6X")
    assert result.primary is None or result.primary.product.id != "rtx_4090"


def test_no_match_for_unrelated_titles(matcher):
    result = matcher.match_title("Logitech G502 gaming mouse")
    assert result.primary is None
    assert result.components == ()


def test_italian_and_german_variant_queries_match(matcher):
    italian = matcher.match_title("Scheda video RTX 3090 usata perfetta")
    assert italian.primary is not None
    assert italian.primary.product.id == "rtx_3090"

    german = matcher.match_title("Grafikkarte RTX 3080 10GB + Rechnung")
    assert german.primary is not None
    assert german.primary.product.id == "rtx_3080"


def test_suffix_variant_never_downgrades_to_base_model(matcher):
    result = matcher.match_title("ZOTAC GeForce RTX 3090 Ti 24GB Gaming")
    assert result.primary is None or result.primary.product.id == "rtx_3090_ti"
