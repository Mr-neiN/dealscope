from src.money import MoneyError, format_eur, parse_amount_minor, parse_optional_minor

import pytest


def test_parse_amount_minor_rounds_half_up():
    assert parse_amount_minor("10.005") == 1001
    assert parse_amount_minor("9.99") == 999
    assert parse_amount_minor(12.5) == 1250


def test_parse_amount_minor_rejects_bad_values():
    with pytest.raises(MoneyError):
        parse_amount_minor(None)
    with pytest.raises(MoneyError):
        parse_amount_minor(True)
    with pytest.raises(MoneyError):
        parse_amount_minor("abc")
    with pytest.raises(MoneyError):
        parse_amount_minor("nan")


def test_parse_optional_minor_handles_missing():
    assert parse_optional_minor(None) is None
    assert parse_optional_minor("") is None
    assert parse_optional_minor("3.2") == 320


def test_format_eur_uses_italian_style_separators():
    assert format_eur(1250) == "€12,50"
    assert format_eur(123456) == "€1.234,56"
    assert format_eur(-150) == "€-1,50"
    assert format_eur(None) == "unknown"
