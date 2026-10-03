#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Выгрузка OpenAPI-схемы API в JSON — из неё фронтенд генерирует TypeScript-типы (npm run gen:api).

    PYTHONPATH=src uv run python -m scripts.export_openapi frontend/openapi.json   # из корня проекта

Сервер запускать не нужно: схема строится из кода приложения. В проде /api/openapi.json выключен
(DOCS_ENABLED=false), а эта выгрузка от него не зависит.
"""

import json
import sys
from pathlib import Path

from main import app


def main() -> None:
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "openapi.json")
    target.write_text(json.dumps(app.openapi(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"OpenAPI schema written to {target}")


if __name__ == "__main__":
    main()
