"""Чтение/запись JSON-файлов состояния (атомарно, UTF-8, стабильный порядок ключей)."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def read_json(path: Path, default):
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def write_json(path: Path, data) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)
