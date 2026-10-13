import pytest

from ragdemo.parse.tiered.types import PageRoute, route_from_dict, route_to_dict


def test_a_route_round_trips_through_json_shaped_dicts() -> None:
    r = PageRoute(
        page=3,
        tier="L3",
        engine="textin",
        text_layer=True,
        reasons=("L0 closure_failed：数字闭合失败：['9.9']", "L1 关闭"),
        elapsed_ms=1200,
        local_engine_calls=1,
        llm_tokens=(300, 80),
        textin_billed=True,
    )
    assert route_from_dict(route_to_dict(r)) == r


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [({"page": 0}, "页码"), ({"tier": "L9"}, "层级"), ({"elapsed_ms": -1}, "耗时")],
)
def test_invalid_routes_are_rejected(kwargs: dict[str, object], message: str) -> None:
    base: dict[str, object] = {"page": 1, "tier": "L0", "engine": "x", "text_layer": True}
    with pytest.raises(ValueError, match=message):
        PageRoute(**{**base, **kwargs})  # type: ignore[arg-type]
