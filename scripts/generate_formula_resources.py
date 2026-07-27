#!/usr/bin/env python3
"""Regenerate the Homebrew formula's `resource` blocks from the actual resolved
dependency tree, so pins never drift from what pip would really install.

Usage:
    python3 scripts/generate_formula_resources.py > resources.rb
Then paste the output into ../homebrew-unhoard/Formula/unhoard.rb,
replacing the existing resource blocks (everything between `depends_on` and `def install`).

Run this after bumping trafilatura/requests or if `pip install -e .` starts
pulling different transitive versions.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

PACKAGES = ["requests", "trafilatura"]  # top-level deps; transitive tree resolved by pip


def resolve_versions() -> dict[str, str]:
    with tempfile.TemporaryDirectory() as tmp:
        venv_dir = Path(tmp) / "venv"
        subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True)
        pip = venv_dir / "bin" / "pip"
        subprocess.run([str(pip), "install", "-q", "--upgrade", "pip"], check=True)
        subprocess.run([str(pip), "install", "-q", *PACKAGES], check=True)
        freeze = subprocess.run([str(pip), "freeze"], check=True, capture_output=True, text=True).stdout
    versions = {}
    for line in freeze.strip().splitlines():
        name, _, ver = line.partition("==")
        versions[name.strip()] = ver.strip()
    return versions


def sdist_info(name: str, version: str) -> dict:
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    with urllib.request.urlopen(url) as resp:
        data = json.load(resp)
    sdist = next((u for u in data["urls"] if u["packagetype"] == "sdist"), None)
    if not sdist:
        raise RuntimeError(f"No sdist found for {name}=={version}; you'll need to pin this one by hand.")
    return {"url": sdist["url"], "sha256": sdist["digests"]["sha256"]}


def main():
    versions = resolve_versions()
    print(f"# Regenerated for: {', '.join(f'{k}=={v}' for k, v in sorted(versions.items()))}", file=sys.stderr)
    for name in sorted(versions):
        info = sdist_info(name, versions[name])
        print(f'  resource "{name}" do')
        print(f'    url "{info["url"]}"')
        print(f'    sha256 "{info["sha256"]}"')
        print("  end\n")


if __name__ == "__main__":
    main()

