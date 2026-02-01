from __future__ import annotations

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.const import UnitOfLength, UnitOfPressure, UnitOfSpeed, UnitOfTemperature
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import AeroWeatherCoordinator


def _metar_item(data: dict[str, Any], icao: str) -> dict[str, Any] | None:
    return (data.get("metar", {}) or {}).get(icao)


def _taf_item(data: dict[str, Any], icao: str) -> dict[str, Any] | None:
    return (data.get("taf", {}) or {}).get(icao)


def _first_present(d: dict[str, Any], keys: list[str]) -> Any:
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def _to_float(val: Any) -> float | None:
    try:
        if val is None:
            return None
        return float(val)
    except (TypeError, ValueError):
        return None


def _to_int(val: Any) -> int | None:
    try:
        if val is None:
            return None
        return int(float(val))
    except (TypeError, ValueError):
        return None


def _parse_ceiling_ft(metar: dict[str, Any]) -> int | None:
    """
    Ceiling = lowest BKN/OVC/VV layer base (ft AGL).
    AviationWeather API payloads vary; handle common shapes.
    """
    # Common modern shape: "clouds": [{"cover":"BKN","base":2500}, ...]
    clouds = metar.get("clouds")
    candidates: list[int] = []

    if isinstance(clouds, list):
        for layer in clouds:
            if not isinstance(layer, dict):
                continue
            cover = (layer.get("cover") or layer.get("cvg") or "").upper()
            base = _to_int(layer.get("base") or layer.get("baseFt") or layer.get("bas"))
            if cover in {"BKN", "OVC", "VV"} and base is not None:
                candidates.append(base)

    # Older/alternate shapes: cldCvg1/cldBas1... etc.
    for i in range(1, 7):
        cover = (metar.get(f"cldCvg{i}") or metar.get(f"cldCvg{i}".lower()) or "")
        base = metar.get(f"cldBas{i}") or metar.get(f"cldBas{i}".lower())
        cover_u = str(cover).upper() if cover is not None else ""
        base_i = _to_int(base)
        if cover_u in {"BKN", "OVC", "VV"} and base_i is not None:
            candidates.append(base_i)

    return min(candidates) if candidates else None


def _flight_category_from_metar(metar: dict[str, Any]) -> str | None:
    """
    Prefer fltCat from API if present; else compute from ceiling/visibility.
    """
    fltcat = _first_present(metar, ["fltCat", "flightCategory", "fltcat"])
    if isinstance(fltcat, str) and fltcat.strip():
        return fltcat.strip().upper()

    # Compute (FAA-ish): VFR >=3000 and >=5; MVFR 1000-2999 or 3-4;
    # IFR 500-999 or 1-2; LIFR <500 or <1
    ceiling = _parse_ceiling_ft(metar)
    vis_sm = _to_float(_first_present(metar, ["visib", "vis", "visibility", "visSm"]))

    # If API provides visibility in meters sometimes:
    vis_m = _to_float(_first_present(metar, ["visibilityMeters", "visMeters", "vis_m"]))
    if vis_sm is None and vis_m is not None:
        vis_sm = vis_m / 1609.344

    # If we have neither, can't compute reliably
    if ceiling is None and vis_sm is None:
        return None

    # Use very high ceiling if missing; same for vis
    ceiling_eff = ceiling if ceiling is not None else 99999
    vis_eff = vis_sm if vis_sm is not None else 99.0

    if ceiling_eff < 500 or vis_eff < 1.0:
        return "LIFR"
    if ceiling_eff < 1000 or vis_eff < 3.0:
        return "IFR"
    if ceiling_eff < 3000 or vis_eff < 5.0:
        return "MVFR"
    return "VFR"


def _wind_dir_deg(metar: dict[str, Any]) -> int | None:
    # Common keys: wdir / windDir / wdirDegrees
    return _to_int(_first_present(metar, ["wdir", "windDir", "wdirDegrees", "wind_dir_degrees"]))


def _wind_spd_kt(metar: dict[str, Any]) -> int | None:
    return _to_int(_first_present(metar, ["wspd", "windSpeed", "wspdKt", "wind_speed_kt"]))


def _wind_gust_kt(metar: dict[str, Any]) -> int | None:
    return _to_int(_first_present(metar, ["wgst", "windGust", "wgstKt", "wind_gust_kt"]))


def _visibility_sm(metar: dict[str, Any]) -> float | None:
    vis_sm = _to_float(_first_present(metar, ["visib", "vis", "visibility", "visSm"]))
    if vis_sm is not None:
        return vis_sm
    vis_m = _to_float(_first_present(metar, ["visibilityMeters", "visMeters", "vis_m"]))
    if vis_m is not None:
        return vis_m / 1609.344
    return None


def _altim_inhg(metar: dict[str, Any]) -> float | None:
    # API may provide altim (inHg) or altimHg
    return _to_float(_first_present(metar, ["altim", "altimHg", "altimeter", "altim_inhg"]))


def _temp_c(metar: dict[str, Any]) -> float | None:
    return _to_float(_first_present(metar, ["temp", "tempC", "temperature", "tmpc"]))


def _dewpoint_c(metar: dict[str, Any]) -> float | None:
    return _to_float(_first_present(metar, ["dewp", "dewpoint", "dewpC", "dwpc"]))


def _wx_string(metar: dict[str, Any]) -> str | None:
    wx = _first_present(metar, ["wxString", "presentWeather", "wx"])
    if isinstance(wx, str) and wx.strip():
        return wx.strip()
    return None


class AeroWeatherSensorDescription(SensorEntityDescription):
    """Sensor description with callbacks (not a dataclass)."""

    def __init__(
        self,
        *,
        key: str,
        name: str,
        icon: str | None = None,
        native_unit_of_measurement: str | None = None,
        value_fn,
        attrs_fn=None,
    ) -> None:
        super().__init__(
            key=key,
            name=name,
            icon=icon,
            native_unit_of_measurement=native_unit_of_measurement,
        )
        self.value_fn = value_fn
        self.attrs_fn = attrs_fn



def _raw_metar(data: dict[str, Any], icao: str) -> str | None:
    m = _metar_item(data, icao)
    if not m:
        return None
    return _first_present(m, ["rawOb", "rawText", "text", "metar"])


def _raw_taf(data: dict[str, Any], icao: str) -> str | None:
    t = _taf_item(data, icao)
    if not t:
        return None
    return _first_present(t, ["rawTAF", "rawText", "text", "taf"])


DESCRIPTIONS: list[AeroWeatherSensorDescription] = [
    # Raw
    AeroWeatherSensorDescription(
        key="metar_raw",
        name="METAR (raw)",
        icon="mdi:weather-windy",
        value_fn=_raw_metar,
        attrs_fn=lambda d, i: _metar_item(d, i) or {},
    ),
    AeroWeatherSensorDescription(
        key="taf_raw",
        name="TAF (raw)",
        icon="mdi:weather-cloudy-clock",
        value_fn=_raw_taf,
        attrs_fn=lambda d, i: _taf_item(d, i) or {},
    ),

    # Decoded METAR fields
    AeroWeatherSensorDescription(
        key="flight_category",
        name="Flight category",
        icon="mdi:airplane",
        value_fn=lambda d, i: (_flight_category_from_metar(_metar_item(d, i) or {}) if _metar_item(d, i) else None),
    ),
    AeroWeatherSensorDescription(
        key="wind_dir",
        name="Wind direction",
        icon="mdi:compass",
        value_fn=lambda d, i: (_wind_dir_deg(_metar_item(d, i) or {}) if _metar_item(d, i) else None),
    ),
    AeroWeatherSensorDescription(
        key="wind_speed",
        name="Wind speed",
        icon="mdi:weather-windy",
        value_fn=lambda d, i: (_wind_spd_kt(_metar_item(d, i) or {}) if _metar_item(d, i) else None),
    ),
    AeroWeatherSensorDescription(
        key="wind_gust",
        name="Wind gust",
        icon="mdi:weather-windy-variant",
        value_fn=lambda d, i: (_wind_gust_kt(_metar_item(d, i) or {}) if _metar_item(d, i) else None),
    ),
    AeroWeatherSensorDescription(
        key="visibility",
        name="Visibility",
        icon="mdi:eye",
        value_fn=lambda d, i: (_visibility_sm(_metar_item(d, i) or {}) if _metar_item(d, i) else None),
    ),
    AeroWeatherSensorDescription(
        key="ceiling",
        name="Ceiling",
        icon="mdi:cloud",
        value_fn=lambda d, i: (_parse_ceiling_ft(_metar_item(d, i) or {}) if _metar_item(d, i) else None),
    ),
    AeroWeatherSensorDescription(
        key="altimeter",
        name="Altimeter",
        icon="mdi:gauge",
        value_fn=lambda d, i: (_altim_inhg(_metar_item(d, i) or {}) if _metar_item(d, i) else None),
    ),
    AeroWeatherSensorDescription(
        key="temp",
        name="Temperature",
        icon="mdi:thermometer",
        value_fn=lambda d, i: (_temp_c(_metar_item(d, i) or {}) if _metar_item(d, i) else None),
    ),
    AeroWeatherSensorDescription(
        key="dewpoint",
        name="Dewpoint",
        icon="mdi:water-percent",
        value_fn=lambda d, i: (_dewpoint_c(_metar_item(d, i) or {}) if _metar_item(d, i) else None),
    ),
    AeroWeatherSensorDescription(
        key="wx",
        name="Weather",
        icon="mdi:weather-partly-rainy",
        value_fn=lambda d, i: (_wx_string(_metar_item(d, i) or {}) if _metar_item(d, i) else None),
    ),
]


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator: AeroWeatherCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities = [
        AeroWeatherSensor(coordinator, icao, desc)
        for icao in coordinator.icaos
        for desc in DESCRIPTIONS
    ]
    async_add_entities(entities)


class AeroWeatherSensor(CoordinatorEntity[AeroWeatherCoordinator], SensorEntity):
    def __init__(self, coordinator, icao: str, description: AeroWeatherSensorDescription):
        super().__init__(coordinator)
        self._icao = icao
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{icao}_{description.key}"
        self._attr_name = f"{icao} {description.name}"

        # Units for numeric decoded sensors
        if description.key in {"wind_speed", "wind_gust"}:
            self._attr_native_unit_of_measurement = UnitOfSpeed.KNOTS
        elif description.key == "visibility":
            self._attr_native_unit_of_measurement = UnitOfLength.MILES
        elif description.key == "ceiling":
            self._attr_native_unit_of_measurement = "ft"
        elif description.key == "altimeter":
            self._attr_native_unit_of_measurement = UnitOfPressure.INHG
        elif description.key in {"temp", "dewpoint"}:
            self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
        elif description.key == "wind_dir":
            self._attr_native_unit_of_measurement = "°"

    @property
    def native_value(self):
        return self.entity_description.value_fn(self.coordinator.data or {}, self._icao)

    @property
    def extra_state_attributes(self):
        if self.entity_description.attrs_fn is None:
            return {}
        return self.entity_description.attrs_fn(self.coordinator.data or {}, self._icao)
