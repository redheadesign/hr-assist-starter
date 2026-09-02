"""Copy an explicitly supplied document locally, with purpose and deduplication."""
import argparse
import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from workspace import local_path, read_json, write_json

EXTENSIONS = {".pdf", ".docx", ".txt", ".md", ".png", ".jpg", ".jpeg", ".webp"}
KINDS = ("resume", "work_history", "portfolio", "photo", "reference", "other")


def intake(source, kind, purpose):
    source = Path(source).resolve(strict=True)
    if kind not in KINDS or not purpose.strip():
        raise ValueError("A document kind and purpose are required")
    if not source.is_file() or source.suffix.lower() not in EXTENSIONS:
        raise ValueError("Unsupported file type; do not import executable or macro-enabled files")
    with source.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    manifest_path = local_path("documents/manifest.json")
    manifest = read_json(manifest_path, [])
    found = next((item for item in manifest if item["sha256"] == digest), None)
    if found:
        return {"duplicate": True, "file": found["file"]}
    relative = f"{'photos' if kind == 'photo' else 'documents'}/{digest[:20]}{source.suffix.lower()}"
    destination = local_path(relative)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    item = {"file": relative, "original_name": source.name, "kind": kind, "purpose": purpose, "sha256": digest, "added_at": datetime.now(timezone.utc).isoformat(), "external_upload_allowed": False}
    manifest.append(item)
    write_json(manifest_path, manifest)
    return {"duplicate": False, "file": relative}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", required=True)
    parser.add_argument("--kind", choices=KINDS, required=True)
    parser.add_argument("--purpose", required=True)
    args = parser.parse_args()
    print(json.dumps(intake(args.file, args.kind, args.purpose), ensure_ascii=False))
