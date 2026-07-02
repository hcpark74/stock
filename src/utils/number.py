def to_float(value: object) -> float:
    try:
        return float(str(value or "0").replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def to_int(value: object) -> int:
    return int(to_float(value))
