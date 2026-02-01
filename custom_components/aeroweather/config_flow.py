from __future__ import annotations

import re
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback

from .const import DOMAIN, CONF_ICAOS, CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL

ICAO_RE = re.compile(r"^[A-Z0-9]{4}$")


class AeroWeatherConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = {}

        if user_input is not None:
            icaos = [
                s.strip().upper()
                for s in user_input[CONF_ICAOS].replace(";", ",").split(",")
                if s.strip()
            ]
            if not icaos or any(not ICAO_RE.match(i) for i in icaos):
                errors["base"] = "invalid_icao"
            else:
                return self.async_create_entry(
                    title="AeroWeather",
                    data={
                        CONF_ICAOS: sorted(set(icaos)),
                        CONF_SCAN_INTERVAL: int(
                            user_input.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
                        ),
                    },
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_ICAOS): str,
                vol.Optional(
                    CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL
                ): vol.Coerce(int),
            }
        )
        return self.async_show_form("user", schema, errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return AeroWeatherOptionsFlow(config_entry)


class AeroWeatherOptionsFlow(config_entries.OptionsFlow):
    def __init__(self, entry):
        self.entry = entry

    async def async_step_init(self, user_input=None):
        current = {**self.entry.data, **self.entry.options}
        errors = {}

        if user_input is not None:
            icaos = [
                s.strip().upper()
                for s in user_input[CONF_ICAOS].replace(";", ",").split(",")
                if s.strip()
            ]
            if not icaos or any(not ICAO_RE.match(i) for i in icaos):
                errors["base"] = "invalid_icao"
            else:
                return self.async_create_entry(
                    title="",
                    data={
                        CONF_ICAOS: sorted(set(icaos)),
                        CONF_SCAN_INTERVAL: int(
                            user_input.get(
                                CONF_SCAN_INTERVAL,
                                current.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                            )
                        ),
                    },
                )

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_ICAOS, default=",".join(current.get(CONF_ICAOS, []))
                ): str,
                vol.Optional(
                    CONF_SCAN_INTERVAL,
                    default=current.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                ): vol.Coerce(int),
            }
        )
        return self.async_show_form("init", schema, errors)
