"""Runtime shape checks for assertions over immutable contract JSON."""

from tychora.contracts.base import FrozenJsonMap, FrozenJsonValue


def json_object(value: object, *keys: str) -> FrozenJsonMap:
    """Select an expected object, checking every nested field's shape."""
    for key in keys:
        assert isinstance(value, FrozenJsonMap)
        value = value[key]
    assert isinstance(value, FrozenJsonMap)
    return value


def json_array(value: object) -> tuple[FrozenJsonValue, ...]:
    assert isinstance(value, tuple)
    return value


def json_integer(value: object) -> int:
    assert isinstance(value, int) and not isinstance(value, bool)
    return value


def json_string(value: object) -> str:
    assert isinstance(value, str)
    return value
