"""Conservative release check. Never prints matched personal values."""
import argparse
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {".py", ".md", ".txt", ".json"}
SPECIAL_FILES = {".gitignore", ".gitattributes", ".release-allowlist", ".githooks/pre-push"}
PATTERNS = {
    "email": re.compile(r"[A-Za-z0-9_.+%-]+@(?!(?:example\.invalid)\b)[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "personal-home": re.compile(r"(?:[A-Za-z]:[/\\]Users[/\\]|/Users/|/home/)[A-Za-z0-9_.-]+", re.I),
    "private-key": re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----"),
    "access-token": re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,})"),
    "personal-profile-url": re.compile(r"https?://(?:t\.me/[^\s)]+|(?:www\.)?linkedin\.com/in/[^\s)]+|hh\.ru/resume/[A-Za-z0-9]+)", re.I),
    "phone": re.compile(r"(?<!\w)\+\d[\d ()-]{9,}\d(?!\w)"),
    "money-literal": re.compile(r"\b\d[\d \u00a0]{2,}\s*(?:₽|руб\b|RUB\b|USD\b|EUR\b)", re.I),
}


def git(*args, cwd=None):
    return subprocess.check_output(["git", *args], cwd=cwd or ROOT, stderr=subprocess.PIPE)


def allowed_files(root):
    entries = (root / ".release-allowlist").read_text(encoding="utf-8").splitlines()
    return {p for p in entries if p and not p.startswith("#")}


def inspect_text(path, data, denylist=()):
    findings = []
    if len(data) > 1_000_000:
        return [f"{path}: oversized file"]
    try:
        value = data.decode("utf-8")
    except UnicodeDecodeError:
        return [f"{path}: binary or non-UTF-8 content"]
    if "\x00" in value:
        return [f"{path}: binary content"]
    for line_number, line in enumerate(value.splitlines(), 1):
        for kind, expression in PATTERNS.items():
            if expression.search(line):
                findings.append(f"{path}:{line_number}: {kind}")
        for term in denylist:
            if term and term.casefold() in line.casefold():
                findings.append(f"{path}:{line_number}: local denylist match")
                break
    return findings


def check(root=ROOT, index=False, history=False, denylist=()):
    root = Path(root)
    allowed = allowed_files(root)
    findings = []
    if index:
        paths = git("ls-files", "-z", cwd=root).decode("utf-8").rstrip("\0").split("\0")
        paths = [p for p in paths if p]
        modes = git("ls-files", "--stage", "-z", cwd=root).decode("utf-8").split("\0")
        for entry in modes:
            if entry and not entry.startswith(("100644 ", "100755 ")):
                findings.append("index: non-regular file or submodule")
    else:
        paths = sorted(allowed)
    for name in paths:
        relative = Path(name)
        if name not in allowed or relative.is_absolute() or ".." in relative.parts or (relative.suffix not in TEXT_SUFFIXES and name not in SPECIAL_FILES):
            findings.append(f"{name}: not approved for release")
            continue
        target = root / name
        if not index and (target.is_symlink() or not target.resolve().is_relative_to(root.resolve())):
            findings.append(f"{name}: unsafe path")
            continue
        try:
            content = git("show", f":{name}", cwd=root) if index else target.read_bytes()
            findings.extend(inspect_text(name, content, denylist))
        except (OSError, subprocess.CalledProcessError):
            findings.append(f"{name}: cannot read")
    if history:
        # Enumerate each reachable tree, including files later deleted from HEAD.
        commits = git("rev-list", "--all", cwd=root).decode().splitlines()
        seen = set()
        for commit in commits:
            metadata = git("show", "-s", "--format=%an%n%ae%n%cn%n%ce%n%B", commit, cwd=root)
            findings.extend(inspect_text("commit metadata", metadata, denylist))
            for row in git("ls-tree", "-rz", commit, cwd=root).split(b"\0"):
                if not row:
                    continue
                header, name_bytes = row.split(b"\t", 1)
                mode, kind, oid = header.decode().split()
                name = name_bytes.decode("utf-8")
                if name not in allowed or kind != "blob" or mode not in ("100644", "100755"):
                    findings.append(f"history/{name}: not approved for release")
                if kind == "blob" and oid not in seen:
                    seen.add(oid)
                    findings.extend(inspect_text(f"history/{name}", git("cat-file", "blob", oid, cwd=root), denylist))
    return sorted(set(findings))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", action="store_true", help="Inspect staged bytes instead of working files")
    parser.add_argument("--history", action="store_true", help="Inspect every reachable commit")
    parser.add_argument("--denylist", help="External JSON array of private strings; never commit it")
    args = parser.parse_args()
    denied = json.loads(Path(args.denylist).read_text(encoding="utf-8")) if args.denylist else []
    if not isinstance(denied, list) or not all(isinstance(v, str) for v in denied):
        parser.error("Denylist must be an array of strings")
    try:
        errors = check(index=args.index, history=args.history, denylist=denied)
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"FAIL: check could not complete ({type(exc).__name__})")
        raise SystemExit(2)
    for error in errors:
        print(error)
    print("FAIL: review required" if errors else "PASS: automated checks passed; manual file review still required")
    raise SystemExit(bool(errors))
