from irmscher_tracker.normalizer import extract_part_numbers, normalize_part_number


def test_normalize_spaced():
    assert normalize_part_number('i 34 01 009') == '3401009'

def test_normalize_compact():
    assert normalize_part_number('i3401009') == '3401009'

def test_normalize_numeric_only():
    assert normalize_part_number('3401009') == '3401009'

def test_normalize_uppercase():
    assert normalize_part_number('I 34 01 009') == '3401009'

def test_normalize_extra_spaces():
    assert normalize_part_number('i  34  01  009') == '3401009'

def test_extract_part_numbers_from_title():
    found = extract_part_numbers("Irmscher Signum Frontspoiler i 34 01 009 and i3401010")
    assert "3401009" in found
    assert "3401010" in found
    assert len(found) == 2

def test_extract_no_match():
    found = extract_part_numbers("Just a random bumper for Opel Vectra C")
    assert found == []
