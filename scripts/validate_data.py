#!/usr/bin/env python3
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
current = json.loads((root / "data" / "dams_current.json").read_text(encoding="utf-8"))
health = json.loads((root / "data" / "provider_health.json").read_text(encoding="utf-8"))
errors = []
ids = set()
for dam in current.get("dams", []):
    did = dam.get("dam_id")
    if not did:
        errors.append("Dam without dam_id")
    elif did in ids:
        errors.append(f"Duplicate dam_id: {did}")
    ids.add(did)
    pct = dam.get("capacity_percent")
    if pct is not None and not isinstance(pct, (int, float)):
        errors.append(f"Non-numeric capacity_percent for {did}")
    if dam.get("operator") not in {"Seqwater", "Sunwater"}:
        errors.append(f"Unknown operator for {did}: {dam.get('operator')}")
if not current.get("generated_at"):
    errors.append("Missing generated_at")
if "sources" not in health:
    errors.append("Provider health has no sources")
if errors:
    print("\n".join(errors))
    raise SystemExit(1)
print(f"Validated {len(ids)} dam records.")
