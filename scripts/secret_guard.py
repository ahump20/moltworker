#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


ALLOW_MARKERS = ("secret-scan:allow", "gitleaks:allow", "pragma: allowlist secret")
SAFE_ENV_EXAMPLES = (
    re.compile(r"\.env\.example$", re.IGNORECASE),
    re.compile(r"\.env\.sample$", re.IGNORECASE),
    re.compile(r"\.env\.template$", re.IGNORECASE),
    re.compile(r"\.example\.env$", re.IGNORECASE),
)
BLOCKED_FILE_PATTERNS = (
    ("environment file", re.compile(r"(^|/)\.env($|\.)", re.IGNORECASE)),
    ("private key", re.compile(r"(^|/)(id_rsa|id_dsa|id_ecdsa|id_ed25519)$", re.IGNORECASE)),
    ("private key", re.compile(r"\.(pem|p12|pfx)$", re.IGNORECASE)),
    ("private key", re.compile(r"(^|/).*private.*\.key$", re.IGNORECASE)),
    ("credentials dump", re.compile(r"(^|/)(credentials|secrets?)\.(json|ya?ml|toml|txt)$", re.IGNORECASE)),
)
SECRET_PATTERNS = (
    ("OpenAI key", re.compile(r"\bsk-(?:proj-|live-|test-)?[A-Za-z0-9_-]{20,}\b")),
    ("Anthropic key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("GitHub token", re.compile(r"\bgh[opusr]_[A-Za-z0-9]{20,}\b")),
    ("GitHub fine-grained token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z\\-_]{35}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("Stripe secret key", re.compile(r"\bsk_(?:live|test)_[A-Za-z0-9]{16,}\b")),
    ("JWT token", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("Private key block", re.compile(r"-----BEGIN (?:[A-Z ]+)?PRIVATE KEY-----")),
    (
        "Credential assignment",
        re.compile(
            r"\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|secret|token|password|passwd|cloudflare_api_token|openai_api_key)\b[^\S\r\n]{0,8}[:=][^\S\r\n]*[\"']?(?!YOUR_|YOUR-|CHANGEME|CHANGE_ME|example|dummy|fake|placeholder|test|sample|<)[A-Za-z0-9_./+=:@-]{12,}",
            re.IGNORECASE,
        ),
    ),
)
PLACEHOLDER_WORDS = ("example", "placeholder", "dummy", "fake", "sample", "changeme", "your_")


@dataclass
class Finding:
    path: str
    line: int | None
    label: str
    excerpt: str


def run_git(args: list[str]) -> str:
    return subprocess.check_output(["git", *args], text=True, stderr=subprocess.STDOUT)


def repo_root() -> Path:
    return Path(run_git(["rev-parse", "--show-toplevel"]).strip())


def staged_files() -> list[str]:
    output = run_git(["diff", "--cached", "--name-only", "--diff-filter=ACMR"])
    return [line.strip() for line in output.splitlines() if line.strip()]


def files_in_range(revision_range: str) -> list[str]:
    output = run_git(["diff", "--name-only", "--diff-filter=ACMR", revision_range])
    return [line.strip() for line in output.splitlines() if line.strip()]


def diff_for_file(path: str, staged: bool, revision_range: str | None) -> str:
    if staged:
        return run_git(["diff", "--cached", "--no-ext-diff", "--unified=0", "--", path])
    if revision_range:
        return run_git(["diff", "--no-ext-diff", "--unified=0", revision_range, "--", path])
    return ""


def is_allowed_example_file(path: str) -> bool:
    return any(pattern.search(path) for pattern in SAFE_ENV_EXAMPLES)


def blocked_file_reason(path: str) -> str | None:
    if is_allowed_example_file(path):
        return None
    for label, pattern in BLOCKED_FILE_PATTERNS:
        if pattern.search(path):
            return label
    return None


def extract_added_lines(diff_text: str) -> list[tuple[int, str]]:
    results: list[tuple[int, str]] = []
    next_line = 0

    for line in diff_text.splitlines():
        if line.startswith("@@"):
            match = re.search(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
            next_line = int(match.group(1)) if match else 0
            continue
        if line.startswith(("+++", "---", "\\")):
            continue
        if line.startswith("+"):
            results.append((next_line, line[1:]))
            next_line += 1
            continue
        if not line.startswith("-"):
            next_line += 1
    return results


def line_findings(path: str, line_number: int, text: str) -> list[Finding]:
    if not text.strip():
        return []
    lowered_text = text.lower()
    if any(marker in lowered_text for marker in ALLOW_MARKERS):
        return []

    findings: list[Finding] = []
    for label, pattern in SECRET_PATTERNS:
        for match in pattern.finditer(text):
            value = match.group(0)
            lowered = value.lower()
            if any(word in lowered for word in PLACEHOLDER_WORDS):
                continue
            findings.append(Finding(path=path, line=line_number, label=label, excerpt=text[:200]))
    return findings


def scan(staged: bool, revision_range: str | None) -> list[Finding]:
    files = staged_files() if staged else files_in_range(revision_range or "")
    findings: list[Finding] = []
    for path in files:
        blocked_reason = blocked_file_reason(path)
        if blocked_reason:
            findings.append(Finding(path=path, line=None, label=f"Blocked {blocked_reason} filename", excerpt=path))
            continue

        diff_text = diff_for_file(path, staged=staged, revision_range=revision_range)
        if not diff_text.strip():
            continue
        for line_number, text in extract_added_lines(diff_text):
            findings.extend(line_findings(path, line_number, text))
    return findings


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Block secrets before they land in git.")
    parser.add_argument("--staged", action="store_true", help="Scan the staged diff.")
    parser.add_argument("--range", dest="revision_range", help="Scan a git revision range.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.staged and not args.revision_range:
        print("Provide --staged or --range.", file=sys.stderr)
        return 2

    root = repo_root()
    subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=root, check=True, capture_output=True)
    findings = scan(staged=args.staged, revision_range=args.revision_range)
    if not findings:
        return 0

    print("Secret scan blocked this change.", file=sys.stderr)
    print("Move credentials to environment or secret storage, or add an explicit allow marker for known fake values.", file=sys.stderr)
    for finding in findings:
        location = f"{finding.path}:{finding.line}" if finding.line else finding.path
        print(f"- {location} — {finding.label}", file=sys.stderr)
        print(f"  {finding.excerpt}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
