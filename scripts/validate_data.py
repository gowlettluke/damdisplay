#!/usr/bin/env python3
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
current = json.loads((root / "data" / "dams_current.json").read_text(encoding="utf-8"))
health = json.loads((root / "data" / "provider_health.json").read_text(encoding="utf-8"))
catalogue = json.loads((root / "data" / "dams.json").read_text(encoding="utf-8"))
errors = []

catalogue_rows = catalogue.get("dams", [])
if not isinstance(catalogue_rows, list) or not catalogue_rows:
    errors.append("data/dams.json contains no dams")

catalogue_ids = set()
station_codes = set()
for dam in catalogue_rows:
    did = dam.get("id") or dam.get("slug")
    if not did:
        errors.append("Catalogue dam without id/slug")
        continue
    if did in catalogue_ids:
        errors.append(f"Duplicate catalogue dam id: {did}")
    catalogue_ids.add(did)
    if dam.get("operator") not in {"Seqwater", "Sunwater"}:
        errors.append(f"Unknown catalogue operator for {did}: {dam.get('operator')}")
    lat = dam.get("latitude")
    lon = dam.get("longitude")
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        errors.append(f"Missing/non-numeric coordinates for catalogue dam {did}")
    code = dam.get("station_code")
    if code:
        if code in station_codes:
            errors.append(f"Duplicate Sunwater station_code: {code}")
        station_codes.add(code)

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
    if did and did not in catalogue_ids:
        errors.append(f"Published dam is not in data/dams.json: {did}")
    if did in catalogue_ids:
        if not isinstance(dam.get("latitude"), (int, float)) or not isinstance(dam.get("longitude"), (int, float)):
            errors.append(f"Published dam lacks catalogue coordinates: {did}")

missing = sorted(catalogue_ids - ids)
if missing:
    errors.append("Catalogue dams missing from dams_current.json: " + ", ".join(missing))

if not current.get("generated_at"):
    errors.append("Missing generated_at")
if "sources" not in health:
    errors.append("Provider health has no sources")
if errors:
    print("\n".join(errors))
    raise SystemExit(1)
print(
    f"Validated {len(ids)} published dam records against {len(catalogue_ids)} catalogue dams "
    f"({len(station_codes)} Sunwater station codes)."
)
