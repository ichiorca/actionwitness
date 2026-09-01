"""Fail the build if a secret-shaped value is committed (009-T7).

Constitution §5 and `memory/rules/security-baseline.md`: never hardcode secrets,
and if one is exposed, stop and rotate before continuing. A scanner cannot make
that judgement — but it can make the exposure impossible to merge without
somebody looking at it, which is the whole job.

Two design decisions worth stating.

**Only high-confidence patterns.** A scanner that flags every long string gets
switched off within a week, and a switched-off scanner is worse than none because
the badge still says it ran. Every pattern below matches a credential format with
a recognisable prefix and length, so a hit is a credential or a deliberate
lookalike — not a hash, a base64 blob, or a long identifier.

**Test placeholders are acknowledged inline, not by path.** Excluding `tests/`
wholesale would mean a real credential pasted into a fixture never gets found,
and fixtures are exactly where credentials get pasted. Instead a line may carry
one of `ACKNOWLEDGED_MARKERS`, which is a claim the author makes in the diff and a
reviewer can see.

Scans tracked files only: the working tree also holds the operator's local rig and
planning documents, which are not part of the deliverable and are not this
script's business.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: A line carrying one of these is a deliberate non-secret. Kept short and
#: unmistakable: the point is that a reader of the diff can tell the author meant
#: it, not that the string is hard to type.
ACKNOWLEDGED_MARKERS = (
    "not-a-real-credential",
    "placeholder",
    "example.com",
    "EXAMPLE",
)

#: (name, pattern). Each matches a credential format, not merely a long string.
PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\b(?:ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{36}\b")),
    ("GitHub fine-grained token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{60,}\b")),
    ("model-provider key", re.compile(r"\bsk-[A-Za-z0-9_-]{24,}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("Shopify access token", re.compile(r"\bshp(?:at|ca|pa|ss)_[a-fA-F0-9]{32}\b")),
    ("PEM-encoded certificate key", re.compile(r"\bPRIVATE KEY-----\\n")),
)

#: Binary and lockfile noise. A lockfile records integrity hashes, which are long
#: base64 strings that no pattern above matches — listed anyway so a future
#: broader pattern does not turn the lockfiles into a wall of false positives.
SKIPPED_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".pdf"})
SKIPPED_NAMES = frozenset({"package-lock.json", "uv.lock", "scan_for_secrets.py"})


def tracked_files() -> list[Path]:
    """Every file git knows about, which is every file that could be published."""
    listing = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [REPO_ROOT / name for name in listing.stdout.split("\0") if name]


def findings(paths: list[Path]) -> list[str]:
    reported: list[str] = []
    for path in paths:
        if path.suffix in SKIPPED_SUFFIXES or path.name in SKIPPED_NAMES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            # Unreadable as text means it is not a source file this scans.
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if any(marker in line for marker in ACKNOWLEDGED_MARKERS):
                continue
            for name, pattern in PATTERNS:
                if pattern.search(line):
                    relative = path.relative_to(REPO_ROOT).as_posix()
                    reported.append(f"{relative}:{number}: possible {name}")
    return reported


def env_file_is_tracked(paths: list[Path]) -> bool:
    """`.env` holds real configuration; `.env.example` is the one that ships."""
    return any(path.name == ".env" for path in paths)


def main() -> int:
    paths = tracked_files()

    problems = findings(paths)
    if env_file_is_tracked(paths):
        problems.append(".env is tracked; only .env.example may be committed")

    if problems:
        print("Secret scan failed:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        print(
            "\nIf a hit is a deliberate test placeholder, put one of "
            f"{list(ACKNOWLEDGED_MARKERS)} on the line. If it is real: stop, "
            "rotate the credential, and scrub it from history before continuing.",
            file=sys.stderr,
        )
        return 1

    print(f"secret scan clean: {len(paths)} tracked files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
