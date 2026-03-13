#!/usr/bin/env bash
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"
git config core.hooksPath .githooks
chmod +x .githooks/pre-commit .githooks/pre-push scripts/install-git-hooks.sh scripts/secret_guard.py
echo "Configured core.hooksPath to .githooks"
