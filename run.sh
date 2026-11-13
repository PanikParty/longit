#!/usr/bin/env bash
# Set up the environment (uv sync) and launch long. Extra arguments are passed through:
#   ./run.sh                      interactive, in the current directory
#   ./run.sh "show last commit"   one-shot request
#   ./run.sh -C ~/code/app        work in another directory
# Symlink it onto your PATH to run long from anywhere; it works in the directory you call it from.
set -euo pipefail

# Resolve the project directory even when invoked through a symlink.
src="${BASH_SOURCE[0]}"
while [ -L "$src" ]; do
  dir="$(cd -P "$(dirname "$src")" && pwd)"
  src="$(readlink "$src")"
  [[ "$src" != /* ]] && src="$dir/$src"
done
project="$(cd -P "$(dirname "$src")" && pwd)"

if ! command -v uv >/dev/null 2>&1; then
  echo "run.sh: uv is not installed. See https://docs.astral.sh/uv/getting-started/installation/" >&2
  exit 1
fi

uv sync --quiet --project "$project"
exec uv run --quiet --project "$project" long "$@"
