import json

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from supvan_e11_mcp.security import ClientStore, Limiter, client_ip, current_client, hash_token
from supvan_e11_mcp.service import Guard

CF_EDGE = "162.158.217.125"


def test_client_ip_trusts_cloudflare_header_only_from_cloudflare():
    h = {b"x-forwarded-for": f"{CF_EDGE}, 10.0.2.5".encode(), b"cf-connecting-ip": b"81.2.3.4"}
    assert client_ip(h, "10.0.2.9") == "81.2.3.4"
    # A LAN caller can't spoof CF-Connecting-IP: its own address is used.
    h = {b"x-forwarded-for": b"192.168.1.20", b"cf-connecting-ip": b"1.1.1.1"}
    assert client_ip(h, "10.0.2.9") == "192.168.1.20"
    assert client_ip({}, "100.64.0.7") == "100.64.0.7"


def test_ban_after_ten_bad_tokens():
    lim = Limiter()
    for _ in range(10):
        lim.auth_failed("1.2.3.4")
    assert not lim.ban_remaining("1.2.3.4")
    lim.auth_failed("1.2.3.4")
    assert lim.ban_remaining("1.2.3.4") > 3500
    assert not lim.ban_remaining("5.6.7.8")


def test_label_quota_counts_copies_and_refunds():
    lim = Limiter()
    assert lim.labels("meals", 50) == 0
    assert lim.labels("meals", 20) > 0  # would make 70 in 10 min
    assert lim.labels("meals", 10) == 0  # exactly 60
    assert lim.labels("claude", 60) == 0  # per client


def _store(tmp_path, **tokens):
    p = tmp_path / "clients.json"
    p.write_text(json.dumps({"clients": {n: {"sha256": hash_token(t)} for n, t in tokens.items()}}))
    return p


def test_store_lookup_and_reload(tmp_path):
    p = _store(tmp_path, meals="tok-meals-xxxxxxxxxxxxxxxx")
    store = ClientStore(p)
    assert store.lookup("tok-meals-xxxxxxxxxxxxxxxx") == "meals"
    assert store.lookup("nope") is None
    import os, time
    _store(tmp_path, claude="tok-claude-xxxxxxxxxxxxxxx")
    os.utime(p, (time.time() + 5, time.time() + 5))
    assert store.lookup("tok-meals-xxxxxxxxxxxxxxxx") is None
    assert store.lookup("tok-claude-xxxxxxxxxxxxxxx") == "claude"


def _app(tmp_path):
    async def who(request):
        return PlainTextResponse(current_client.get())

    async def health(request):
        return PlainTextResponse("ok")

    lim = Limiter()
    app = Starlette(
        routes=[Route("/api/status", who), Route("/api/health", health)],
        middleware=[Middleware(Guard, clients=ClientStore(_store(tmp_path, meals="good-token-xxxxxxxxxxxx")), limiter=lim)],
    )
    return TestClient(app), lim


def test_guard_end_to_end(tmp_path):
    c, lim = _app(tmp_path)
    assert c.get("/api/health").status_code == 200
    assert c.get("/api/status").status_code == 401
    r = c.get("/api/status", headers={"Authorization": "Bearer good-token-xxxxxxxxxxxx"})
    assert r.status_code == 200 and r.text == "meals"
    # Ten bad tokens from one Cloudflare-fronted caller, then they're banned,
    # even with a good token, while another caller is unaffected.
    bad = {"Authorization": "Bearer wrong", "X-Forwarded-For": CF_EDGE, "CF-Connecting-IP": "81.2.3.4"}
    for _ in range(11):
        c.get("/api/status", headers=bad)
    r = c.get("/api/status", headers={**bad, "Authorization": "Bearer good-token-xxxxxxxxxxxx"})
    assert r.status_code == 429 and "Retry-After" in r.headers
    r = c.get("/api/status", headers={"Authorization": "Bearer good-token-xxxxxxxxxxxx"})
    assert r.status_code == 200
