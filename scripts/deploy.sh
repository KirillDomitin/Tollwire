#!/usr/bin/env bash
set -euo pipefail
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
command -v python3 >/dev/null || { printf 'Python 3 is required for release deployment.\n' >&2; exit 1; }
exec python3 "$script_dir/release.py" deploy "$@"
