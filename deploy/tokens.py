#!/usr/bin/env python3
"""Manage label-service client tokens (run on the service host, stdlib only).

  deploy/tokens.py add meals-app         new token, printed once
  deploy/tokens.py list
  deploy/tokens.py revoke meals-app
  deploy/tokens.py import NAME FILE      hash an existing token from FILE

Only SHA-256 hashes are stored, in deploy/secrets/clients.json (git-ignored,
mounted read-only into the container). The service picks up changes within a
request; no restart needed.
"""

import hashlib
import json
import os
import secrets
import sys
import tempfile
from datetime import date
from pathlib import Path

PATH = Path(__file__).resolve().parent / "secrets" / "clients.json"


def load() -> dict:
    return json.loads(PATH.read_text()) if PATH.exists() else {"clients": {}}


def save(data: dict) -> None:
    PATH.parent.mkdir(mode=0o700, exist_ok=True)
    # Atomic replace; the directory (not the file) is mounted, so the container sees it.
    fd, tmp = tempfile.mkstemp(dir=PATH.parent)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(tmp, 0o644)  # container reads it as another uid; it holds hashes only
    os.replace(tmp, PATH)


def put(name: str, token: str) -> None:
    data = load()
    data["clients"][name] = {"sha256": hashlib.sha256(token.encode()).hexdigest(), "created": date.today().isoformat()}
    save(data)


def main() -> None:
    args = sys.argv[1:]
    if args[:1] == ["add"] and len(args) == 2:
        if args[1] in load()["clients"]:
            sys.exit(f"{args[1]} already exists; revoke it first to rotate")
        token = secrets.token_urlsafe(32)
        put(args[1], token)
        print(f"Token for {args[1]} (shown once, store it in that app's secrets):\n{token}")
    elif args[:1] == ["import"] and len(args) == 3:
        put(args[1], Path(args[2]).read_text().strip())
        print(f"Imported token for {args[1]}")
    elif args[:1] == ["revoke"] and len(args) == 2:
        data = load()
        if data["clients"].pop(args[1], None) is None:
            sys.exit(f"no client {args[1]}")
        save(data)
        print(f"Revoked {args[1]}")
    elif args == ["list"]:
        for name, c in sorted(load()["clients"].items()):
            print(f"{name:20} created {c.get('created', '?')}{'  (disabled)' if c.get('disabled') else ''}")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
