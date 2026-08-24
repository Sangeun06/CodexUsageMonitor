from __future__ import annotations

import sys
from pathlib import Path


def resolve_account_key(root: Path, token_file: Path, explicit: str | None = None) -> str:
    if explicit:
        key = explicit.strip().lower()
        if len(key) != 12 or any(character not in "0123456789abcdef" for character in key):
            raise SystemExit("--account-key must be exactly 12 hexadecimal characters")
        return key
    return registered_account_key(root, token_file)


def registered_account_key(root: Path, token_file: Path, auth_file: Path | None = None) -> str:
    sys.path.insert(0, str(root))
    from collector import read_account

    token = token_file.read_text().strip()
    account = read_account(auth_file or (Path.home() / ".codex" / "auth.json"), token)
    key = account.get("key")
    if not key or key == "unknown":
        raise SystemExit("could not derive the registered Codex account key")
    return key
