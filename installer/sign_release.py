#!/usr/bin/env python3
"""
Signs a release for the agent's auto-update (v2.0.0.15+).

Run by build.yml after the installer is compiled. Writes, next to the
installer:
  manifest.json  {"product", "version", "file", "size", "sha256"}
  manifest.sig   base64 RSA (PKCS#1 v1.5, SHA-256) signature of manifest.json's exact bytes

watchdog.ps1 on each PC downloads both, checks the signature against the
public key built into it, then checks the installer's size and SHA-256
against the manifest before running it. The private key comes from the
UPDATE_SIGNING_KEY repository secret (PEM) and never leaves GitHub Actions.

Safety checks here, so a bad release fails the build instead of reaching PCs:
  - the signature is verified against installer/update-signing-public.pem
    (the same key as in watchdog.ps1), so a wrong secret is caught;
  - on a tag build, the tag must be exactly "v" + the installer's version.

Usage: sign_release.py <installer.iss> <path to RR-IT-Insight-Setup.exe>
Without UPDATE_SIGNING_KEY set (e.g. a fork), it prints a warning and exits 0
on branch builds, but fails on tag builds — a release must always be signed.
"""
import base64
import hashlib
import json
import os
import re
import sys
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


def installer_version(iss_path: Path) -> str:
    m = re.search(r'#define\s+MyAppVersion\s+"([0-9.]+)"', iss_path.read_text(encoding="utf-8"))
    if not m:
        sys.exit("Could not find MyAppVersion in " + str(iss_path))
    return m.group(1)


def main() -> None:
    iss_path, exe_path = Path(sys.argv[1]), Path(sys.argv[2])
    version = installer_version(iss_path)
    ref = os.environ.get("GITHUB_REF", "")
    is_tag = ref.startswith("refs/tags/")
    if is_tag and ref != f"refs/tags/v{version}":
        sys.exit(f"Tag {ref} does not match installer version {version} — refusing to sign.")

    key_pem = os.environ.get("UPDATE_SIGNING_KEY", "").strip()
    if not key_pem:
        if is_tag:
            sys.exit("UPDATE_SIGNING_KEY secret is not set — a release must be signed.")
        print("::warning::UPDATE_SIGNING_KEY not set — skipping release signing (branch build).")
        return

    data = exe_path.read_bytes()
    manifest = {
        "product": "rr-it-insight-agent",
        "version": version,
        "file": exe_path.name,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    manifest_bytes = json.dumps(manifest, separators=(",", ":"), sort_keys=True).encode("utf-8")

    private_key = serialization.load_pem_private_key(key_pem.encode("utf-8"), password=None)
    signature = private_key.sign(manifest_bytes, padding.PKCS1v15(), hashes.SHA256())

    public_pem = (Path(__file__).parent / "update-signing-public.pem").read_bytes()
    public_key = serialization.load_pem_public_key(public_pem)
    public_key.verify(signature, manifest_bytes, padding.PKCS1v15(), hashes.SHA256())  # raises if wrong key

    out_dir = exe_path.parent
    (out_dir / "manifest.json").write_bytes(manifest_bytes)
    (out_dir / "manifest.sig").write_text(base64.b64encode(signature).decode("ascii"), encoding="ascii")
    print(f"Signed release manifest for {version}: {manifest['sha256']} ({manifest['size']} bytes)")


if __name__ == "__main__":
    main()
