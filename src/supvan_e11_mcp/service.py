"""Homelab label service: REST API + remote MCP, printing over Bluetooth via
the host's adapter and any ESPHome proxies (hub.py).

Endpoints (all but /api/health need `Authorization: Bearer <client token>`;
clients and limits are in security.py, tokens are managed with deploy/tokens.py):
  GET  /api/health                  liveness only; says nothing about the printer
  GET  /api/radios                  which radios currently see a printer
  GET  /api/status                  model, tape, problems
  POST /api/labels/preview          label JSON -> PNG
  POST /api/labels/print            label JSON -> {"ok": true, "message": ...}
  POST /api/feed, /api/release
  /mcp                              MCP over streamable HTTP (same tools as the Mac server)

Label JSON is either a template:
  {"template": "freezer", "fields": {"dish": "Chilli", "portions": 2}, "copies": 1}
or free text, with the same fields as the preview_label tool:
  {"text": "Spices\\nTop shelf", "border": true, "copies": 1, "density": 4}
"""

from __future__ import annotations

import contextlib
import logging
import os

import uvicorn
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

from .bridge import Bridge
from .hub import Hub
from .printer import PrinterError
from .security import (
    ClientStore,
    Limiter,
    RateLimited,
    client_ip,
    current_client,
    default_clients_path,
)
from .tools import Labels, build_server, png

log = logging.getLogger("service")

TEXT_FIELDS = {"text", "length_mm", "font", "size", "align", "qr", "border", "invert"}


class HubBackend:
    def __init__(self, bridge: Bridge) -> None:
        self.bridge = bridge

    async def status(self) -> dict:
        return await self.bridge.status()

    async def feed(self) -> None:
        await self.bridge.feed()

    async def print_raster(self, raster: bytes, cols: int, density: int = 4) -> None:
        await self.bridge.print_raster(raster, cols, density)

    async def release(self) -> None:
        await self.bridge.run("disconnect")


class Guard:
    """Pure ASGI middleware (keeps MCP's streaming responses intact): bans,
    per-client tokens and request rate limits, plus one log line per call."""

    def __init__(self, app, clients: ClientStore, limiter: Limiter) -> None:
        self.app = app
        self.clients = clients
        self.limiter = limiter

    @staticmethod
    async def _deny(scope, receive, send, status: int, error: str, retry_after: float = 0) -> None:
        headers = {"Retry-After": str(int(retry_after) + 1)} if retry_after else None
        await JSONResponse({"ok": False, "error": error}, status_code=status, headers=headers)(scope, receive, send)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope["headers"])
        ip = client_ip(headers, (scope.get("client") or (None,))[0])
        path = scope["path"]

        if path == "/api/health":
            if wait := self.limiter.health.allow(ip):
                return await self._deny(scope, receive, send, 429, "slow down", wait)
            return await self.app(scope, receive, send)

        if left := self.limiter.ban_remaining(ip):
            return await self._deny(scope, receive, send, 429, "too many bad tokens; try later", left)

        auth = headers.get(b"authorization", b"").decode("latin-1")
        client = self.clients.lookup(auth[7:]) if auth.startswith("Bearer ") else None
        if client is None:
            self.limiter.auth_failed(ip)
            log.warning("unauthorised %s %s from %s", scope["method"], path, ip)
            return await self._deny(scope, receive, send, 401, "unauthorised")

        if wait := self.limiter.requests.allow(client):
            return await self._deny(scope, receive, send, 429, "too many requests", wait)

        log.info("%s %s %s from %s", client, scope["method"], path, ip)
        token = current_client.set(client)
        try:
            await self.app(scope, receive, send)
        finally:
            current_client.reset(token)


def create_app() -> Starlette:
    clients = ClientStore(default_clients_path())
    if not len(clients):
        log.warning("no client tokens in %s; every request will be refused", clients.path)
    limiter = Limiter()
    allowed_hosts = [h.strip() for h in os.environ.get("ALLOWED_HOSTS", "").split(",") if h.strip()]

    hub = Hub()
    bridge = Bridge(locate=hub.locate)
    labels = Labels(HubBackend(bridge))

    def label_quota(n: int) -> None:
        client = current_client.get()
        if wait := limiter.labels(client, n):
            raise RateLimited(f"label quota reached for {client}; try again in {int(wait) + 1} s", wait)

    labels.quota = label_quota
    mcp = build_server(labels, who=current_client.get)
    mcp_app = mcp.streamable_http_app(
        stateless_http=True,
        json_response=True,
        host="0.0.0.0",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=bool(allowed_hosts),
            allowed_hosts=allowed_hosts,
            allowed_origins=[f"https://{h}" for h in allowed_hosts],
        ),
    )

    async def image_from(body: dict):
        if "template" in body:
            return await labels.template_image(body["template"], body.get("fields") or {}, body.get("length_mm"))
        if "text" in body:
            return await labels.text_image(**{k: v for k, v in body.items() if k in TEXT_FIELDS})
        raise ValueError('send either "template" (+ "fields") or "text"')

    def error(e: Exception) -> JSONResponse:
        if isinstance(e, RateLimited):
            return JSONResponse(
                {"ok": False, "error": str(e)}, status_code=429, headers={"Retry-After": str(int(e.retry_after) + 1)}
            )
        if isinstance(e, (ValueError, TypeError, KeyError)):
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        if isinstance(e, (PrinterError, RuntimeError)):
            return JSONResponse({"ok": False, "error": str(e)}, status_code=503)
        log.exception("unexpected error")
        return JSONResponse({"ok": False, "error": "internal error"}, status_code=500)

    async def health(request: Request):
        return JSONResponse({"ok": True})

    async def radios(request: Request):
        return JSONResponse({"ok": True, "printers_seen": hub.radios()})

    async def status(request: Request):
        try:
            return JSONResponse({"ok": True, **(await labels.status())})
        except Exception as e:  # noqa: BLE001
            return error(e)

    async def preview(request: Request):
        try:
            return Response(png(await image_from(await request.json())), media_type="image/png")
        except Exception as e:  # noqa: BLE001
            return error(e)

    async def print_(request: Request):
        try:
            body = await request.json()
            img = await image_from(body)
            msg = await labels.print_image(img, int(body.get("copies", 1)), int(body.get("density", 4)))
            return JSONResponse({"ok": True, "message": msg})
        except Exception as e:  # noqa: BLE001
            return error(e)

    async def feed(request: Request):
        try:
            await bridge.feed()
            return JSONResponse({"ok": True})
        except Exception as e:  # noqa: BLE001
            return error(e)

    async def release(request: Request):
        await bridge.run("disconnect")
        return JSONResponse({"ok": True})

    @contextlib.asynccontextmanager
    async def lifespan(app):
        await hub.start()
        async with mcp.session_manager.run():
            yield
        await bridge.close()
        await hub.stop()

    return Starlette(
        routes=[
            Route("/api/health", health),
            Route("/api/radios", radios),
            Route("/api/status", status),
            Route("/api/labels/preview", preview, methods=["POST"]),
            Route("/api/labels/print", print_, methods=["POST"]),
            Route("/api/feed", feed, methods=["POST"]),
            Route("/api/release", release, methods=["POST"]),
            Mount("/", mcp_app),
        ],
        middleware=[Middleware(Guard, clients=clients, limiter=limiter)],
        lifespan=lifespan,
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if os.environ.get("SUPVAN_DEBUG"):
        logging.getLogger("supvan_e11_mcp").setLevel(logging.DEBUG)
    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), log_level="info")


if __name__ == "__main__":
    main()
