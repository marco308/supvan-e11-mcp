"""Who is calling, and how often.

- Per-client tokens: clients.json maps a client name to the SHA-256 of its
  token (manage with deploy/tokens.py). The file is re-read when it changes, so
  adding or revoking a client needs no restart. Only hashes are stored.
- Real client IP: requests arrive Cloudflare -> Traefik -> nginx -> here, so
  the peer address is useless. Traefik overwrites X-Forwarded-For with the
  address it saw; when that is a Cloudflare edge we trust Cloudflare's
  CF-Connecting-IP header, otherwise we use the address itself (LAN/VPN).
  A caller that reaches the service directly, skipping the proxy, can forge
  these headers and so dodge the per-IP ban; it still needs a valid token.
- Rate limits, in memory (one replica; a restart clears them):
    bad tokens: 10 per IP in 10 min -> that IP is banned for an hour
    requests:   120 per client per minute
    labels:     60 per client per 10 minutes, 500 per day (copies count)
    /api/health: 30 per IP per minute
"""

from __future__ import annotations

import contextvars
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import time
from collections import defaultdict, deque
from pathlib import Path

log = logging.getLogger("security")

# https://www.cloudflare.com/ips/ (fetched 2026-10-01)
CLOUDFLARE_NETS = [
    ipaddress.ip_network(n)
    for n in (
        "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22",
        "141.101.64.0/18", "108.162.192.0/18", "190.93.240.0/20", "188.114.96.0/20",
        "197.234.240.0/22", "198.41.128.0/17", "162.158.0.0/15", "104.16.0.0/13",
        "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22",
        "2400:cb00::/32", "2606:4700::/32", "2803:f800::/32", "2405:b500::/32",
        "2405:8100::/32", "2a06:98c0::/29", "2c0f:f248::/32",
    )
]

# The client making the current request, for per-client print limits and logs.
current_client: contextvars.ContextVar[str] = contextvars.ContextVar("current_client", default="-")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def is_cloudflare(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in CLOUDFLARE_NETS)


def client_ip(headers: dict[bytes, bytes], peer: str | None) -> str:
    """Best guess at the real caller (see module docstring)."""
    xff = headers.get(b"x-forwarded-for", b"").decode("latin-1")
    hop = xff.split(",")[0].strip() if xff else (peer or "")
    if is_cloudflare(hop) and (cf := headers.get(b"cf-connecting-ip")):
        return cf.decode("latin-1").strip()
    return hop or "unknown"


class ClientStore:
    """clients.json: {"clients": {"<name>": {"sha256": "<hex>", "disabled": false}}}"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._mtime = 0.0
        self._by_hash: dict[str, str] = {}
        self._reload()

    def _reload(self) -> None:
        try:
            mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            self._by_hash = {}
            return
        if mtime == self._mtime:
            return
        data = json.loads(self.path.read_text())
        self._by_hash = {
            c["sha256"]: name for name, c in data.get("clients", {}).items() if not c.get("disabled")
        }
        self._mtime = mtime
        log.info("loaded %d client token(s): %s", len(self._by_hash), ", ".join(sorted(self._by_hash.values())))

    def __len__(self) -> int:
        self._reload()
        return len(self._by_hash)

    def lookup(self, token: str) -> str | None:
        self._reload()
        given = hash_token(token)
        for h, name in self._by_hash.items():
            if hmac.compare_digest(h, given):
                return name
        return None


class Window:
    """Sliding-window counter: at most `limit` hits per `seconds` per key."""

    def __init__(self, limit: int, seconds: float) -> None:
        self.limit, self.seconds = limit, seconds
        self.hits: dict[str, deque[float]] = defaultdict(deque)

    MAX_KEYS = 10_000

    def _trim(self, key: str, now: float) -> deque[float]:
        if len(self.hits) > self.MAX_KEYS:
            # Forget keys with nothing recent, so a flood of distinct IPs can't
            # grow this without bound.
            cutoff = now - self.seconds
            for k in [k for k, v in self.hits.items() if not v or v[-1] <= cutoff]:
                del self.hits[k]
        q = self.hits[key]
        while q and q[0] <= now - self.seconds:
            q.popleft()
        return q

    def allow(self, key: str, n: int = 1) -> float:
        """Record n hits and return 0, or return seconds to wait if over the limit."""
        now = time.monotonic()
        q = self._trim(key, now)
        if len(q) + n > self.limit:
            return max(1.0, q[0] + self.seconds - now) if q else self.seconds
        q.extend([now] * n)
        return 0.0

    def count(self, key: str) -> int:
        return len(self._trim(key, time.monotonic()))


class Limiter:
    def __init__(self) -> None:
        self.auth_failures = Window(10, 600)
        self.banned: dict[str, float] = {}
        self.ban_seconds = 3600
        self.requests = Window(120, 60)
        self.health = Window(30, 60)
        self.labels_short = Window(60, 600)
        self.labels_day = Window(500, 86400)

    def ban_remaining(self, ip: str) -> float:
        until = self.banned.get(ip, 0)
        left = until - time.monotonic()
        if left <= 0:
            self.banned.pop(ip, None)
            return 0.0
        return left

    def auth_failed(self, ip: str) -> None:
        if not self.auth_failures.allow(ip):  # 0 = still under the limit
            return
        self.banned[ip] = time.monotonic() + self.ban_seconds
        self.auth_failures.hits.pop(ip, None)
        log.warning("banned %s for %ds after repeated bad tokens", ip, self.ban_seconds)

    def labels(self, client: str, n: int) -> float:
        """Check the per-client label quota for n labels; 0 if allowed."""
        wait = self.labels_short.allow(client, n)
        if wait:
            return wait
        wait = self.labels_day.allow(client, n)
        if wait:
            # Give back the short-window hits we just took.
            q = self.labels_short.hits[client]
            for _ in range(n):
                q.pop()
        return wait


class RateLimited(Exception):
    def __init__(self, message: str, retry_after: float) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def default_clients_path() -> Path:
    return Path(os.environ.get("LABEL_CLIENTS_FILE", "/run/secrets/clients.json"))
