# Punjab MODIS / VIIRS FIRMS Downloader

A small, repeatable downloader for NASA FIRMS Standard Processing (SP) active-fire detections over the selected Punjab, India study area. MODIS and VIIRS S-NPP are downloaded and stored separately; the script does not merge the sensors.

## Selected study periods

The four inclusive date windows selected for the initial sample are:

1. 2025-10-20 to 2025-10-31
2. 2025-11-01 to 2025-11-10
3. 2025-11-11 to 2025-11-20
4. 2025-11-21 to 2025-11-30

These windows cover late October and November 2025, a useful initial sampling window for Punjab's post-monsoon rice-residue burning season. They are a proposed sampling design, not a guarantee of detections. The team should review the resulting counts and coverage before treating the sample as representative.

The script expects up to **8 CSV files**: 4 periods x 2 sensors. A period with zero detections is retained as a valid empty-result CSV and marked `WARNING` in `download_summary.csv`.

## Requirements

- Python 3.10+
- A NASA FIRMS MAP_KEY
- Internet access

Install dependencies:

```bash
python -m venv .venv
# Windows PowerShell:
.venv\Scripts\Activate.ps1
# macOS/Linux:
# source .venv/bin/activate
pip install -r requirements.txt
```

## Configure the API key safely

1. Obtain/rotate your own NASA FIRMS MAP_KEY if needed.
2. Copy `.env.example` to a new file named `.env`.
3. Replace the placeholder with your key:

```env
FIRMS_MAP_KEY=put_your_key_here
```

**Never commit `.env`, paste the key into the Python file, or share it in screenshots.** The `.gitignore` excludes `.env`. If a key was previously exposed in a shared file or repository, revoke/regenerate it before using this project.

## Run

Download the configured Punjab periods:

```bash
python firms_download.py
```

Validate existing downloaded CSVs without making API requests:

```bash
python firms_download.py --validate-only
```

Force re-download of the configured periods:

```bash
python firms_download.py --force
```

## Output

```text
Area_1/
  MODIS/
    ...MODIS_SP...csv
  VIIRS_SNPP/
    ...VIIRS_SNPP_SP...csv
_raw_api_responses/
  Area_1/
    MODIS/
    VIIRS_SNPP/
download_summary.csv
```

The script checks the returned sensor/instrument, coordinates, date window, required fields, and exact duplicate rows. Original API response chunks are retained locally for audit. It also checks FIRMS data availability before downloading.

## Team handoff

- **Backend/script owner:** maintain this downloader and make sure the same configuration is used by the team.
- **Data owner:** run the script, inspect `download_summary.csv`, review zero/low-count periods, perform the agreed cleaning/standardization, and publish the final shared dataset and data dictionary.
- **AI owner:** agree with the Data owner on the input schema and use the approved dataset; do not assume the two sensors are interchangeable.

Downloaded data and raw API responses are ignored by Git by default. Share the approved final dataset with the team using the storage location agreed by the team (for example, shared cloud storage or Git LFS if appropriate). Do not commit API credentials.

## Important note

The four dates are a practical initial choice based on the project's Punjab study-area and seasonal focus. Before final analysis, confirm that the requested FIRMS SP products cover these dates and inspect the actual detection counts. The script performs the availability check at runtime and stops if a requested period is outside the product's published availability.
