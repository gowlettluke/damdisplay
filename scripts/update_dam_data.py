#!/usr/bin/env python3
"""Collect and normalise current and historical Queensland dam data.

Sources:
- Seqwater current dam-level table
- Seqwater historical Drupal response
- Sunwater historical/current station API
- Sunwater current dams page (browser-rendered validation/fallback)

The script preserves last-good data when a source fails. It intentionally contains
no EAP assessment logic.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from dateutil import parser as date_parser

BRISBANE = ZoneInfo("Australia/Brisbane")
UTC = timezone.utc
ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
HISTORY_DIR = DATA_DIR / "history"
RAW_DIR = DATA_DIR / "raw"

SEQ_CURRENT_URL = "https://www.seqwater.com.au/dam-levels"
SEQ_HISTORY_URL = "https://www.seqwater.com.au/historic-dam-levels"
SUN_DAMS_URL = "https://www.sunwater.com.au/dams/"
SUN_HISTORY_PAGE_URL = "https://www.sunwater.com.au/water-data/historical-dam-capacity/"
SUN_API_ROOT = "https://data.sunwater.com.au"
SEQ_HISTORY_AJAX_LIBS_FALLBACK = (
    "eJx9UktywyAMvZBjn6G77rrqmhGg2mrEpxLEcU5f4iR1puN2w_A-PAmQZbgsg6XUwyecO8eg2jAoPvYBVWFEfeCYfNOSYNtJAKYLdnguTPE4eKkZuL_DbkxpZDQQgZdCToffRJdBYBTIkz7ObkxfY66WSSf0nSKImwxkMlBLcilkxoLDH3zzf83GQzCMJ2QdJtKShBzwobEHN4GU1QQFZXCpxvKeN6J51oMHy8kdN37kZIE3vMWuxa6gvdWm54aMFQTvpAb7LNyvadYKRvADBaPDPYtLXEP8W9F9KRagiLIn_ryM0RoCyLJnwoAytp6WW5Yr_zSC5wzRg22_W9r3mz1PnlLEmcpUqPDuRedFaV7GNeJJpzj6FDbiRDjrS5vX11aTUd5OKEJtLJW8DBYnOFESvcHr0luQpi5aMNyGe424BfUh-cr4DSsINME"
)

USER_AGENT = "QueenslandDamSituationDisplay/1.0 (+GitHub Actions; public data collector)"
REQUEST_TIMEOUT = 60


@dataclass
class SourceHealth:
    source: str
    provider: str
    role: str
    required: bool
    status: str = "not_run"
    records: int = 0
    dams: int = 0
    newest_observation: str | None = None
    message: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    duration_seconds: float | None = None


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def json_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def json_read(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def compact_ws(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def slugify(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    value = value.lower().replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value


def normalise_display_name(raw_name: str) -> str:
    name = compact_ws(raw_name)
    replacements = {
        "Callide Dam Intake": "Callide Dam",
        "Bill Gunn (Lake Dyer)": "Bill Gunn Dam",
        "Lake Macdonald (Six Mile Creek)": "Lake Macdonald Dam",
        "North Pine (Lake Samsonvale)": "North Pine Dam",
        "Sideling Creek (Lake Kurwongbah)": "Sideling Creek Dam",
        "E.J. Beardmore": "E.J. Beardmore Dam",
    }
    if name in replacements:
        return replacements[name]
    name = re.sub(r"\s*\([^)]*\)\s*", " ", name).strip()
    name = re.sub(r"\s+Intake$", "", name, flags=re.I)
    if not re.search(r"\bDam$", name, flags=re.I):
        name += " Dam"
    return name


def dam_id_for(name: str) -> str:
    return slugify(normalise_display_name(name))


def parse_number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and math.isnan(value):
            return None
        return float(value)
    text = compact_ws(value)
    if not text or text.lower() in {"n/a", "na", "null", "none", "-", "—"}:
        return None
    text = text.replace(",", "").replace("ML", "").replace("ml", "").replace("%", "")
    text = re.sub(r"(?<=\d)\s+(?=\d)", "", text)
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    return float(match.group()) if match else None


def parse_datetime(value: Any, *, dayfirst: bool = False, default_tz: ZoneInfo = BRISBANE) -> datetime | None:
    text = compact_ws(value)
    if not text:
        return None
    try:
        dt = date_parser.parse(text, dayfirst=dayfirst)
    except (ValueError, TypeError, OverflowError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=default_tz)
    return dt.astimezone(UTC)


def request_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    })
    return session


def timed_health(source: str, provider: str, role: str, required: bool) -> tuple[SourceHealth, float]:
    started = utc_now()
    return SourceHealth(
        source=source,
        provider=provider,
        role=role,
        required=required,
        started_at=iso(started),
    ), time.monotonic()


def finish_health(health: SourceHealth, started_monotonic: float, status: str, *, records: int = 0,
                  dams: int = 0, newest: datetime | None = None, message: str | None = None) -> SourceHealth:
    health.status = status
    health.records = records
    health.dams = dams
    health.newest_observation = iso(newest)
    health.message = message
    health.finished_at = iso(utc_now())
    health.duration_seconds = round(time.monotonic() - started_monotonic, 3)
    return health


def save_raw(name: str, content: str | bytes) -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    suffix = ".bin" if isinstance(content, bytes) else ".txt"
    path = RAW_DIR / f"{name}{suffix}"
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


def find_table_by_headers(soup: BeautifulSoup, required_headers: Iterable[str]) -> Any | None:
    required = [x.lower() for x in required_headers]
    for table in soup.find_all("table"):
        headers = [compact_ws(x.get_text(" ", strip=True)).lower() for x in table.find_all("th")]
        joined = " | ".join(headers)
        if all(token in joined for token in required):
            return table
    return None


def fetch_seqwater_current(session: requests.Session) -> tuple[list[dict[str, Any]], SourceHealth]:
    health, timer = timed_health("seqwater_current_page", "Seqwater", "current_primary", True)
    try:
        response = session.get(SEQ_CURRENT_URL, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        save_raw("seqwater_current_page", response.text)
        soup = BeautifulSoup(response.text, "html.parser")
        table = find_table_by_headers(soup, ["dam name", "% full", "latest observation"])
        if table is None:
            raise RuntimeError("Could not locate the Seqwater current dam-level table")

        rows: list[dict[str, Any]] = []
        for tr in table.select("tbody tr"):
            cells = tr.find_all("td")
            if len(cells) < 5:
                continue
            values = [compact_ws(cell.get_text(" ", strip=True)) for cell in cells]
            raw_name = values[0]
            raw_name = re.split(r"\s+View\s+|\s+Historical\s+levels", raw_name, maxsplit=1, flags=re.I)[0]
            if not raw_name:
                continue
            dam_name = normalise_display_name(raw_name)
            observation = parse_datetime(values[4], dayfirst=True)
            info = values[5] if len(values) > 5 else ""
            row = {
                "dam_id": dam_id_for(dam_name),
                "dam_name": dam_name,
                "operator": "Seqwater",
                "capacity_percent": parse_number(values[3]),
                "full_supply_volume_ml": parse_number(values[1]),
                "volume_ml": parse_number(values[2]),
                "observed_at": iso(observation),
                "observation_precision": "datetime",
                "information": info or None,
                "source": "seqwater_current_page",
                "source_url": SEQ_CURRENT_URL,
            }
            if row["capacity_percent"] is not None:
                rows.append(row)

        if not rows:
            raise RuntimeError("Seqwater current table was found but no dam rows were parsed")
        newest = max((parse_datetime(r["observed_at"]) for r in rows if r.get("observed_at")), default=None)
        return rows, finish_health(health, timer, "ok", records=len(rows), dams=len(rows), newest=newest)
    except Exception as exc:  # noqa: BLE001
        return [], finish_health(health, timer, "failed", message=str(exc))


def extract_form_value(soup: BeautifulSoup, name: str) -> str | None:
    node = soup.find(attrs={"name": name})
    if node and node.get("value") is not None:
        return str(node.get("value"))
    return None


def recursively_find_historical(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        if isinstance(value.get("data"), list) and isinstance(value.get("series"), list):
            return value
        for child in value.values():
            result = recursively_find_historical(child)
            if result is not None:
                return result
    elif isinstance(value, list):
        for child in value:
            result = recursively_find_historical(child)
            if result is not None:
                return result
    return None


def fetch_seqwater_history(session: requests.Session, history_days: int) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any] | None, SourceHealth]:
    health, timer = timed_health("seqwater_history", "Seqwater", "history_primary", True)
    try:
        page = session.get(SEQ_HISTORY_URL, timeout=REQUEST_TIMEOUT)
        page.raise_for_status()
        save_raw("seqwater_history_page", page.text)
        soup = BeautifulSoup(page.text, "html.parser")
        form_build_id = extract_form_value(soup, "form_build_id")
        if not form_build_id:
            match = re.search(r'name=["\']form_build_id["\'][^>]*value=["\']([^"\']+)', page.text)
            form_build_id = match.group(1) if match else None
        if not form_build_id:
            raise RuntimeError("Seqwater Drupal form_build_id was not found")

        today_brisbane = datetime.now(BRISBANE).date()
        start_date = today_brisbane - timedelta(days=history_days)
        body: dict[str, str] = {
            "start_date": start_date.isoformat(),
            "end_date": today_brisbane.isoformat(),
            "form_build_id": form_build_id,
            "form_id": "historical_dam_storage_form",
            "_triggering_element_name": "start_date",
            "_drupal_ajax": "1",
            "ajax_page_state[theme]": "seqwater",
            "ajax_page_state[theme_token]": "",
        }
        libraries = extract_form_value(soup, "ajax_page_state[libraries]")
        if not libraries:
            match = re.search(r'ajax_page_state\[libraries\]["\']?\s*[:=]\s*["\']([^"\']+)', page.text)
            libraries = match.group(1) if match else None
        body["ajax_page_state[libraries]"] = libraries or SEQ_HISTORY_AJAX_LIBS_FALLBACK

        response = session.post(
            SEQ_HISTORY_URL,
            params={"ajax_form": "1", "_wrapper_format": "drupal_ajax"},
            data=body,
            headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": SEQ_HISTORY_URL,
            },
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        save_raw("seqwater_history_response", response.text)
        payload = response.json()
        historical = recursively_find_historical(payload)
        if historical is None:
            raise RuntimeError("Seqwater historical data payload was not found in the Drupal response")

        data_records = historical.get("data") or []
        series_records = historical.get("series") or []
        if not data_records:
            raise RuntimeError("Seqwater historical payload contained no observations")

        histories: dict[str, list[dict[str, Any]]] = defaultdict(list)
        grid_history: list[dict[str, Any]] = []
        definitions: list[tuple[str, str, str | None, str]] = []
        for meta in series_records:
            field = compact_ws(meta.get("y"))
            if not field:
                continue
            name = compact_ws(meta.get("name")) or ("SEQ Water Grid" if field == "g" else f"Seqwater {field}")
            if field.startswith("p") and field[1:].isdigit():
                volume_field = "l" + field[1:]
                definitions.append((field, name, volume_field, "dam"))
            elif field == "g":
                definitions.append((field, "SEQ Water Grid", "l", "grid"))

        if not definitions:
            raise RuntimeError("Seqwater historical series metadata contained no dam percentage fields")

        newest: datetime | None = None
        total = 0
        for field, raw_name, volume_field, kind in definitions:
            display_name = "SEQ Water Grid" if kind == "grid" else normalise_display_name(raw_name)
            did = "seq-water-grid" if kind == "grid" else dam_id_for(display_name)
            for item in data_records:
                observation_date_text = compact_ws(item.get("ds"))
                try:
                    obs_date = date_parser.parse(observation_date_text, dayfirst=True).date()
                except (ValueError, TypeError, OverflowError):
                    continue
                capacity = parse_number(item.get(field))
                volume = parse_number(item.get(volume_field)) if volume_field else None
                if capacity is None and volume is None:
                    continue
                observed_local = datetime.combine(obs_date, datetime.min.time(), tzinfo=BRISBANE)
                observed_utc = observed_local.astimezone(UTC)
                newest = observed_utc if newest is None or observed_utc > newest else newest
                row = {
                    "dam_id": did,
                    "dam_name": display_name,
                    "operator": "Seqwater",
                    "observed_at": iso(observed_utc),
                    "observation_date": obs_date.isoformat(),
                    "observation_precision": "date",
                    "capacity_percent": capacity,
                    "volume_ml": volume,
                    "storage_level_m": None,
                    "rainfall_mm": None,
                    "flow_cms": None,
                    "total_flow_ml": None,
                    "river_level_m": None,
                    "source": "seqwater_history",
                    "source_url": SEQ_HISTORY_URL,
                }
                (grid_history if kind == "grid" else histories[did]).append(row)
                total += 1

        grid_summary = None
        if grid_history:
            grid_history.sort(key=lambda x: x["observed_at"] or "")
            grid_summary = {
                "current": grid_history[-1],
                "history": grid_history,
            }
        return dict(histories), grid_summary, finish_health(
            health, timer, "ok", records=total, dams=len(histories), newest=newest
        )
    except Exception as exc:  # noqa: BLE001
        return {}, None, finish_health(health, timer, "failed", message=str(exc))


def discover_sunwater_sites(session: requests.Session) -> tuple[list[dict[str, str]], str]:
    response = session.get(SUN_HISTORY_PAGE_URL, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    save_raw("sunwater_history_page", response.text)
    soup = BeautifulSoup(response.text, "html.parser")
    sites: list[dict[str, str]] = []
    seen: set[str] = set()
    for option in soup.find_all("option"):
        code = compact_ws(option.get("value"))
        name = compact_ws(option.get_text(" ", strip=True))
        if not code or code == "#" or not name or name.lower() == "dam" or "dam" not in name.lower():
            continue
        if code in seen:
            continue
        seen.add(code)
        sites.append({"station_code": code, "dam_name": normalise_display_name(name)})
    if not sites:
        raise RuntimeError("No Sunwater station codes were discovered from the historical selector")
    return sites, response.text


def fetch_sunwater_api(session: requests.Session, history_days: int) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]], SourceHealth]:
    health, timer = timed_health("sunwater_api", "Sunwater", "current_and_history_primary", True)
    try:
        sites, _ = discover_sunwater_sites(session)
        start_local = datetime.now(BRISBANE) - timedelta(days=history_days)
        start_utc = start_local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
        start_text = start_utc.isoformat(timespec="milliseconds").replace("+00:00", "Z")

        histories: dict[str, list[dict[str, Any]]] = defaultdict(list)
        current: list[dict[str, Any]] = []
        newest: datetime | None = None
        errors: list[str] = []

        for site in sites:
            code = site["station_code"]
            fallback_name = site["dam_name"]
            token = 1
            page_count = 0
            site_rows: list[dict[str, Any]] = []
            seen_tokens: set[int] = set()
            try:
                while token and token not in seen_tokens and page_count < 100:
                    seen_tokens.add(token)
                    page_count += 1
                    response = session.get(
                        f"{SUN_API_ROOT}/api/Sites/{code}/data",
                        params={"startDate": start_text, "continuationToken": str(token)},
                        headers={"Accept": "application/json"},
                        timeout=REQUEST_TIMEOUT,
                    )
                    response.raise_for_status()
                    payload = response.json()
                    values = payload.get("value") or []
                    for raw in values:
                        observed = parse_datetime(raw.get("date"), default_tz=BRISBANE)
                        if not observed:
                            continue
                        api_name = compact_ws(raw.get("shortName")) or fallback_name
                        dam_name = normalise_display_name(api_name)
                        did = dam_id_for(dam_name)
                        row = {
                            "dam_id": did,
                            "dam_name": dam_name,
                            "operator": "Sunwater",
                            "station_code": code,
                            "site_id": raw.get("siteId"),
                            "observed_at": iso(observed),
                            "observation_date": observed.astimezone(BRISBANE).date().isoformat(),
                            "observation_precision": "datetime",
                            "capacity_percent": parse_number(raw.get("percentageFull")),
                            "volume_ml": parse_number(raw.get("volumeMegaLitres")),
                            "storage_level_m": parse_number(raw.get("storageLevelMetres")),
                            "rainfall_mm": parse_number(raw.get("rainfallMillimetres")),
                            "flow_cms": parse_number(raw.get("cubicMetersPerSecond")),
                            "total_flow_ml": parse_number(raw.get("totalFlowMegaLitres")),
                            "river_level_m": parse_number(raw.get("riverLevelMetres")),
                            "source": "sunwater_api",
                            "source_url": f"{SUN_API_ROOT}/api/Sites/{code}/data",
                        }
                        if row["capacity_percent"] is not None or row["volume_ml"] is not None:
                            site_rows.append(row)
                            newest = observed if newest is None or observed > newest else newest
                    next_token = payload.get("continuationToken")
                    try:
                        next_token = int(next_token)
                    except (TypeError, ValueError):
                        next_token = 0
                    token = next_token if next_token > 0 and next_token not in seen_tokens else 0
                if not site_rows:
                    errors.append(f"{fallback_name}: no observations")
                    continue
                site_rows.sort(key=lambda x: x["observed_at"] or "")
                did = site_rows[-1]["dam_id"]
                histories[did].extend(site_rows)
                latest = dict(site_rows[-1])
                latest.update({
                    "information": None,
                    "full_supply_volume_ml": derive_full_supply_volume(
                        latest.get("volume_ml"), latest.get("capacity_percent")
                    ),
                    "full_supply_volume_basis": "derived_from_current_volume_and_percentage",
                })
                current.append(latest)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{fallback_name}: {exc}")

        if not current:
            raise RuntimeError("No Sunwater API dam observations were collected" + (": " + "; ".join(errors[:4]) if errors else ""))
        status = "partial" if errors else "ok"
        message = "; ".join(errors[:8]) if errors else None
        total_records = sum(len(v) for v in histories.values())
        return dict(histories), current, finish_health(
            health, timer, status, records=total_records, dams=len(current), newest=newest, message=message
        )
    except Exception as exc:  # noqa: BLE001
        return {}, [], finish_health(health, timer, "failed", message=str(exc))


def derive_full_supply_volume(volume_ml: float | None, capacity_percent: float | None) -> float | None:
    if volume_ml is None or capacity_percent is None or capacity_percent <= 0:
        return None
    return round(volume_ml / (capacity_percent / 100.0), 3)


def fetch_sunwater_current_page(skip_browser: bool) -> tuple[list[dict[str, Any]], SourceHealth]:
    health, timer = timed_health("sunwater_current_page", "Sunwater", "current_validation_fallback", False)
    if skip_browser:
        return [], finish_health(health, timer, "skipped", message="Browser collection was disabled")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return [], finish_health(health, timer, "unavailable", message="Playwright is not installed")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(user_agent=USER_AGENT, viewport={"width": 1600, "height": 1200})
            page.goto(SUN_DAMS_URL, wait_until="networkidle", timeout=90000)
            page.wait_for_timeout(2500)
            html = page.content()
            save_raw("sunwater_current_rendered", html)
            rows = page.evaluate(
                r"""
                () => {
                  const clean = (v) => (v || '').replace(/\s+/g, ' ').trim();
                  const results = [];
                  const capacities = Array.from(document.querySelectorAll('.capacity-percentage'));
                  for (const capacityNode of capacities) {
                    let container = capacityNode;
                    for (let i = 0; i < 10 && container; i++, container = container.parentElement) {
                      if (!container.querySelector) continue;
                      const heading = container.querySelector('h2');
                      const capacity = container.querySelector('.capacity-percentage');
                      if (heading && capacity) {
                        const capText = clean(capacity.textContent);
                        if (!/%|\d/.test(capText)) break;
                        results.push({
                          dam_name: clean(heading.textContent),
                          capacity_text: capText,
                          location: clean((container.querySelector('.location.databroker-field') || {}).textContent),
                          scheme: clean((container.querySelector('.scheme') || {}).textContent)
                        });
                        break;
                      }
                    }
                  }
                  return results;
                }
                """
            )
            browser.close()

        parsed: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in rows or []:
            capacity = parse_number(raw.get("capacity_text"))
            name = compact_ws(raw.get("dam_name"))
            if capacity is None or not name:
                continue
            dam_name = normalise_display_name(name)
            did = dam_id_for(dam_name)
            if did in seen:
                continue
            seen.add(did)
            parsed.append({
                "dam_id": did,
                "dam_name": dam_name,
                "operator": "Sunwater",
                "capacity_percent": capacity,
                "observed_at": None,
                "observation_precision": "collection_time_only",
                "location": compact_ws(raw.get("location")) or None,
                "scheme": compact_ws(raw.get("scheme")) or None,
                "source": "sunwater_current_page",
                "source_url": SUN_DAMS_URL,
            })
        if not parsed:
            raise RuntimeError("The rendered Sunwater current page contained no parseable dam capacities")
        return parsed, finish_health(health, timer, "ok", records=len(parsed), dams=len(parsed))
    except Exception as exc:  # noqa: BLE001
        return [], finish_health(health, timer, "failed", message=str(exc))


def history_key(row: dict[str, Any]) -> str:
    return "|".join([
        str(row.get("source") or ""),
        str(row.get("observed_at") or row.get("observation_date") or ""),
        str(row.get("capacity_percent") if row.get("capacity_percent") is not None else ""),
        str(row.get("volume_ml") if row.get("volume_ml") is not None else ""),
    ])


def merge_history(dam_id: str, incoming: list[dict[str, Any]], retention_days: int) -> list[dict[str, Any]]:
    path = HISTORY_DIR / f"{dam_id}.json"
    previous_doc = json_read(path, {})
    previous = previous_doc.get("observations", []) if isinstance(previous_doc, dict) else []
    merged: dict[str, dict[str, Any]] = {}
    for row in [*previous, *incoming]:
        merged[history_key(row)] = row
    cutoff = utc_now() - timedelta(days=retention_days)
    output = []
    for row in merged.values():
        dt = parse_datetime(row.get("observed_at"))
        if dt and dt < cutoff:
            continue
        output.append(row)
    output.sort(key=lambda x: x.get("observed_at") or x.get("observation_date") or "")
    json_write(path, {
        "dam_id": dam_id,
        "generated_at": iso(utc_now()),
        "observations": output,
    })
    return output


def nearest_prior(history: list[dict[str, Any]], current_time: datetime, delta: timedelta) -> dict[str, Any] | None:
    target = current_time - delta
    candidates: list[tuple[float, dict[str, Any]]] = []
    for row in history:
        dt = parse_datetime(row.get("observed_at"))
        value = row.get("capacity_percent")
        if not dt or value is None or dt > current_time:
            continue
        distance = abs((dt - target).total_seconds())
        candidates.append((distance, row))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    tolerance = max(delta.total_seconds() * 0.45, 18 * 3600)
    return candidates[0][1] if candidates[0][0] <= tolerance else None


def calculate_changes(current: dict[str, Any], history: list[dict[str, Any]]) -> dict[str, Any]:
    current_value = current.get("capacity_percent")
    current_time = parse_datetime(current.get("observed_at")) or utc_now()
    result: dict[str, Any] = {}
    for label, delta in [("24h", timedelta(hours=24)), ("7d", timedelta(days=7)), ("30d", timedelta(days=30))]:
        prior = nearest_prior(history, current_time, delta)
        prior_value = prior.get("capacity_percent") if prior else None
        change = round(current_value - prior_value, 3) if current_value is not None and prior_value is not None else None
        result[f"change_{label}_percentage_points"] = change
        result[f"comparison_{label}_at"] = prior.get("observed_at") if prior else None
    seven = result.get("change_7d_percentage_points")
    if seven is None:
        trend = "unknown"
    elif seven > 0.05:
        trend = "rising"
    elif seven < -0.05:
        trend = "falling"
    else:
        trend = "stable"
    result["trend_7d"] = trend
    return result


def latest_previous_current() -> dict[str, dict[str, Any]]:
    doc = json_read(DATA_DIR / "dams_current.json", {})
    return {row.get("dam_id"): row for row in doc.get("dams", []) if row.get("dam_id")}


def select_current_records(
    seq_current: list[dict[str, Any]],
    seq_histories: dict[str, list[dict[str, Any]]],
    sun_api_current: list[dict[str, Any]],
    sun_page_current: list[dict[str, Any]],
    previous_current: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}

    # Seqwater current page is primary; the latest daily history fills missing dams.
    for row in seq_current:
        selected[row["dam_id"]] = dict(row)
    for did, history in seq_histories.items():
        if did not in selected and history:
            fallback = dict(history[-1])
            fallback.update({
                "information": None,
                "full_supply_volume_ml": derive_full_supply_volume(
                    fallback.get("volume_ml"), fallback.get("capacity_percent")
                ),
                "full_supply_volume_basis": "derived_from_historical_volume_and_percentage",
                "current_source_status": "historical_fallback",
            })
            selected[did] = fallback

    # Sunwater API is primary. Rendered page is validation and fallback.
    sun_page_map = {row["dam_id"]: row for row in sun_page_current}
    for row in sun_api_current:
        output = dict(row)
        page_row = sun_page_map.get(row["dam_id"])
        if page_row:
            page_pct = page_row.get("capacity_percent")
            api_pct = row.get("capacity_percent")
            output["secondary_capacity_percent"] = page_pct
            output["secondary_source"] = "sunwater_current_page"
            output["source_difference_percentage_points"] = (
                round(api_pct - page_pct, 3) if api_pct is not None and page_pct is not None else None
            )
            output["location"] = page_row.get("location")
            output["scheme"] = page_row.get("scheme")
        selected[row["dam_id"]] = output
    for did, page_row in sun_page_map.items():
        if did not in selected:
            output = dict(page_row)
            output.update({
                "volume_ml": None,
                "full_supply_volume_ml": None,
                "storage_level_m": None,
                "rainfall_mm": None,
                "flow_cms": None,
                "total_flow_ml": None,
                "river_level_m": None,
                "information": "Sunwater API unavailable; capacity is from the current dams page and has no provider observation timestamp.",
                "current_source_status": "current_page_fallback",
            })
            selected[did] = output

    # Preserve last-good records for dams missing from this run.
    for did, prior in previous_current.items():
        if did not in selected:
            output = dict(prior)
            output["current_source_status"] = "last_good_preserved"
            output.setdefault("quality_flags", [])
            if "last_good_preserved" not in output["quality_flags"]:
                output["quality_flags"].append("last_good_preserved")
            selected[did] = output

    return list(selected.values())


def calculate_data_age(row: dict[str, Any], generated: datetime) -> tuple[float | None, str]:
    observed = parse_datetime(row.get("observed_at"))
    if not observed:
        return None, "timestamp_unavailable"
    age_minutes = max(0.0, (generated - observed).total_seconds() / 60.0)
    if age_minutes <= 180:
        status = "current"
    elif age_minutes <= 24 * 60:
        status = "delayed"
    else:
        status = "stale"
    return round(age_minutes, 1), status


def build_summaries(dams: list[dict[str, Any]]) -> dict[str, Any]:
    valid = [d for d in dams if d.get("capacity_percent") is not None]
    percentages = sorted(float(d["capacity_percent"]) for d in valid)
    median = None
    if percentages:
        mid = len(percentages) // 2
        median = percentages[mid] if len(percentages) % 2 else (percentages[mid - 1] + percentages[mid]) / 2
    total_volume = sum(float(d["volume_ml"]) for d in valid if d.get("volume_ml") is not None)
    rising = sum(d.get("trend_7d") == "rising" for d in valid)
    falling = sum(d.get("trend_7d") == "falling" for d in valid)
    stable = sum(d.get("trend_7d") == "stable" for d in valid)
    above_full = sum(float(d["capacity_percent"]) >= 100 for d in valid)
    bands = {
        "0_25": sum(float(d["capacity_percent"]) < 25 for d in valid),
        "25_50": sum(25 <= float(d["capacity_percent"]) < 50 for d in valid),
        "50_75": sum(50 <= float(d["capacity_percent"]) < 75 for d in valid),
        "75_100": sum(75 <= float(d["capacity_percent"]) < 100 for d in valid),
        "100_plus": above_full,
    }
    by_operator: dict[str, Any] = {}
    for operator in ("Seqwater", "Sunwater"):
        rows = [d for d in valid if d.get("operator") == operator]
        operator_pcts = [float(d["capacity_percent"]) for d in rows]
        by_operator[operator] = {
            "dams_reporting": len(rows),
            "median_capacity_percent": round(sorted(operator_pcts)[len(operator_pcts) // 2], 2) if operator_pcts else None,
            "stored_volume_ml": round(sum(float(d["volume_ml"]) for d in rows if d.get("volume_ml") is not None), 2),
            "rising_7d": sum(d.get("trend_7d") == "rising" for d in rows),
            "falling_7d": sum(d.get("trend_7d") == "falling" for d in rows),
        }
    return {
        "dams_reporting": len(valid),
        "median_capacity_percent": round(median, 2) if median is not None else None,
        "stored_volume_ml": round(total_volume, 2),
        "rising_7d": rising,
        "falling_7d": falling,
        "stable_7d": stable,
        "at_or_above_100_percent": above_full,
        "capacity_bands": bands,
        "by_operator": by_operator,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--history-days", type=int, default=400)
    parser.add_argument("--retention-days", type=int, default=730)
    parser.add_argument("--skip-sunwater-browser", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    session = request_session()
    generated = utc_now()

    seq_current, h_seq_current = fetch_seqwater_current(session)
    seq_histories, grid_summary, h_seq_history = fetch_seqwater_history(session, args.history_days)
    sun_histories, sun_api_current, h_sun_api = fetch_sunwater_api(session, args.history_days)
    sun_page_current, h_sun_page = fetch_sunwater_current_page(args.skip_sunwater_browser)
    health = [h_seq_current, h_seq_history, h_sun_api, h_sun_page]

    previous_current = latest_previous_current()
    current_records = select_current_records(
        seq_current,
        seq_histories,
        sun_api_current,
        sun_page_current,
        previous_current,
    )

    combined_incoming: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for did, rows in seq_histories.items():
        combined_incoming[did].extend(rows)
    for did, rows in sun_histories.items():
        combined_incoming[did].extend(rows)

    output_dams: list[dict[str, Any]] = []
    for current in current_records:
        did = current["dam_id"]
        history = merge_history(did, combined_incoming.get(did, []), args.retention_days)
        current.update(calculate_changes(current, history))
        age, freshness = calculate_data_age(current, generated)
        current["data_age_minutes"] = age
        current["freshness_status"] = freshness
        current.setdefault("quality_flags", [])
        difference = current.get("source_difference_percentage_points")
        if difference is not None:
            abs_diff = abs(float(difference))
            if abs_diff > 1.0:
                current["quality_flags"].append("current_source_discrepancy_over_1_percentage_point")
            elif abs_diff > 0.2:
                current["quality_flags"].append("minor_current_source_difference")
        if freshness == "stale":
            current["quality_flags"].append("stale_observation")
        current["history_30d"] = [
            {
                "observed_at": row.get("observed_at"),
                "capacity_percent": row.get("capacity_percent"),
                "volume_ml": row.get("volume_ml"),
                "storage_level_m": row.get("storage_level_m"),
            }
            for row in history[-90:]
        ]
        output_dams.append(current)

    output_dams.sort(key=lambda x: (x.get("operator") or "", x.get("dam_name") or ""))
    summaries = build_summaries(output_dams)

    required_failures = [asdict(h) for h in health if h.required and h.status == "failed"]
    health_doc = {
        "generated_at": iso(generated),
        "overall_status": "failed" if required_failures else ("degraded" if any(h.status in {"failed", "partial", "unavailable"} for h in health) else "ok"),
        "required_source_failure_count": len(required_failures),
        "sources": [asdict(h) for h in health],
    }
    json_write(DATA_DIR / "provider_health.json", health_doc)

    current_doc = {
        "generated_at": iso(generated),
        "timezone": "Australia/Brisbane",
        "interpretation_notes": [
            "Operator readings are automated public data and may be unverified.",
            "Values above 100% are retained as published.",
            "Capacity percentages can reflect temporary or reduced operating full-supply levels.",
            "Queensland-wide capacity is shown as a median, not a simple or volume-weighted average across unlike storages.",
        ],
        "summaries": summaries,
        "seqwater_grid": grid_summary,
        "dams": output_dams,
    }
    json_write(DATA_DIR / "dams_current.json", current_doc)

    manifest = {
        "generated_at": iso(generated),
        "dams_published": len(output_dams),
        "history_files": len(list(HISTORY_DIR.glob("*.json"))),
        "provider_health": health_doc["overall_status"],
        "required_source_failure_count": len(required_failures),
        "source_urls": {
            "seqwater_current": SEQ_CURRENT_URL,
            "seqwater_history": SEQ_HISTORY_URL,
            "sunwater_current": SUN_DAMS_URL,
            "sunwater_history": SUN_HISTORY_PAGE_URL,
            "sunwater_api": SUN_API_ROOT,
        },
    }
    json_write(DATA_DIR / "run_manifest.json", manifest)

    if args.verbose:
        print(json.dumps(manifest, indent=2))
        for item in health:
            print(f"{item.source}: {item.status} ({item.records} records, {item.dams} dams) {item.message or ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
