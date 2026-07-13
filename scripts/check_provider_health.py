#!/usr/bin/env python3
import json
from pathlib import Path

path = Path(__file__).resolve().parents[1] / "data" / "provider_health.json"
doc = json.loads(path.read_text(encoding="utf-8"))
failures = [s for s in doc.get("sources", []) if s.get("required") and s.get("status") == "failed"]
if failures:
    for source in failures:
        print(f"Required source failed: {source.get('source')}: {source.get('message')}")
    raise SystemExit(1)
print("Required dam data sources are healthy.")
