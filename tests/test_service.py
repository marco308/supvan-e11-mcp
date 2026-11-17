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


MCP_HEADERS = {**AUTH, "Accept": "application/json, text/event-stream"}


def call(c, name, arguments, headers=MCP_HEADERS):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    r = c.post("/mcp", json=body, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["result"]


def preview_id(result):
    text = next(b["text"] for b in result["content"] if b["type"] == "text")
    return text.split()[1]


def test_mcp_preview_returns_picture_and_id(client):
    c, printed = client
    result = call(c, "preview_label", {"text": "M3x8"})
    assert [b["type"] for b in result["content"]] == ["image", "text"]
    assert preview_id(result) and printed == []


def test_mcp_prints_a_preview_once_with_copies(client):
    c, printed = client
    pid = preview_id(call(c, "preview_freezer_label", {"dish": "Stew"}))
    assert not call(c, "print_label", {"preview_id": pid, "copies": 2})["isError"]
    assert printed == ["meals", "meals"]
    again = call(c, "print_label", {"preview_id": pid})
    assert again["isError"] and "already been printed" in again["content"][0]["text"]
    assert printed == ["meals", "meals"]


def test_mcp_print_without_a_preview_prints_nothing(client):
    c, printed = client
    for args in ({}, {"preview_id": ""}, {"preview_id": "made-up"}):
        assert call(c, "print_label", args)["isError"]
    assert call(c, "print_label", {"text": "M3x8"})["isError"]  # the old way: refused, not printed
    assert printed == []


def test_mcp_preview_belongs_to_its_client(client, tmp_path, monkeypatch):
    c, printed = client
    pid = preview_id(call(c, "preview_label", {"text": "M3x8"}))
    other = "other-token-xxxxxxxxxxxxxxxxx"
    p = tmp_path / "clients.json"
    data = json.loads(p.read_text())
    data["clients"]["other"] = {"sha256": hash_token(other)}
    p.write_text(json.dumps(data))
    result = call(c, "print_label", {"preview_id": pid}, {**MCP_HEADERS, "Authorization": f"Bearer {other}"})
    assert result["isError"] and printed == []


def test_mcp_bad_arguments_dont_spend_the_preview(client):
    c, printed = client
    pid = preview_id(call(c, "preview_label", {"text": "M3x8"}))
    assert call(c, "print_label", {"preview_id": pid, "copies": 99})["isError"]
    assert not call(c, "print_label", {"preview_id": pid})["isError"]
    assert printed == ["meals"]


def test_mcp_tool_failure_explains_why(client, monkeypatch):
    c, _ = client

    async def busy(self, raster, cols, density=4):
        raise service.PrinterError("The printer stayed busy.")

    monkeypatch.setattr(service.HubBackend, "print_raster", busy)
    pid = preview_id(call(c, "preview_label", {"text": "M3x8"}))
    result = call(c, "print_label", {"preview_id": pid})
    assert result["isError"]
    assert result["content"][0]["text"].endswith("Printed 0 of 1; then: The printer stayed busy.")
    retry = call(c, "print_label", {"preview_id": pid})  # it may have printed: never again by id
    assert retry["isError"] and "already been printed" in retry["content"][0]["text"]


def test_health_is_open_but_says_nothing_about_the_printer(client):
    c, _ = client
    assert c.get("/api/health").json() == {"ok": True}
    assert c.get("/api/radios").status_code == 401
    assert c.get("/api/radios", headers=AUTH).json() == {"ok": True, "printers_seen": []}
