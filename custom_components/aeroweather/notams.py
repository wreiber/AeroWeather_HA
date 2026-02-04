"""
NOTAM fetcher for AeroWeather (pure HTTP, no Home Assistant imports).

This module is intentionally provider-agnostic:
- It builds a request to a NOTAM REST endpoint
- Returns a list of raw NOTAM dicts (as returned by the provider)
- Normalization/classification/severity is handled elsewhere (notam_helpers.py)

IMPORTANT:
Different NOTAM providers use different URLs/parameters/JSON shapes.
This file is written to be easy to adapt once we confirm the endpoint
you want to use (FAA NMS / SWIM-derived, etc.).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping
import asyncio
import logging

from aiohttp import ClientResponseError, ClientSession, ClientTimeout

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class NotamApiConfig:
    """
    Provider configuration.

    base_url:
      The provider endpoint that returns NOTAMs in JSON.

    api_key:
      Optional. If your provider requires an API key, we will send:
        - header:  "x-api-key: <key>"
      If your provider uses a different header/bearer token, change _build_headers().

    timeout_s:
      Total request timeout.

    page_size:
      If provider supports paging. Used as a hint via common param names.
    """
    base_url: str
    api_key: str | None = None
    timeout_s: int = 20
    page_size: int = 200


def _build_headers(cfg: NotamApiConfig) -> dict[str, str]:
    headers: dict[str, str] = {
        "accept": "application/json",
        "user-agent": "HomeAssistant-AeroWeather/1.0",
    }
    if cfg.api_key:
        headers["x-api-key"] = cfg.api_key
    return headers


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _extract_list(payload: Any) -> list[dict[str, Any]]:
    """
    Providers vary:
      - Some return a list directly: [...]
      - Some return an object with "items"/"notams"/"data"/etc.

    We try a few common patterns, otherwise fail loudly.
    """
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]

    if isinstance(payload, dict):
        for key in ("notams", "items", "data", "results"):
            val = payload.get(key)
            if isinstance(val, list):
                return [x for x in val if isinstance(x, dict)]

    raise ValueError("Unexpected NOTAM payload shape (expected list or dict with notams/items/data/results).")


async def fetch_notams_for_icao(
    session: ClientSession,
    icao: str,
    cfg: NotamApiConfig,
) -> list[dict[str, Any]]:
    """
    Fetch NOTAMs for one ICAO.

    Parameters sent are "best-effort defaults" that work with many APIs.
    Once we lock your provider, we will tune these exactly.
    """
    icao = (icao or "").strip().upper()
    if len(icao) != 4:
        raise ValueError(f"ICAO must be 4 letters, got: {icao!r}")

    # Common parameter names used by various NOTAM APIs.
    # We include multiple aliases because some providers accept one of them.
    params: dict[str, str] = {
        # location
        "location": icao,
        "icaoLocation": icao,
        "airport": icao,
        # time bounding (many providers default to "active"; some want explicit windows)
        "effectiveBefore": _utc_now_iso(),
        # paging hints
        "pageSize": str(cfg.page_size),
        "limit": str(cfg.page_size),
        # try to prefer only currently-active NOTAMs when supported
        "active": "true",
    }

    headers = _build_headers(cfg)
    timeout = ClientTimeout(total=cfg.timeout_s)

    try:
        async with session.get(cfg.base_url, params=params, headers=headers, timeout=timeout) as resp:
            resp.raise_for_status()
            payload = await resp.json(content_type=None)
    except asyncio.TimeoutError as err:
        raise RuntimeError(f"NOTAM request timed out for {icao} (base_url={cfg.base_url})") from err
    except ClientResponseError as err:
        # Helpful log: status + URL
        raise RuntimeError(
            f"NOTAM request failed for {icao}: HTTP {err.status} (base_url={cfg.base_url})"
        ) from err
    except Exception as err:
        raise RuntimeError(f"NOTAM request failed for {icao} (base_url={cfg.base_url}): {err}") from err

    try:
        notams = _extract_list(payload)
    except Exception as err:
        _LOGGER.debug("Raw NOTAM payload for %s: %s", icao, payload)
        raise

    return notams


async def fetch_notams_bulk(
    session: ClientSession,
    icaos: list[str],
    cfg: NotamApiConfig,
) -> dict[str, list[dict[str, Any]]]:
    """
    Fetch NOTAMs for multiple ICAOs concurrently.

    Returns:
      { "KCLT": [...], "KRUQ": [...], ... }
    """
    clean_icaos = [i.strip().upper() for i in (icaos or []) if i and i.strip()]
    results: dict[str, list[dict[str, Any]]] = {}

    async def _one(i: str) -> None:
        results[i] = await fetch_notams_for_icao(session, i, cfg)

    await asyncio.gather(*[_one(i) for i in clean_icaos])
    return results

