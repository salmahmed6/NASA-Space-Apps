#!/usr/bin/env python3
"""
NASA Space Apps 2026 - MODIS / VIIRS S-NPP hotspot sample downloader
=====================================================================

Downloads NASA FIRMS Standard Processing hotspot detections for Punjab, India,
using MODIS and VIIRS S-NPP across four selected periods (8 CSV files total),
organised as:

    <OUTPUT_DIR>/
        Area_1/MODIS/        4 CSVs
        Area_1/VIIRS_SNPP/   4 CSVs
        _raw_api_responses/  every API response saved unchanged (audit trail)
        download_summary.csv validation table (paths + record counts)

Rules this script enforces
--------------------------
* Only real data: a CSV is written only when EVERY API request for that
  period succeeded. A failed period produces NO file (never a placeholder).
* MODIS and VIIRS are never mixed: each file is checked for the correct
  `instrument` value.
* Every row is checked to be inside its area's bounding box and its period.
* Invalid rows (missing / impossible values, exact duplicates) are removed
  and counted; the raw responses are kept untouched in _raw_api_responses/.
* A file with 0 detections is a real, valid answer from FIRMS (no fires
  seen) - it is kept but flagged as a WARNING in the summary.

The FIRMS MAP_KEY is set in the MAP_KEY constant below (CONFIGURATION).
Before pushing to GitHub, set MAP_KEY = "" and use an environment variable
FIRMS_MAP_KEY or a `.env` file (FIRMS_MAP_KEY=xxxx) instead.

Usage
-----
    pip install pandas requests
    python firms_download.py                 # download + validate
    python firms_download.py --validate-only # just re-check existing files
    python firms_download.py --force         # re-download even if valid
    python firms_download.py --areas Area_1  # Punjab (only configured area)
"""

from __future__ import annotations

import argparse
import io
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

# =============================================================================
# CONFIGURATION - edit here
# =============================================================================

# Project folder (outputs are created inside it). Default = the folder that
# contains this script, so it works on every team member's machine.
# Can be overridden with --out.
OUTPUT_DIR = Path(__file__).resolve().parent

FIRMS_API_BASE = "https://firms.modaps.eosdis.nasa.gov/api"
MAP_KEY_ENV_VAR = "FIRMS_MAP_KEY"

# Never hard-code a real API key here. Read it from FIRMS_MAP_KEY or .env.
MAP_KEY = ""

# Folder name -> FIRMS source id. "_SP" = Standard Processing (final data).
SENSORS = {
    "MODIS": "MODIS_SP",
    "VIIRS_SNPP": "VIIRS_SNPP_SP",
}
# Value expected in the FIRMS `instrument` column for each sensor folder.
EXPECTED_INSTRUMENT = {
    "MODIS": "MODIS",
    "VIIRS_SNPP": "VIIRS",
}

# Study area: Punjab, India. bbox = (west, south, east, north) in decimal degrees.
# Four inclusive UTC date windows selected to cover the main 2025
# post-monsoon rice-residue burning period. These are analysis windows,
# not a claim that detections exist in every window.
AREAS = [
    {
        "id": "Area_1",
        "name": "Punjab_India",
        "bbox": (73.8, 29.5, 77.0, 32.5),
        "periods": [
            ("2025-10-20", "2025-10-31"),
            ("2025-11-01", "2025-11-10"),
            ("2025-11-11", "2025-11-20"),
            ("2025-11-21", "2025-11-30"),
        ],
    },
]

MAX_DAYS_PER_REQUEST = 5          # FIRMS area API limit (day_range 1..5)
REQUEST_TIMEOUT_S = 120
MAX_RETRIES = 4                   # for network errors / HTTP 429 / 5xx
PAUSE_BETWEEN_REQUESTS_S = 1.0    # be gentle with the 5000-per-10-min quota
BBOX_TOLERANCE_DEG = 1e-6

RAW_DIR_NAME = "_raw_api_responses"
SUMMARY_FILE_NAME = "download_summary.csv"
REQUIRED_COLUMNS = ["latitude", "longitude", "acq_date", "acq_time",
                    "instrument", "confidence"]

# =============================================================================
# Implementation
# =============================================================================

log = logging.getLogger("firms")


class FirmsError(Exception):
    """FIRMS returned something that is not valid hotspot data."""


@dataclass
class PeriodResult:
    area_id: str
    sensor: str
    start: str
    end: str
    path: Path
    status: str = "PENDING"        # OK / WARNING / FAILED / MISSING / SKIPPED
    records: int | None = None
    rejected: dict = field(default_factory=dict)
    message: str = ""


# ---------- helpers ----------------------------------------------------------

def parse_day(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def output_path(out_dir: Path, area: dict, sensor: str, start: str, end: str) -> Path:
    fname = f"{area['id']}_{area['name']}_{SENSORS[sensor]}_{start}_to_{end}.csv"
    return out_dir / area["id"] / sensor / fname


def date_chunks(start: date, end: date, max_days: int):
    """Split an inclusive date range into (chunk_start, n_days) pieces."""
    cur = start
    while cur <= end:
        n = min(max_days, (end - cur).days + 1)
        yield cur, n
        cur += timedelta(days=n)


def load_map_key(out_dir: Path) -> str:
    if MAP_KEY.strip():
        return MAP_KEY.strip()
    key = os.environ.get(MAP_KEY_ENV_VAR, "").strip()
    if key:
        return key
    for env_file in (out_dir / ".env", Path(__file__).resolve().parent / ".env"):
        if env_file.is_file():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith(f"{MAP_KEY_ENV_VAR}="):
                    key = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if key:
                        return key
    raise SystemExit(
        f"ERROR: no FIRMS MAP_KEY found.\n"
        f"Create a file named .env in {out_dir} containing:\n"
        f"    {MAP_KEY_ENV_VAR}=your_key_here\n"
        f"or set the environment variable {MAP_KEY_ENV_VAR}."
    )


def http_get(session: requests.Session, url: str) -> str:
    """GET with retries for transient failures. Returns response text."""
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = session.get(url, timeout=REQUEST_TIMEOUT_S)
            if r.status_code == 429 or r.status_code >= 500:
                raise requests.HTTPError(f"HTTP {r.status_code}")
            r.raise_for_status()
            return r.text
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as e:
            last_err = e
            wait = 5 * attempt
            log.warning("  request failed (%s), retry %d/%d in %ds",
                        e, attempt, MAX_RETRIES, wait)
            time.sleep(wait)
    raise FirmsError(f"request failed after {MAX_RETRIES} attempts: {last_err}")


def safe_url(url: str, key: str) -> str:
    return url.replace(key, "<MAP_KEY>")


# ---------- availability -----------------------------------------------------

def check_availability(session: requests.Session, key: str, areas: list[dict]) -> None:
    """Stop early if any requested period is outside FIRMS SP availability."""
    url = f"{FIRMS_API_BASE}/data_availability/csv/{key}/ALL"
    text = http_get(session, url)
    if "data_id" not in text.splitlines()[0]:
        raise FirmsError(f"unexpected availability response: {text[:200]!r}")
    avail = pd.read_csv(io.StringIO(text), dtype=str).set_index("data_id")
    problems = []
    for sensor, source in SENSORS.items():
        if source not in avail.index:
            problems.append(f"{source} not listed by FIRMS")
            continue
        lo = parse_day(avail.loc[source, "min_date"])
        hi = parse_day(avail.loc[source, "max_date"])
        log.info("Availability %-14s %s -> %s", source, lo, hi)
        for area in areas:
            for s, e in area["periods"]:
                if parse_day(s) < lo or parse_day(e) > hi:
                    problems.append(f"{area['id']} {source} {s}..{e} outside {lo}..{hi}")
    if problems:
        raise SystemExit("ERROR: requested periods not available:\n  " + "\n  ".join(problems))


# ---------- download + validation of one chunk -------------------------------

def fetch_chunk(session, key, sensor, bbox, chunk_start: date, n_days: int,
                raw_dir: Path, area_id: str) -> pd.DataFrame:
    source = SENSORS[sensor]
    bbox_s = ",".join(str(v) for v in bbox)
    url = f"{FIRMS_API_BASE}/area/csv/{key}/{source}/{bbox_s}/{n_days}/{chunk_start.isoformat()}"
    log.info("  GET %s", safe_url(url, key))
    text = http_get(session, url)
    time.sleep(PAUSE_BETWEEN_REQUESTS_S)

    first_line = text.lstrip().splitlines()[0] if text.strip() else ""
    if "latitude" not in first_line:
        # FIRMS reports errors (bad key, bad range, quota...) as plain text.
        raise FirmsError(f"FIRMS error for {source} {chunk_start}: {text.strip()[:200]!r}")

    # keep the untouched response for audit
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_file = raw_dir / f"{area_id}_{source}_{chunk_start.isoformat()}_{n_days}d.csv"
    raw_file.write_text(text, encoding="utf-8")

    # read everything as text so the original formatting is preserved on output
    df = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False)
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise FirmsError(f"{source} {chunk_start}: missing columns {missing}")

    # the response must belong to the requested window (protects against a
    # misunderstanding of the API date semantics)
    if len(df):
        d = pd.to_datetime(df["acq_date"], format="%Y-%m-%d", errors="coerce").dt.date
        lo, hi = chunk_start, chunk_start + timedelta(days=n_days - 1)
        outside = d.notna() & ((d < lo) | (d > hi))
        if outside.any():
            raise FirmsError(
                f"{source} {chunk_start}: {int(outside.sum())} rows dated outside "
                f"{lo}..{hi} (e.g. {df.loc[outside, 'acq_date'].iloc[0]}). Not saving.")
    return df


def clean_rows(df: pd.DataFrame, sensor: str, bbox, start: date, end: date):
    """Remove invalid rows; raise if the data is from the wrong sensor."""
    rejected = {}
    if df.empty:
        return df, rejected

    instr = df["instrument"].str.strip().str.upper()
    wrong = instr.ne(EXPECTED_INSTRUMENT[sensor]) & instr.ne("")
    if wrong.any():
        raise FirmsError(f"{sensor}: found instrument values {sorted(instr[wrong].unique())} "
                         f"- refusing to mix sensors")

    lat = pd.to_numeric(df["latitude"], errors="coerce")
    lon = pd.to_numeric(df["longitude"], errors="coerce")
    t = pd.to_numeric(df["acq_time"], errors="coerce")
    d = pd.to_datetime(df["acq_date"], format="%Y-%m-%d", errors="coerce").dt.date
    w, s, e, n = bbox
    tol = BBOX_TOLERANCE_DEG

    checks = {
        "missing_required_value": df[REQUIRED_COLUMNS].eq("").any(axis=1),
        "bad_coordinates": lat.isna() | lon.isna() | ~lat.between(-90, 90) | ~lon.between(-180, 180),
        "outside_bbox": ~(lat.between(s - tol, n + tol) & lon.between(w - tol, e + tol)),
        "bad_date_or_outside_period": d.isna() | (d < start) | (d > end),
        "bad_acq_time": t.isna() | ~t.between(0, 2359) | (t % 100 >= 60),
    }
    bad = pd.Series(False, index=df.index)
    for reason, mask in checks.items():
        new = mask & ~bad
        if new.any():
            rejected[reason] = int(new.sum())
        bad |= mask
    df = df[~bad]

    dups = df.duplicated()
    if dups.any():
        rejected["exact_duplicate"] = int(dups.sum())
        df = df[~dups]
    return df.reset_index(drop=True), rejected


# ---------- one output file --------------------------------------------------

def process_period(session, key, area, sensor, start_s, end_s, out_dir, force) -> PeriodResult:
    path = output_path(out_dir, area, sensor, start_s, end_s)
    res = PeriodResult(area["id"], sensor, start_s, end_s, path)

    if path.exists() and not force:
        chk = validate_file(path, area, sensor, start_s, end_s)
        if chk.status in ("OK", "WARNING"):
            chk.status, chk.message = "SKIPPED", "already valid (use --force to re-download)"
            log.info("  SKIPPED %s (already valid, %d rows)", path.name, chk.records)
            return chk
        log.info("  existing file invalid (%s) -> re-downloading", chk.message)

    start, end = parse_day(start_s), parse_day(end_s)
    raw_dir = out_dir / RAW_DIR_NAME / area["id"] / sensor
    try:
        parts = [fetch_chunk(session, key, sensor, area["bbox"], cs, nd, raw_dir, area["id"])
                 for cs, nd in date_chunks(start, end, MAX_DAYS_PER_REQUEST)]
        df = pd.concat(parts, ignore_index=True)
        df, rejected = clean_rows(df, sensor, area["bbox"], start, end)
    except FirmsError as e:
        res.status, res.message = "FAILED", str(e)
        log.error("  FAILED: %s", e)
        return res

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    df.to_csv(tmp, index=False)
    os.replace(tmp, path)          # atomic: never leaves a half-written file

    res.records, res.rejected = len(df), rejected
    res.status = "OK" if len(df) else "WARNING"
    res.message = "" if len(df) else "0 detections returned by FIRMS for this period"
    log.info("  saved %s (%d rows, rejected %s)", path.name, len(df), rejected or 0)
    return res


# ---------- final validation -------------------------------------------------

def validate_file(path: Path, area: dict, sensor: str, start_s: str, end_s: str) -> PeriodResult:
    res = PeriodResult(area["id"], sensor, start_s, end_s, path)
    if not path.is_file():
        res.status, res.message = "MISSING", "file not found"
        return res
    try:
        df = pd.read_csv(path, dtype=str, keep_default_na=False)
        missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
        if missing:
            raise FirmsError(f"missing columns {missing}")
        _, rejected = clean_rows(df, sensor, area["bbox"], parse_day(start_s), parse_day(end_s))
        if rejected:
            raise FirmsError(f"contains invalid rows {rejected}")
    except (FirmsError, pd.errors.ParserError, pd.errors.EmptyDataError) as e:
        res.status, res.message = "INVALID", str(e)
        return res
    res.records = len(df)
    res.status = "OK" if len(df) else "WARNING"
    res.message = "" if len(df) else "0 detections"
    return res


def validate_all(areas, out_dir) -> list[PeriodResult]:
    results = []
    for area in areas:
        for sensor in SENSORS:
            for s, e in area["periods"]:
                results.append(validate_file(output_path(out_dir, area, sensor, s, e),
                                             area, sensor, s, e))
    return results


def report(results: list[PeriodResult], out_dir: Path, areas) -> bool:
    rows = [{
        "area": r.area_id, "sensor": r.sensor, "period": f"{r.start}..{r.end}",
        "status": r.status, "records": "" if r.records is None else r.records,
        "path": str(r.path.relative_to(out_dir)), "note": r.message,
    } for r in results]
    table = pd.DataFrame(rows)
    with pd.option_context("display.max_colwidth", 80, "display.width", 200):
        print("\n" + table.drop(columns="path").to_string(index=False))
    table.insert(0, "checked_at_utc", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
    table.to_csv(out_dir / SUMMARY_FILE_NAME, index=False)

    all_ok = True
    print()
    for area in areas:
        for sensor in SENSORS:
            n_ok = sum(1 for r in results if r.area_id == area["id"]
                       and r.sensor == sensor and r.status in ("OK", "WARNING"))
            n_exp = len(area["periods"])
            flag = "OK " if n_ok == n_exp else "!! "
            all_ok &= n_ok == n_exp
            print(f"{flag}{area['id']}/{sensor}: {n_ok}/{n_exp} valid files")
    total = sum(1 for r in results if r.status in ("OK", "WARNING"))
    expected = sum(len(a["periods"]) for a in areas) * len(SENSORS)
    print(f"\nTOTAL: {total}/{expected} valid CSV files. Summary -> {out_dir / SUMMARY_FILE_NAME}")
    return all_ok


# ---------- main -------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Download FIRMS MODIS/VIIRS samples for Punjab, India.")
    ap.add_argument("--out", type=Path, default=OUTPUT_DIR, help="project/output folder")
    ap.add_argument("--areas", nargs="*", help="area ids to process, e.g. Area_1 Area_3")
    ap.add_argument("--force", action="store_true", help="re-download even if files are valid")
    ap.add_argument("--validate-only", action="store_true", help="only check existing files")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    out_dir: Path = args.out
    out_dir.mkdir(parents=True, exist_ok=True)

    areas = [a for a in AREAS if not args.areas or a["id"] in args.areas]
    if not areas:
        raise SystemExit(f"No matching areas. Known: {[a['id'] for a in AREAS]}")

    # always create the folder structure
    for area in areas:
        for sensor in SENSORS:
            (out_dir / area["id"] / sensor).mkdir(parents=True, exist_ok=True)

    if not args.validate_only:
        key = load_map_key(out_dir)
        with requests.Session() as session:
            try:
                check_availability(session, key, areas)
            except FirmsError as e:
                raise SystemExit(f"ERROR: could not check data availability: {e}")
            for area in areas:
                for sensor in SENSORS:
                    for s, e in area["periods"]:
                        log.info("%s %s %s..%s", area["id"], sensor, s, e)
                        process_period(session, key, area, sensor, s, e, out_dir, args.force)

    ok = report(validate_all(areas, out_dir), out_dir, areas)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
