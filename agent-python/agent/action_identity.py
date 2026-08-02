from __future__ import annotations

import hashlib
import json


def action_signature(action: object) -> str:
    encoded = json.dumps(
        {
            "tool_id": getattr(action, "tool_id"),
            "tool_schema_version": getattr(action, "tool_schema_version"),
            "strategy": getattr(action, "strategy"),
            "arguments": getattr(action, "arguments"),
            "target_coverage": getattr(action, "target_coverage"),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()
