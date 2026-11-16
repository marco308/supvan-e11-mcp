import datetime as dt

from supvan_e11_mcp.templates import freezer, freezer_detail


def test_the_date_line_is_just_the_frozen_date_by_default():
    assert freezer_detail("2026-10-01") == "Frozen 1 Oct 2026"


def test_use_by_and_portions_only_when_given():
    assert freezer_detail("2026-10-01", use_by="2027-01-01", portions=2) == (
        "Frozen 1 Oct 2026 · use by Jan 27 · 2 portions"
    )


def test_frozen_defaults_to_today():
    today = dt.date.today()
    assert freezer_detail() == f"Frozen {today.day} {today:%b %Y}"


def test_renders_with_and_without_a_qr_code():
    for qr in (None, "HTTPS://MEALS.EXAMPLE.COM/L/RABC"):
        img = freezer("Chilli con carne", frozen="2026-10-01", qr=qr)
        assert img.height == 96


def test_oversized_requests_are_refused():
    import pytest

    from supvan_e11_mcp import render
    from supvan_e11_mcp.templates import render_template

    with pytest.raises(ValueError):
        render.render(render.LabelSpec(text="x", length_mm=100_000))
    with pytest.raises(ValueError):
        render.render(render.LabelSpec(text="x" * 5000))
    with pytest.raises(ValueError):
        render.render(render.LabelSpec(text="x", size=100_000))
    with pytest.raises(ValueError):
        render_template("freezer", {"dish": "Soup", "nonsense": 1}, 40)
    with pytest.raises(ValueError):
        render_template("freezer", {"dish": "Soup"}, 100_000)
