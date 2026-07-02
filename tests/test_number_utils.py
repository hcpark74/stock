from src.utils.number import to_float, to_int


def test_to_float_accepts_commas_and_empty_values():
    assert to_float("1,234.5") == 1234.5
    assert to_float(None) == 0.0
    assert to_float("bad") == 0.0


def test_to_int_uses_shared_float_parser():
    assert to_int("1,234.9") == 1234
    assert to_int("bad") == 0
