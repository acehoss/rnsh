def bitwise_or_if(value: int, condition: bool, orval: int):
    if not condition:
        return value
    return value | orval


def check_and(value: int, andval: int) -> bool:
    return (value & andval) > 0
