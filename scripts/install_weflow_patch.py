"""Install the pinned Windows x64 DLL from WeFlow Community Patch issue #2.

No clock changes, process termination, or automatic Bot startup. --check is offline.
The DLL keeps its upstream license; this installer is project-owned AGPL-3.0 code.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
COMMIT = "57829a0391d05138f5e002a52f636c01295a378a"
REPOSITORY = "AnonymousUser443/WeFlow-Community-Patch"
PATCH_SHA256 = "1536606b1b1b2a0dc9de631a7f45504f5d466de0979e50b3f94548ae124993d0"
KNOWN_ORIGINALS = {
    "6397760da70de8062829fbe6a2ec01cf0616d6f2b334e6fe54873898f38f7ad7",
    "6915913a3a9930694e5c821bf58841e17bbad0f56691e307674fb0e31e9d44b8",
}
DEFAULT_DLL = ROOT / "weflow-core/resources/wcdb/win32/x64/wcdb_api.dll"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_patch(data: bytes) -> None:
    if hashlib.sha256(data).hexdigest() != PATCH_SHA256:
        raise ValueError("Downloaded DLL SHA-256 does not match the pinned patch")
    pe_offset = int.from_bytes(data[60:64], "little")
    if data[:2] != b"MZ" or data[pe_offset:pe_offset + 6] != b"PE\0\0\x64\x86":
        raise ValueError("Expected a Windows x64 PE DLL")


def read_url(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "WechatBot-DLL-maintenance"})
    with urllib.request.urlopen(req, timeout=60) as response:
        return response.read()


def download_patch() -> bytes:
    # GitHub API fallback avoids raw.githubusercontent.com connectivity failures.
    url = f"https://raw.githubusercontent.com/{REPOSITORY}/{COMMIT}/binaries/wcdb_api.dll"
    try:
        data = read_url(url)
        validate_patch(data)
        return data
    except (OSError, ValueError):
        api = f"https://api.github.com/repos/{REPOSITORY}/contents/binaries/wcdb_api.dll?ref={COMMIT}"
        record = json.loads(read_url(api))
        if not record.get("content"):
            record = json.loads(read_url(record["git_url"]))
        data = base64.b64decode(record["content"], validate=False)
        validate_patch(data)
        return data


def assert_not_loaded(target: Path) -> None:
    import psutil
    normalized = os.path.normcase(str(target.resolve()))
    for process in psutil.process_iter(["pid", "name", "exe"]):
        try:
            # Only examine the owned WeFlow runtime; never stop any process.
            exe = process.info.get("exe") or ""
            if os.path.normcase(exe) != os.path.normcase(str(ROOT / "weflow-core/WeFlow.exe")):
                continue
            if any(os.path.normcase(m.path) == normalized for m in process.memory_maps()):
                raise RuntimeError(f"DLL is loaded by PID {process.pid}; stop Bot/WeFlow first")
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied as exc:
            raise RuntimeError("Cannot verify WeFlow DLL use; stop WeFlow first") from exc


def install(target: Path, data: bytes, allow_version_change: bool = False) -> Path | None:
    validate_patch(data)
    old_hash = sha256(target) if target.is_file() else None
    if old_hash == PATCH_SHA256:
        return None
    if old_hash and old_hash not in KNOWN_ORIGINALS and not allow_version_change:
        raise ValueError("Unknown DLL version; review ABI compatibility before using --allow-version-change")
    assert_not_loaded(target)
    backup_dir = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "WechatBot/backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"wcdb_api-{old_hash}.dll" if old_hash else None
    if backup:
        if backup.exists() and sha256(backup) != old_hash:
            raise ValueError("Existing backup hash mismatch; refusing to overwrite it")
        if not backup.exists():
            shutil.copy2(target, backup)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, suffix=".tmp", delete=False) as f:
            temporary = Path(f.name)
            f.write(data)
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return backup


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Only check installed hash; no network or changes")
    parser.add_argument("--dll", type=Path, default=DEFAULT_DLL)
    parser.add_argument("--source", type=Path, help="Use an already downloaded DLL (hash is still enforced)")
    parser.add_argument("--allow-version-change", action="store_true")
    args = parser.parse_args()
    target = args.dll.resolve()
    try:
        if args.check:
            actual = sha256(target)
            print(f"{target}\nSHA-256: {actual}")
            return 0 if actual == PATCH_SHA256 else 1
        if target.is_file() and sha256(target) == PATCH_SHA256:
            print("Patched DLL is already installed and verified; no download required.")
            return 0
        data = args.source.read_bytes() if args.source else download_patch()
        backup = install(target, data, args.allow_version_change)
        print("Patched DLL verified and installed (or already installed).")
        if backup:
            print(f"Backup: {backup}")
        print("Start/restart Bot after installation; no system clock changes are needed.")
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
