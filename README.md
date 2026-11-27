<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/logo/labels-mark-cream.svg">
    <img src="assets/logo/labels-mark-ink.svg" width="112" height="112" alt="supvan-e11-mcp mark: a freezer label with a QR code, drawn as a half section">
  </picture>
</p>

# supvan-e11-mcp

[![CI](https://github.com/marco308/supvan-e11-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/marco308/supvan-e11-mcp/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/marco308/supvan-e11-mcp/graph/badge.svg)](https://codecov.io/gh/marco308/supvan-e11-mcp)

Print labels on a SUPVAN E11 Bluetooth label maker without the phone app:
from Claude on a Mac (local MCP server), from the command line, or from a
small label service on a home server (REST API + remote MCP) that other apps
and agents can call.

Not affiliated with SUPVAN. It works with the one E11 I own; the protocol code
covers the E-series (E10/E11/E12/E16) but only the E11 is tested.

## Label service

A container that runs next to a Bluetooth adapter and prints for any client
with a token. It reaches the printer through the host's own adapter plus any
ESPHome Bluetooth proxies listed in `SUPVAN_PROXIES`, using whichever hears
the printer best. Every endpoint except `/api/health` needs
`Authorization: Bearer <client token>`.

### Security

The service is built to be reachable from the internet behind a reverse proxy
(mine is Cloudflare → Traefik → nginx → service), so:

- **One token per client app** (`claude-code`, `meals-app`, …), managed on the
  host with `deploy/tokens.py add|list|revoke <name>`. Only SHA-256 hashes
  are stored (`deploy/secrets/clients.json`, git-ignored); changes apply
  without a restart. Every request is logged with the client name and IP.
- **Real client IPs**: behind Cloudflare the proxy only sees Cloudflare edges,
  so the service uses the `CF-Connecting-IP` header, trusted only when the hop
  the proxy saw is a published Cloudflare range (`security.py`).
- **Rate limits** (in memory): 10 bad tokens from one IP in 10 min bans that IP
  for an hour; 120 requests per client per minute; 60 labels per client per
  10 minutes and 500 per day (copies count); `/api/health` 30 per IP per
  minute. Over a limit returns 429 with `Retry-After`.
- **Size limits**: labels are 8–300 mm long with at most 300 characters of
  text, so a request can't make the service draw a huge image.
- **What it doesn't do**: a caller who reaches the container's port directly,
  skipping the proxy, can forge the forwarding headers and dodge the per-IP
  ban (it still needs a valid token). Keep that port on a trusted network.

```bash
# Freezer label: dish, frozen (default today), qr; use_by and portions only if given
curl -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"template":"freezer","fields":{"dish":"Chilli con carne","portions":2}}' \
  https://labels.example.com/api/labels/print

# Any text label; /api/labels/preview takes the same JSON and returns a PNG
curl -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"text":"Spices\nTop shelf","border":true}' https://labels.example.com/api/labels/preview -o p.png
```

| Endpoint | |
|---|---|
| `GET /api/health` | no auth; liveness only |
| `GET /api/radios` | which radios currently see a printer |
| `GET /api/status` | model, tape, labels left, problems |
| `POST /api/labels/preview` | label JSON → PNG |
| `POST /api/labels/print` | label JSON (+ `copies`, `density`) → `{"ok": true}` |
| `POST /api/feed`, `POST /api/release` | feed a label; drop Bluetooth |
| `/mcp` | MCP over streamable HTTP, same tools as below |

Errors come back as `{"ok": false, "error": "..."}`: 400 for a bad request,
503 when the printer can't be reached (usually switched off; it powers itself
down after a while and can't be woken over Bluetooth).

### Deploying

CI builds `ghcr.io/marco308/supvan-e11-mcp:<sha>` on every push to `main`. On
the Linux machine with the Bluetooth adapter:

```bash
git clone https://github.com/marco308/supvan-e11-mcp && cd supvan-e11-mcp
cp deploy/.env.example deploy/.env    # set LABEL_HOST, and SUPVAN_PROXIES if any
deploy/tokens.py add my-first-client  # prints the token once
deploy/deploy.sh
```

`deploy/` is how I run it, on Docker Swarm with Traefik; treat it as an
example. The service itself is a Compose container, not a Swarm service,
because reaching BlueZ over D-Bus needs `apparmor:unconfined`, which Swarm
can't set. Traefik only routes to Swarm services, so `deploy/stack.yml` runs a
small nginx front door for `LABEL_HOST`. Without Swarm, `docker compose -f
deploy/compose.yml up -d` alone gives you the service on port 8799; put your
own HTTPS proxy in front of it.

**Adding a room:** flash an ESP32 with ESPHome's Bluetooth proxy firmware
(`bluetooth_proxy: active: true`), then add `host|api_encryption_key` to
`SUPVAN_PROXIES` in `deploy/.env`, inside double quotes (bash reads a bare `|`
as a pipe), and re-run `deploy/deploy.sh`.

## Mac setup (local MCP)

```
Claude ──MCP──▶ server.py ──Unix socket──▶ Label Printer Bridge.app ──BLE──▶ E11
                (draws labels)             (daemon.py, owns Bluetooth)
```

macOS kills any process that uses Bluetooth unless the app responsible for it
declares `NSBluetoothAlwaysUsageDescription`. Claude Code and plain `python`
don't, so the Bluetooth side runs in a tiny app bundle of its own. The MCP
server and CLI start it with `open` when needed. It drops the printer after
two idle minutes so the phone app can connect again.

```bash
uv sync
scripts/make_app.sh            # builds ~/Applications/Label Printer Bridge.app
claude mcp add --scope user supvan-e11 -- uv --directory "$PWD" run supvan-e11-mcp
```

Rebuild the app only if you move the repo (it points at this checkout's `.venv`).
The bridge logs to `~/Library/Logs/label-printer-bridge.log`. Set
`SUPVAN_DEBUG=1` when the bridge first starts to log every frame.

## MCP tools

| Tool | What it does |
|---|---|
| `printer_status` | Model, loaded tape (width, label length, labels left), problems |
| `preview_label` | Picture of a text label and a `preview_id`, nothing printed |
| `preview_freezer_label` | The same for the freezer template |
| `print_label` | Print a preview by its `preview_id` (`copies`, `density`) |
| `feed_tape` | Feed one blank label |
| `release_printer` | Drop Bluetooth so the phone app can connect |

Labels take `text` (`\n` for a second line), `length_mm` (defaults to the loaded
label), `font` (sans-bold, sans, condensed-bold, condensed, serif, mono; bundled DejaVu), `size`, `align`, `qr`,
`border` and `invert`.

**Printing needs a preview.** `print_label` takes the `preview_id` a preview returned and
prints exactly that picture, so an agent can't print a label nobody was shown. Each preview
prints once (`copies` sets how many) and lasts 10 minutes, and only the client that made it
can print it, so a retry or a second turn after a dropped link can't print the same label
twice. This is MCP only: the REST API's `POST /api/labels/print` still prints in one call,
because an app's print button is already a person choosing one label.

## CLI

```bash
uv run supvan-e11 status
uv run supvan-e11 print 'Flour\nopened 1 Oct' --qr https://example.com --preview /tmp/p.png --dry-run
uv run supvan-e11 print "Spices" --border
```

## Hardware facts (my unit)

- Advertises under a name starting `T0` followed by its serial; reports model
  `G15`, firmware 1.
- Bluetooth service `fee7`, one characteristic `fec1` for both notify and write
  (write without response), written in 128-byte chunks.
- Print head 96 dots (12 mm) at 8 dots/mm. Tape: 12 × 40 mm die-cut, 3 mm gap.
- Verified 2026-10-01 with a bordered test label: image row 0 / LSB-first is
  correct (not mirrored or flipped), the full 96 × 320 dot border prints, and
  density 4 is crisp. Placement along the label is within ~0.5 mm of centre.

## Credits

The protocol comes from [heeen/supvan-cups](https://github.com/heeen/supvan-cups)
(MIT), via the E11 work in
[efcroasdell/supvan-cups](https://github.com/efcroasdell/supvan-cups) and
[Curetix/label-printer](https://github.com/Curetix/label-printer).
`protocol.py` mirrors the Rust function names.

## Licence

MIT, see [LICENSE](LICENSE). It carries Florian Hänel's copyright notice too,
because `protocol.py` and `printer.py` are ported from his MIT-licensed
supvan-cups. The bundled DejaVu fonts have their own licence
(`src/supvan_e11_mcp/fonts/DejaVu-LICENSE`).
