#!/usr/bin/env python3
"""Build a non-admin Windows collector ZIP package."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
import zipfile
from pathlib import Path

from package_common import resolve_account_key


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "dist"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default="http://143.248.136.95:8765")
    parser.add_argument("--token-file", type=Path, default=ROOT / "data" / "collector.token")
    parser.add_argument("--account-key", help="Registered 12-character anonymized account key")
    parser.add_argument("--without-token", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not (args.server.startswith("http://") or args.server.startswith("https://")):
        raise SystemExit("--server must use http:// or https://")
    version = (ROOT / "VERSION").read_text().strip()
    account_key = resolve_account_key(ROOT, args.token_file, args.account_key)
    flavor = "generic" if args.without_token else "provisioned"
    package_name = f"codex-usage-collector-{version}-windows-user-{flavor}"
    args.output_dir.mkdir(parents=True, exist_ok=True)
    output = args.output_dir / f"{package_name}.zip"

    with tempfile.TemporaryDirectory(prefix="codex-windows-collector-build-") as temp:
        package = Path(temp) / f"codex-usage-collector-{version}"
        package.mkdir()
        sources = {
            ROOT / "collector.py": package / "collector.py",
            ROOT / "packaging" / "windows" / "install.ps1": package / "install.ps1",
            ROOT / "packaging" / "windows" / "uninstall.ps1": package / "uninstall.ps1",
            ROOT / "packaging" / "windows" / "install.cmd": package / "install.cmd",
            ROOT / "packaging" / "windows" / "uninstall.cmd": package / "uninstall.cmd",
        }
        for source, destination in sources.items():
            shutil.copy2(source, destination)
        (package / "bundle.conf").write_text(f"server={args.server}\naccount_key={account_key}\n")
        if not args.without_token:
            if not args.token_file.is_file():
                raise SystemExit(f"collector token not found: {args.token_file}")
            shutil.copy2(args.token_file, package / "collector.token")
        manifest = {
            "name": "codex-usage-collector",
            "version": version,
            "platform": "windows",
            "server": args.server,
            "account_key": account_key,
            "token_included": not args.without_token,
            "install_scope": "current-user",
            "requires": "Python 3 and a local Codex installation",
        }
        (package / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        checksums = [
            f"{sha256(item)}  {item.name}"
            for item in sorted(package.iterdir())
            if item.name != "SHA256SUMS"
        ]
        (package / "SHA256SUMS").write_text("\n".join(checksums) + "\n")

        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for item in sorted(package.iterdir()):
                archive.write(item, arcname=f"{package.name}/{item.name}")
    output.chmod(0o600 if not args.without_token else 0o644)
    print(output)
    print(f"sha256={sha256(output)}")
    print(f"token_included={not args.without_token}")


if __name__ == "__main__":
    main()
