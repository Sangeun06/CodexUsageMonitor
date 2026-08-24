#!/usr/bin/env python3
"""Build an elevated, machine-wide Windows NSIS installer."""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import urlparse

from package_common import resolve_account_key


ROOT = Path(__file__).resolve().parents[1]
PYTHON_RUNTIME_SHA256 = "8766a8775746235e23cf5aee5027ab1060bb981d93110577adcf3508aa0cbd55"


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
    parser.add_argument("--python-runtime", type=Path, required=True)
    parser.add_argument("--makensis", type=Path, required=True)
    parser.add_argument("--nsis-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.token_file.is_file():
        raise SystemExit(f"collector token not found: {args.token_file}")
    parsed_server = urlparse(args.server)
    if parsed_server.scheme not in {"http", "https"} or not parsed_server.hostname:
        raise SystemExit("--server must be an absolute HTTP(S) URL")
    server_port = parsed_server.port or (443 if parsed_server.scheme == "https" else 80)
    account_key = resolve_account_key(ROOT, args.token_file, args.account_key)
    if sha256(args.python_runtime) != PYTHON_RUNTIME_SHA256:
        raise SystemExit("Python runtime SHA-256 does not match the pinned official release")
    version = (ROOT / "VERSION").read_text().strip()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    output = (args.output_dir / f"codex-usage-collector-{version}-windows-machine-setup.exe").resolve()

    with tempfile.TemporaryDirectory(prefix="codex-windows-exe-") as temp:
        runtime = Path(temp) / "runtime"
        runtime.mkdir()
        with zipfile.ZipFile(args.python_runtime) as archive:
            archive.extractall(runtime)
        if not (runtime / "pythonw.exe").is_file() or not any(runtime.glob("_sqlite3.pyd")):
            raise SystemExit("embedded Python runtime is missing pythonw.exe or SQLite support")

        environment = os.environ.copy()
        environment["NSISDIR"] = str(args.nsis_dir.resolve())
        command = [
            str(args.makensis.resolve()),
            "-V3",
            "-WX",
            f"-DAPP_VERSION={version}",
            f"-DSOURCE_DIR={ROOT}",
            f"-DRUNTIME_DIR={runtime}",
            f"-DTOKEN_FILE={args.token_file.resolve()}",
            f"-DSERVER_URL={args.server}",
            f"-DSERVER_PORT={server_port}",
            f"-DTARGET_ACCOUNT_KEY={account_key}",
            f"-DOUTPUT_EXE={output}",
            str(ROOT / "packaging" / "windows" / "installer.nsi"),
        ]
        subprocess.run(command, check=True, env=environment)
    output.chmod(0o600)
    print(output)
    print(f"sha256={sha256(output)}")
    print("scope=machine (UAC administrator approval required)")


if __name__ == "__main__":
    main()
