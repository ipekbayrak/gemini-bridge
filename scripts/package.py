#!/usr/bin/env python3
"""Build a portable release ZIP without machine data or job logs."""
import argparse, hashlib, json, re, stat, zipfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
FILES = {"plugin.json", "mcp.json", "README.md", "README.tr.md", "LICENSE", ".gitignore"}
DIRS = {"scripts", "skills", "assets", "tests"}

def build(destination):
    version = json.loads((ROOT / "plugin.json").read_text())["version"]
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / f"Gemini-Bridge-{version}.zip"
    included = []
    for path in sorted(ROOT.rglob("*")):
        rel = path.relative_to(ROOT)
        if not path.is_file() or path.is_symlink(): continue
        if rel.parts[0] not in DIRS and str(rel) not in FILES: continue
        if any(x.startswith(".") and x != ".gitignore" for x in rel.parts): continue
        if "__pycache__" in rel.parts or path.suffix in {".pyc", ".log"}: continue
        raw = path.read_bytes()
        if re.search(rb"/" + rb"Users/[^/\s]+/|/" + rb"home/[^/\s]+/|job-" + rb"[0-9a-f]{16}|[A-Za-z0-9._%+-]+@gmail\.com", raw):
            raise SystemExit(f"Machine-specific or task data found in {rel}")
        included.append((path, rel, raw))
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for path, rel, raw in included:
            info = zipfile.ZipInfo(str(rel), (2026, 1, 1, 0, 0, 0))
            mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
            info.external_attr = (stat.S_IFREG | mode) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, raw)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(".sha256").write_text(digest + "  " + archive.name + "\n")
    return {"archive": str(archive), "sha256": digest, "files": len(included)}

if __name__ == "__main__":
    p = argparse.ArgumentParser();p.add_argument("--output", type=Path, required=True)
    print(json.dumps(build(p.parse_args().output), indent=2))
