import json

import pytest
from starlette.testclient import TestClient

from supvan_e11_mcp import service
from supvan_e11_mcp.security import current_client, hash_token

TOKEN = "meals-token-xxxxxxxxxxxxxxxxx"


@pytest.fixture
def client(tmp_path, monkeypatch):
    p = tmp_path / "clients.json"
    p.write_text(json.dumps({"clients": {"meals": {"sha256": hash_token(TOKEN)}}}))
    monkeypatch.setenv("LABEL_CLIENTS_FILE", str(p))
    printed = []

    async def fake_print(self, raster, cols, density=4):
        printed.append(current_client.get())

    async def fake_status(self):
        return {"material": {"length_mm": 40}}

    async def noop(self):
        return None

    monkeypatch.setattr(service.HubBackend, "print_raster", fake_print)
    monkeypatch.setattr(service.HubBackend, "status", fake_status)
    monkeypatch.setattr(service.Hub, "start", noop)
    monkeypatch.setattr(service.Hub, "stop", noop)
    with TestClient(service.create_app()) as c:
        yield c, printed


AUTH = {"Authorization": f"Bearer {TOKEN}"}


def test_rest_print_is_attributed_and_limited(client):
    c, printed = client
    body = {"template": "freezer", "fields": {"dish": "Soup"}, "copies": 20}
    for _ in range(3):  # 60 labels: exactly the 10-minute quota
        assert c.post("/api/labels/print", json=body, headers=AUTH).json()["ok"]
    assert printed == ["meals"] * 60
    r = c.post("/api/labels/print", json=body, headers=AUTH)
    assert r.status_code == 429 and "quota" in r.json()["error"]


def test_mcp_tool_call_carries_client(client):
    c, printed = client
    call = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "print_freezer_label", "arguments": {"dish": "Stew", "copies": 2}},
    }
    r = c.post("/mcp", json=call, headers={**AUTH, "Accept": "application/json, text/event-stream"})
    assert r.status_code == 200, r.text
    assert printed == ["meals", "meals"]


def test_mcp_tool_failure_explains_why(client, monkeypatch):
    c, _ = client

    async def busy(self, raster, cols, density=4):
        raise service.PrinterError("The printer stayed busy.")

    monkeypatch.setattr(service.HubBackend, "print_raster", busy)
    call = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "print_label", "arguments": {"text": "M3x8"}},
    }
    r = c.post("/mcp", json=call, headers={**AUTH, "Accept": "application/json, text/event-stream"})
    result = r.json()["result"]
    assert result["isError"]
    assert result["content"][0]["text"].endswith("Printed 0 of 1; then: The printer stayed busy.")


def test_health_is_open_but_says_nothing_about_the_printer(client):
    c, _ = client
    assert c.get("/api/health").json() == {"ok": True}
    assert c.get("/api/radios").status_code == 401
    assert c.get("/api/radios", headers=AUTH).json() == {"ok": True, "printers_seen": []}
