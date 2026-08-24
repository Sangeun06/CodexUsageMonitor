#!/usr/bin/env python3
"""Build a rootless, optionally provisioned collector deployment bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tarfile
import tempfile
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
    parser.add_argument("--without-token", action="store_true", help="Build a generic bundle with no embedded secret")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not (args.server.startswith("http://") or args.server.startswith("https://")):
        raise SystemExit("--server must use http:// or https://")
    version = (ROOT / "VERSION").read_text().strip()
    account_key = resolve_account_key(ROOT, args.token_file, args.account_key)
    flavor = "generic" if args.without_token else "provisioned"
    filename = f"codex-usage-collector-{version}-linux-user-{flavor}.tar.gz"
    args.output_dir.mkdir(parents=True, exist_ok=True)
    output = args.output_dir / filename

    with tempfile.TemporaryDirectory(prefix="codex-collector-build-") as temp:
        package = Path(temp) / f"codex-usage-collector-{version}"
        package.mkdir()
        copies = {
            ROOT / "collector.py": package / "collector.py",
            ROOT / "deploy" / "codex-usage-collector.service": package / "codex-usage-collector.service",
            ROOT / "packaging" / "install.sh": package / "install.sh",
            ROOT / "packaging" / "uninstall.sh": package / "uninstall.sh",
        }
        for source, destination in copies.items():
            shutil.copy2(source, destination)
        for executable in (package / "collector.py", package / "install.sh", package / "uninstall.sh"):
            executable.chmod(0o755)
        (package / "bundle.conf").write_text(f"server={args.server}\naccount_key={account_key}\n")
        (package / "bundle.conf").chmod(0o644)
        if not args.without_token:
            if not args.token_file.is_file():
                raise SystemExit(f"collector token not found: {args.token_file}")
            shutil.copy2(args.token_file, package / "collector.token")
            (package / "collector.token").chmod(0o600)
        manifest = {
            "name": "codex-usage-collector",
            "version": version,
            "server": args.server,
            "account_key": account_key,
            "token_included": not args.without_token,
            "install_scope": "current-user",
        }
        (package / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        checksums = []
        for item in sorted(package.iterdir()):
            if item.name != "SHA256SUMS":
                checksums.append(f"{sha256(item)}  {item.name}")
        (package / "SHA256SUMS").write_text("\n".join(checksums) + "\n")

        with tarfile.open(output, "w:gz") as archive:
            archive.add(package, arcname=package.name)
    output.chmod(0o600 if not args.without_token else 0o644)
    print(output)
    print(f"sha256={sha256(output)}")
    print(f"token_included={not args.without_token}")


if __name__ == "__main__":
    main()
