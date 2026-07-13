# Queensland Dam Situation Display

A standalone GitHub Pages product for current and historical dam-storage information published by **Seqwater** and **Sunwater**.

This project intentionally contains **no EAP assessment logic**.

## What it includes

- Seqwater current dam-level table
- Seqwater daily historical storage series
- Sunwater station API current and historical measurements
- Sunwater rendered dams page as an independent current-capacity check and fallback
- Last-good-data preservation when one source fails
- Source-health reporting
- Current dashboard, searchable dam table, trend views and per-dam history drawer
- Fullscreen operations-centre wall display with automatic scene rotation
- GitHub Actions collection, data commit and Pages deployment

## Data outputs

```text
data/dams_current.json       Canonical current records, summaries and short history
data/history/<dam_id>.json   Retained observation history for each dam
data/provider_health.json    Independent status for each upstream source
data/run_manifest.json       Latest run summary
data/raw/                     Latest raw responses for diagnosis
```

## Source precedence

### Seqwater

1. Current dam-level page
2. Latest historical daily observation
3. Last-good published record

### Sunwater

1. Timestamped station API observation
2. Rendered current dams page
3. Last-good published record

The Sunwater webpage value is also compared with the API capacity. A difference above one percentage point is flagged in the data record.

## Important interpretation

- Operator values are automated public readings and may not be verified.
- Values above 100% are retained as published.
- Temporary or reduced full-supply operating levels may affect the meaning of percentage full.
- The statewide headline uses the **median dam percentage**, not a simple average of unlike storages.
- Sunwater full-supply volume is derived from current volume and percentage only when both are available; the basis is labelled in the record.
- Seqwater historical data is daily and represents the final observation of each day, so it may not capture event peaks.

## First setup

1. Create a new GitHub repository.
2. Copy this project into the repository root.
3. In **Settings → Actions → General**, enable **Read and write permissions**.
4. In **Settings → Pages**, choose **GitHub Actions** as the source.
5. Run **Actions → Update Queensland dam data → Run workflow**.

The workflow runs hourly at 17 minutes past the hour.

## Local collector test

From the project directory:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
py -m pip install -r scripts\requirements.txt
py -m playwright install chromium
py scripts\update_dam_data.py --history-days 400 --verbose
py scripts\validate_data.py
```

To omit the browser-rendered Sunwater fallback during a quick local test:

```powershell
py scripts\update_dam_data.py --history-days 400 --skip-sunwater-browser --verbose
```

## Local frontend test

Browsers block JSON requests when `index.html` is opened directly. Run:

```powershell
py -m http.server 8000
```

Then open:

```text
http://localhost:8000
```

Direct wall-display launch:

```text
http://localhost:8000/?wall=1
```

Wall controls:

- `Space` — pause or resume
- `Left` / `Right` — previous or next scene
- `F` — fullscreen
- `Esc` — exit presentation mode

## Expected maintenance

The collectors deliberately discover Seqwater Drupal form values and Sunwater station codes at runtime. If either provider changes its webpage structure or API contract, `data/provider_health.json` records the failure while previously successful dam records remain published.
