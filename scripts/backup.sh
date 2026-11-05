#!/usr/bin/env bash
set -euo pipefail
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root=${TOLLWIRE_ROOT:-$(dirname -- "$script_dir")}
exec bash "$root/panel" backup "$@"
