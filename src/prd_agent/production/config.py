from __future__ import annotations

import os
from pathlib import Path


def secret_or_environment(name: str) -> str:
    file_name = os.environ.get(f"{name}_FILE")
    if file_name:
        value = Path(file_name).read_text(encoding="utf-8").strip()
    else:
        value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} or {name}_FILE is required")
    return value
