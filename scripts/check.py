#!/usr/bin/env python3
"""Run an explicit check, retain its log and print a compact result (stdlib only)."""

import argparse
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name", help="short label, e.g. backend-unit")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="-- executable arguments")
    options = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", options.name):
        parser.error("name must contain 1–64 letters, digits, underscores or hyphens")
    command = options.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("an explicit command is required")
    folder = Path(__file__).resolve().parent.parent / ".checks"
    folder.mkdir(mode=0o700, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    descriptor, filename = tempfile.mkstemp(prefix=f"{stamp}-{options.name}-", suffix=".log", dir=folder)
    started = time.monotonic()
    process = None
    result = 1
    try:
        with os.fdopen(descriptor, "wb") as log:
            try:
                process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
                result = process.wait()
            except KeyboardInterrupt:
                if process is not None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                result = 130
            except OSError as exc:
                # Do not print command arguments or environment values.
                log.write(f"Cannot start check: {type(exc).__name__}\n".encode())
                result = 127
    finally:
        outcome = "PASS" if result == 0 else f"FAIL exit={result}"
        print(f"{outcome}: {options.name} ({time.monotonic() - started:.1f}s)")
        print(f"Log: {filename}")
    return result if result >= 0 else 128 - result


if __name__ == "__main__":
    sys.exit(main())
