from src.products.parser import (
    condition_class_from_ebay,
    contains_phrase,
    extract_attributes,
    normalize_text,
    parse_title,
)


def test_normalize_text_lowercases_and_splits_letter_digit_boundaries():
    assert normalize_text("RTX3090Ti") == "rtx 3090 ti"
    assert normalize_text("GeForce RTX 3090!!") == "geforce rtx 3090"
    # 5800X3D splits into individual letter/digit boundaries; titles and
    # catalogue aliases normalize identically, which is what matching relies on.
    assert normalize_text("Ryzen 7 5800X3D") == "ryzen 7 5800 x 3 d"


def test_normalize_text_transliterates_german_characters():
    assert normalize_text("Kaese Grosse") == "kaese grosse"
    assert normalize_text("Größe") == "groesse"
    assert normalize_text("ÄÖÜ") == "aeoeue"


def test_contains_phrase_requires_whole_token_match():
    assert contains_phrase(normalize_text("used RTX 3090."), "rtx 3090")
    # "rtx 3090" does occur inside "rtx 3090 ti"; sub-phrase containment is a
    # matcher concern (suffix guard), contains_phrase is token-level only.
    assert contains_phrase(normalize_text("rtx 3090 ti"), "rtx 3090")
    assert not contains_phrase(normalize_text("rtx 30 90"), "rtx 3090")
    assert not contains_phrase(normalize_text("the rtx 2080s are gone"), "rtx 3090")
    assert not contains_phrase(normalize_text("price 130 90 dollars"), "rtx 3090")


def test_extract_attributes_finds_vram_and_ram_generation():
    attributes = extract_attributes(normalize_text("NVIDIA RTX 3090 24GB GDDR6X"))
    assert attributes["capacity_gb"] == 24
    assert attributes["vram_gb"] == 24

    ram = extract_attributes(normalize_text("Crucial 32GB (2x16GB) DDR4 3200 MHz"))
    assert ram["ram_gen"] == "ddr4"
    assert ram["capacity_gb"] == 32


def test_extract_attributes_handles_socket_and_form_factor():
    attributes = extract_attributes(normalize_text("ASUS B550-F ATX motherboard AM4"))
    assert attributes["form_factor"] == "atx"
    assert attributes["socket"] == "am4"


def test_condition_classification():
    assert condition_class_from_ebay("Brand New") == "new"
    assert condition_class_from_ebay("Open box") == "new"
    assert condition_class_from_ebay("Pre-owned") == "used_working"
    assert condition_class_from_ebay("For parts or not working") == "for_parts"
    assert condition_class_from_ebay(None) == "unknown"
    assert condition_class_from_ebay("Seller refurbished") == "refurbished"


def test_parse_title_returns_normalized_tokens_and_attributes():
    parsed = parse_title("NVIDIA GeForce RTX 3090")
    assert parsed.tokens[:4] == ("nvidia", "geforce", "rtx", "3090")


def test_brand_tokens_are_detected():
    parsed = parse_title("MSI GeForce RTX 3090")
    assert "msi" in parsed.brand_tokens
    assert "geforce" in parsed.brand_tokens
