from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from aiohttp import ClientSession
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    API_BASE,
    CONF_ICAOS,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)


class AeroWeatherCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        self.session: ClientSession = async_get_clientsession(hass)

        data = {**entry.data, **entry.options}
        self.icaos = data.get(CONF_ICAOS, [])
        scan = int(data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))

        super().__init__(
            hass,
            logger=__import__("logging").getLogger(__name__),
            name=DOMAIN,
            update_interval=timedelta(seconds=scan),
        )

async def _fetch(self, endpoint: str, ids: str):
    async with self.session.get(
        f"{API_BASE}/{endpoint}",
        params={"ids": ids, "format": "json"},
        timeout=20,
    ) as resp:
        # No content = no TAF/METAR available (common for TAF). Not an error.
        if resp.status == 204:
            return []

        if resp.status != 200:
            text = await resp.text()
            raise UpdateFailed(f"{endpoint} HTTP {resp.status}: {text[:200]}")

        payload = await resp.json(content_type=None)

        # Normalize list vs wrapped dict responses
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in ("data", "results", endpoint):
                val = payload.get(key)
                if isinstance(val, list):
                    return val
        return []


        ids = ",".join(self.icaos)
        try:
            metars, tafs = await asyncio.gather(
                self._fetch("metar", ids),
                self._fetch("taf", ids),
            )
        except Exception as err:
            raise UpdateFailed(str(err)) from err

        return {
            "icaos": self.icaos,
            "metar": {
                (m.get("icaoId") or m.get("stationId")).upper(): m
                for m in metars or []
                if m.get("icaoId") or m.get("stationId")
            },
            "taf": {
                (t.get("icaoId") or t.get("stationId")).upper(): t
                for t in tafs or []
                if t.get("icaoId") or t.get("stationId")
            },
        }

