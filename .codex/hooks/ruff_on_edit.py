"""PostToolUse-хук Claude Code: после Edit/Write .py-файла — ruff format + ruff check --fix только этого файла.

Ошибки, которые ruff не исправил сам, уходят в stderr с кодом 2 — Claude Code показывает их агенту сразу,
и он чинит их в том же шаге, а не после полного прогона.
"""

import json
import subprocess
import sys

data = json.load(sys.stdin)
path = (data.get("tool_input") or {}).get("file_path", "")
if not path.endswith(".py"):
    sys.exit(0)

subprocess.run(["uv", "run", "--quiet", "ruff", "format", "--quiet", path], check=False)
check = subprocess.run(
    ["uv", "run", "--quiet", "ruff", "check", "--fix", "--quiet", "--output-format=concise", path],
    capture_output=True,
    text=True,
)
if check.returncode != 0:
    print(check.stdout.strip()[-2000:], file=sys.stderr)
    sys.exit(2)
