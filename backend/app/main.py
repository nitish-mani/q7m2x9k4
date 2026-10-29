from __future__ import annotations

import asyncio
import logging
import os
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("wind-forecast-intelligence")
BASE_URL = os.getenv("ELEXON_BASE_URL", "https://data.elexon.co.uk/bmrs/api/v1")
UK = ZoneInfo("Europe/London")
app = FastAPI(title="Wind Forecast Intelligence API", version="1.0.0", description="January 2026 wind forecast vintage analysis using Elexon WINDFOR and FUELHH only.")
origins = [x.strip() for x in os.getenv("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",") if x.strip()]
app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=False, allow_methods=["GET"], allow_headers=["*"])


def parse_dt(value: Any) -> pd.Timestamp:
    ts = pd.to_datetime(value, utc=True, errors="coerce")
    return ts


async def fetch_stream(client: httpx.AsyncClient, dataset: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Fetch rows using the dataset-specific Elexon stream filters.

    FUELHH is filtered by settlement date. WINDFOR is filtered by forecast
    publication time so we retain the forecast vintages needed for a historical
    day-ahead backtest. The API's date filters are not interchangeable.
    """
    rows: list[dict[str, Any]] = []
    cursor = start
    while cursor < end:
        window_end = min(cursor + timedelta(days=7), end)
        url = f"{BASE_URL}/datasets/{dataset}/stream"
        if dataset == "FUELHH":
            # Settlement-date bounds are inclusive; use non-overlapping chunks.
            params = {
                "settlementDateFrom": cursor.date().isoformat(),
                "settlementDateTo": (window_end - timedelta(days=1)).date().isoformat(),
            }
        elif dataset == "WINDFOR":
            params = {
                "publishDateTimeFrom": cursor.isoformat().replace("+00:00", "Z"),
                "publishDateTimeTo": window_end.isoformat().replace("+00:00", "Z"),
            }
        else:
            raise ValueError(f"Unsupported Elexon dataset: {dataset}")
        response = await client.get(url, params=params)
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, list):
            rows.extend(item for item in payload if isinstance(item, dict))
        elif isinstance(payload, dict):
            nested = payload.get("data", payload.get("results", []))
            if isinstance(nested, list):
                rows.extend(item for item in nested if isinstance(item, dict))
        cursor = window_end
    return rows


async def load_january_data() -> tuple[pd.DataFrame, pd.DataFrame]:
    # Include prior-day forecast publications and a boundary day on each side.
    start = datetime(2025, 12, 31, tzinfo=timezone.utc)
    end = datetime(2026, 2, 1, tzinfo=timezone.utc)
    timeout = httpx.Timeout(60.0, connect=15.0)
    async with httpx.AsyncClient(timeout=timeout, headers={"Accept": "application/json", "User-Agent": "wind-forecast-intelligence/1.0"}) as client:
        actual_rows, forecast_rows = await asyncio.gather(
            fetch_stream(client, "FUELHH", start, end),
            fetch_stream(client, "WINDFOR", start, end),
        )
    if not actual_rows or not forecast_rows:
        raise RuntimeError(f"Elexon returned no rows (FUELHH={len(actual_rows)}, WINDFOR={len(forecast_rows)}).")
    actual = pd.DataFrame(actual_rows)
    forecast = pd.DataFrame(forecast_rows)
    required_actual = {"startTime", "generation", "fuelType"}
    required_forecast = {"startTime", "publishTime", "generation"}
    if not required_actual.issubset(actual.columns):
        raise RuntimeError(f"FUELHH schema mismatch; expected {sorted(required_actual)}, got {list(actual.columns)}")
    if not required_forecast.issubset(forecast.columns):
        raise RuntimeError(f"WINDFOR schema mismatch; expected {sorted(required_forecast)}, got {list(forecast.columns)}")

    actual = actual[actual["fuelType"].astype(str).str.upper().eq("WIND")].copy()
    actual["startTime"] = pd.to_datetime(actual["startTime"], utc=True, errors="coerce")
    actual["actual_mw"] = pd.to_numeric(actual["generation"], errors="coerce")
    actual = actual.dropna(subset=["startTime", "actual_mw"])
    # Deduplicate overlapping API publication records before creating national totals.
    if "publishTime" in actual.columns:
        actual["publishTime"] = pd.to_datetime(actual["publishTime"], utc=True, errors="coerce")
        actual = actual.sort_values("publishTime").drop_duplicates(["startTime", "fuelType"], keep="last")
    actual = actual.groupby("startTime", as_index=False)["actual_mw"].sum()

    forecast["startTime"] = pd.to_datetime(forecast["startTime"], utc=True, errors="coerce")
    forecast["publishTime"] = pd.to_datetime(forecast["publishTime"], utc=True, errors="coerce")
    forecast["forecast_mw"] = pd.to_numeric(forecast["generation"], errors="coerce")
    forecast = forecast.dropna(subset=["startTime", "publishTime", "forecast_mw"])
    return actual, forecast


def get_cutoff(delivery_start: pd.Timestamp, cutoff_hour: int) -> pd.Timestamp:
    local_delivery = delivery_start.tz_convert(UK)
    previous_day = local_delivery.date() - timedelta(days=1)
    local_cutoff = datetime.combine(previous_day, time(hour=cutoff_hour), tzinfo=UK)
    return pd.Timestamp(local_cutoff).tz_convert("UTC")


def build_analysis(actual: pd.DataFrame, forecasts: pd.DataFrame, cutoff_hour: int = 10) -> pd.DataFrame:
    jan_start = pd.Timestamp("2026-01-01T00:00:00Z")
    feb_start = pd.Timestamp("2026-02-01T00:00:00Z")
    actual_jan = actual[(actual.startTime >= jan_start) & (actual.startTime < feb_start)].copy()
    forecast_jan = forecasts[(forecasts.startTime >= jan_start) & (forecasts.startTime < feb_start)].copy()
    if actual_jan.empty or forecast_jan.empty:
        raise RuntimeError(
            "No January 2026 rows available after filtering "
            f"(actual rows={len(actual_jan)}, forecast delivery rows={len(forecast_jan)})."
        )

    # WINDFOR's sample delivery timestamps are hourly while FUELHH outturn is
    # half-hourly. Select a vintage per forecast delivery hour, then apply that
    # hourly forecast to the two half-hourly settlement intervals in that hour.
    chosen = []
    for delivery, group in forecast_jan.groupby("startTime", sort=False):
        cutoff = get_cutoff(delivery, cutoff_hour)
        eligible = group[group.publishTime <= cutoff]
        if not eligible.empty:
            row = eligible.loc[eligible.publishTime.idxmax()]
            chosen.append({"forecast_key": delivery, "forecast_mw": float(row.forecast_mw), "publishTime": row.publishTime, "cutoffTime": cutoff})
    selected = pd.DataFrame(chosen)
    if selected.empty:
        raise RuntimeError("No forecasts were published before the configured day-ahead cutoff. Check API timestamps or cutoff assumptions.")

    actual_jan["forecast_key"] = actual_jan["startTime"].dt.floor("h")
    result = actual_jan.merge(selected, on="forecast_key", how="left")
    result["error_mw"] = result["forecast_mw"] - result["actual_mw"]
    result["absolute_error_mw"] = result["error_mw"].abs()
    result["squared_error_mw2"] = result["error_mw"] ** 2
    result["delivery_local"] = result["startTime"].dt.tz_convert(UK).astype(str)
    result["delivery_date"] = result["startTime"].dt.tz_convert(UK).dt.strftime("%Y-%m-%d")
    result["publish_local"] = result["publishTime"].dt.tz_convert(UK).astype(str)
    return result.sort_values("startTime").reset_index(drop=True)


def metrics(frame: pd.DataFrame) -> dict[str, Any]:
    valid = frame.dropna(subset=["forecast_mw", "actual_mw", "error_mw"])
    if valid.empty:
        return {"sample_count": 0, "coverage_pct": 0, "mae_mw": None, "rmse_mw": None, "bias_mw": None, "mape_pct": None, "wape_pct": None}
    errors = valid["error_mw"].to_numpy(dtype=float)
    actual = valid["actual_mw"].to_numpy(dtype=float)
    nonzero = np.abs(actual) > 1e-9
    mape = float(np.mean(np.abs(errors[nonzero] / actual[nonzero])) * 100) if nonzero.any() else None
    denominator = float(np.sum(np.abs(actual)))
    wape = float(np.sum(np.abs(errors)) / denominator * 100) if denominator > 1e-9 else None
    return {
        "sample_count": int(len(valid)),
        "coverage_pct": round(100 * len(valid) / max(len(frame), 1), 2),
        "mae_mw": round(float(np.mean(np.abs(errors))), 2),
        "rmse_mw": round(float(np.sqrt(np.mean(errors ** 2))), 2),
        "bias_mw": round(float(np.mean(errors)), 2),
        "mape_pct": round(mape, 2) if mape is not None else None,
        "wape_pct": round(wape, 2) if wape is not None else None,
    }


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "wind-forecast-intelligence-api"}


@app.get("/api/analysis")
async def analysis(cutoff_hour: int = Query(default=10, ge=0, le=23)) -> dict[str, Any]:
    """January 2026 analysis. cutoff_hour is UK local time on the prior day."""
    try:
        actual, forecasts = await load_january_data()
        frame = build_analysis(actual, forecasts, cutoff_hour)
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        logger.exception("Analysis data load failed")
        raise HTTPException(status_code=502, detail=f"Unable to build analysis from Elexon data: {exc}") from exc
    valid = frame.dropna(subset=["forecast_mw", "actual_mw", "error_mw"])
    daily = []
    for day, group in frame.groupby("delivery_date", sort=True):
        m = metrics(group)
        daily.append({"date": day, **m, "actual_mwh": round(float(group.actual_mw.sum() * 0.5), 2), "forecast_mwh": round(float(group.forecast_mw.sum(min_count=1) * 0.5), 2) if group.forecast_mw.notna().any() else None})
    # Return a manageable but chart-friendly half-hourly series (up to 1,488 rows).
    points = []
    for row in frame.itertuples(index=False):
        points.append({
            "start_time": row.startTime.isoformat(), "delivery_local": row.delivery_local,
            "delivery_date": row.delivery_date,
            "actual_mw": round(float(row.actual_mw), 2) if pd.notna(row.actual_mw) else None,
            "forecast_mw": round(float(row.forecast_mw), 2) if pd.notna(row.forecast_mw) else None,
            "error_mw": round(float(row.error_mw), 2) if pd.notna(row.error_mw) else None,
            "publish_time": row.publish_local if pd.notna(row.publish_local) else None,
            "cutoff_time": row.cutoffTime.isoformat() if pd.notna(row.cutoffTime) else None,
        })
    return {
        "metadata": {"period": "2026-01", "timezone": "Europe/London", "cutoff_hour_local": cutoff_hour,
                     "cutoff_rule": f"latest publishTime at or before {cutoff_hour:02d}:00 UK time on the day before delivery",
                     "sources": [f"{BASE_URL}/datasets/FUELHH/stream", f"{BASE_URL}/datasets/WINDFOR/stream"],
                     "scope_note": "Wind generation forecast accuracy only. This is not a total procurement recommendation because demand, prices, interconnectors and other generation are out of scope.",
                     "resolution_note": "WINDFOR hourly forecasts are applied to both half-hourly FUELHH intervals within the matching UTC hour; no linear interpolation is assumed."},
        "summary": metrics(frame), "daily": daily, "points": points,
        "data_quality": {"actual_intervals": int(len(frame)), "intervals_with_eligible_forecast": int(len(valid)), "missing_forecast_intervals": int(frame.forecast_mw.isna().sum()), "forecast_publication_count": int(forecasts.publishTime.nunique())}
    }
