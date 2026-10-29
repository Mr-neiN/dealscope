import pytest

from src.config import ConfigError, load_config

from tests.test_matcher import ENV, PROJECT_ROOT


def test_full_config_loads_and_validates():
    config = load_config(ENV, base_dir=PROJECT_ROOT)
    assert config.settings.ebay_client_id == "test-id"
    assert config.marketplaces == ("EBAY_IT", "EBAY_DE", "EBAY_AT")
    assert all(product.aliases for product in config.products)
    assert all(product.queries for product in config.products)
    # Every broad search references an enabled marketplace.
    for broad in config.broad_searches:
        assert set(broad.marketplaces) <= set(config.marketplaces)


def test_missing_delivery_country_fails():
    bad_env = dict(ENV)
    # SHIP_TO_COUNTRY is used as the delivery country default.
    bad_env["SHIP_TO_COUNTRY"] = "ITALY"
    with pytest.raises(ConfigError):
        load_config(bad_env, base_dir=PROJECT_ROOT)


def test_invalid_marketplace_fails(tmp_path):
    with pytest.raises(ConfigError):
        load_config(dict(ENV, EBAY_ENVIRONMENT="staging"), base_dir=PROJECT_ROOT)


def test_duplicate_product_ids_rejected(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    from pathlib import Path

    for name in ("products.yaml", "references.yaml", "exclusions.yaml"):
        source = Path(PROJECT_ROOT / "config") / name
        text = source.read_text(encoding="utf-8")
        if name == "products.yaml":
            entries = [entry for entry in text.split("\n  - id:") if entry.strip()]
            # Inject a duplicate product at the end.
            duplicate = "\n  - id: rtx_3090\n    name: DUP\n    category: gpu\n    aliases:\n      - rtx 3090 dup\n    queries:\n      - DUP\n"
            text = text + duplicate
        (config_dir / name).write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(dict(ENV, CONFIG_DIR=str(config_dir)), base_dir=PROJECT_ROOT)


def test_unknown_but_wellformed_marketplace_warns_and_is_accepted(tmp_path, caplog):
    from pathlib import Path
    import logging

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    for name in ("products.yaml", "references.yaml", "exclusions.yaml"):
        source = Path(PROJECT_ROOT / "config") / name
        text = source.read_text(encoding="utf-8")
        if name == "products.yaml":
            text = text.replace("  - EBAY_AT\n", "  - EBAY_AT\n  - EBAY_ZZ\n", 1)
        (config_dir / name).write_text(text, encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="src.config"):
        config = load_config(dict(ENV, CONFIG_DIR=str(config_dir)), base_dir=PROJECT_ROOT)
    assert config.marketplaces == ("EBAY_IT", "EBAY_DE", "EBAY_AT", "EBAY_ZZ")
    assert any("EBAY_ZZ" in record.message for record in caplog.records)


def test_verified_reference_without_evidence_rejected(tmp_path):
    from pathlib import Path

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    for name in ("products.yaml", "exclusions.yaml", "references.yaml"):
        source = Path(PROJECT_ROOT / "config") / name
        (config_dir / name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    references_path = config_dir / "references.yaml"
    text = references_path.read_text(encoding="utf-8")
    broken = text.replace(
        'source: "PLACEHOLDER - replace with verified sold evidence"',
        'source: "verified manual", observed_at: 2026-10-01, verified: true, sample_size: 1,0',
        1,
    )
    references_path.write_text(broken, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(dict(ENV, CONFIG_DIR=str(config_dir)), base_dir=PROJECT_ROOT)
