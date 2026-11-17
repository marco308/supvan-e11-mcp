import pytest
from PIL import Image

from supvan_e11_mcp.tools import Previews


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_preview_prints_once_for_its_client_until_it_expires():
    clock = Clock()
    p = Previews(ttl=600, clock=clock)
    img = Image.new("1", (320, 96))
    pid = p.add(img, "claude")
    with pytest.raises(ValueError, match="No preview"):
        p.take(pid, "meals")
    assert p.take(pid, "claude") is img
    with pytest.raises(ValueError, match="already been printed"):
        p.take(pid, "claude")
    old = p.add(img, "claude")
    clock.t = 600
    with pytest.raises(ValueError, match="10 minutes old"):
        p.take(old, "claude")


def test_previews_are_pruned():
    clock = Clock()
    p = Previews(ttl=600, clock=clock, keep=3)
    ids = [p.add(Image.new("1", (8, 8)), "c") for _ in range(5)]
    with pytest.raises(ValueError, match="No preview"):
        p.take(ids[0], "c")
    assert p.take(ids[-1], "c")
