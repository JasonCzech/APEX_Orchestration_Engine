import pytest

from apex.adapters.options import coerce_bool


@pytest.mark.parametrize(
    "value,default,expected",
    [
        (None, True, True),
        (None, False, False),
        (True, False, True),
        (False, True, False),
        ("false", True, False),
        ("False", True, False),
        ("0", True, False),
        ("no", True, False),
        ("off", True, False),
        ("true", False, True),
        ("1", False, True),
        ("yes", False, True),
        (0, True, False),
        (1, False, True),
        ("garbage", True, True),
        ("garbage", False, False),
    ],
)
def test_coerce_bool(value: object, default: bool, expected: bool) -> None:
    assert coerce_bool(value, default=default) is expected
