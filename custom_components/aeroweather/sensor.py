from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import re

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


ALTIM_HPA_PER_INHG = 33.8638866667

def _altimeter_to_inhg(val: Any) -> float | None:
    """Return altimeter setting in inHg from various possible inputs."""
    if val is None:
        return None

    # Handle raw METAR tokens like "A3011" or "Q1013"
    if isinstance(val, str):
        s = val.strip().upper()
        m = re.search(r"\bA(\d{4})\b", s)
        if m:
            return round(int(m.group(1)) / 100.0, 2)
        m = re.search(r"\bQ(\d{4})\b", s)
        if m:
            return round(int(m.group(1)) / ALTIM_HPA_PER_INHG, 2)

        # fall through: maybe it's a numeric string
        try:
            val = float(s)
        except ValueError:
            return None

    # Numeric path
    f = _to_float(val)
    if f is None:
        return None

    # Heuristic:
    # - If it's > 80, it's definitely NOT inHg (likely hPa)
    # - If it's ~25-35, it's probably already inHg
    if f > 80:
        return round(f / ALTIM_HPA_PER_INHG, 2)
    return round(f, 2)


def _c_to_f(c: float) -> float:
    return (c * 9.0 / 5.0) + 32.0


def _pressure_altitude_ft(field_elev_ft: float, altimeter_inhg: float) -> float:
    """
    Pressure Altitude (ft) approximation:
      PA ≈ field_elev + (29.92 - altimeter) * 1000
    """
    return field_elev_ft + (29.92 - altimeter_inhg) * 1000.0


def _isa_temp_c_at_alt_ft(alt_ft: float) -> float:
    """
    ISA temp at altitude (°C):
      ISA ≈ 15 - 2°C per 1000 ft
    """
    return 15.0 - 2.0 * (alt_ft / 1000.0)


def _density_altitude_ft(field_elev_ft: float, altimeter_inhg: float, oat_c: float) -> float:
    """
    Density Altitude (ft) approximation:
      DA ≈ PA + 120 * (OAT - ISA)
    """
    pa = _pressure_altitude_ft(field_elev_ft, altimeter_inhg)
    isa = _isa_temp_c_at_alt_ft(pa)
    return pa + 120.0 * (oat_c - isa)


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


def _ceil_ft_from_layers(metar: dict[str, Any]) -> float:
    """
    Return ceiling in feet AGL.
    If no ceiling is reported (CLR / SKC / FEW / SCT only),
    return 0 to indicate 'Clear'.
    """
    layers = metar.get("clouds")
    if not layers:
        return "Clear"

    ceilings: list[float] = []
    for layer in layers:
        cover = layer.get("cover")
        base = _to_float(layer.get("base_ft_agl"))
        if cover in ("BKN", "OVC", "VV") and base is not None:
            ceilings.append(base)

    return min(ceilings) if ceilings else "Clear"



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


_VIS_RE = re.compile(r"\b(?:(P)?(\d+)(?:\s+(\d+)/(\d+))?|(\d+)/(\d+))SM\b")

def _parse_vis_from_raw_sm(raw: str) -> float | None:
    """Parse visibility in SM from raw METAR like 10SM, P6SM, 1 1/2SM, 3/4SM."""
    if not raw:
        return None

    m = _VIS_RE.search(raw)
    if not m:
        # CAVOK implies >=10km (~6.2SM)
        if "CAVOK" in raw:
            return 6.2
        return None

    whole = m.group(2)
    num1, den1 = m.group(3), m.group(4)
    num2, den2 = m.group(5), m.group(6)

    vis = float(whole) if whole else 0.0
    if num1 and den1:
        vis += float(num1) / float(den1)
    elif num2 and den2:
        vis += float(num2) / float(den2)

    return vis


def _visibility_sm(metar: dict[str, Any]) -> float | None:
    # 1) Try structured numeric fields first
    vis_sm = _to_float(_first_present(metar, ["visib", "vis", "visibility", "visSm"]))
    if vis_sm is not None:
        return vis_sm

    # 2) Try meters → miles
    vis_m = _to_float(_first_present(metar, ["visibilityMeters", "visMeters", "vis_m"]))
    if vis_m is not None:
        return vis_m / 1609.344

    # 3) Fallback: parse from raw METAR text (e.g., 10SM, 1 1/2SM, P6SM)
    raw = _first_present(metar, ["rawOb", "rawText", "text", "metar"])
    if isinstance(raw, str):
        return _parse_vis_from_raw_sm(raw)

    return None


def _altim_inhg(metar: dict[str, Any]) -> float | None:
    raw = _first_present(
        metar,
        ["altim", "altimHg", "altimeter", "altim_inhg", "qnh", "QNH"],
    )
    return _altimeter_to_inhg(raw)
    

def _temp_c(metar: dict[str, Any]) -> float | None:
    return _to_float(_first_present(metar, ["temp", "tempC", "temperature", "tmpc"]))


def _dewpoint_c(metar: dict[str, Any]) -> float | None:
    return _to_float(_first_present(metar, ["dewp", "dewpoint", "dewpC", "dwpc"]))


def _wx_string(metar: dict[str, Any]) -> str | None:
    wx = metar.get("wx")
    if not wx:
        return None
    if isinstance(wx, list):
        return " ".join([str(x) for x in wx if x])
    return str(wx)

def _parse_ceiling_ft(metar: dict[str, Any]) -> float:
    """Return ceiling in feet AGL. If no ceiling is reported, return 0 (Clear)."""
    layers = metar.get("clouds")
    if not layers:
        return 0

    ceilings: list[float] = []
    for layer in layers:
        cover = layer.get("cover")
        base = _to_float(layer.get("base_ft_agl"))
        if cover in ("BKN", "OVC", "VV") and base is not None:
            ceilings.append(base)

    return min(ceilings) if ceilings else 0

# --- Density altitude support ---

FIELD_ELEV_FT: dict[str, int] = {
    "KCLT": 748,
    "KINT": 969,
    "KRUQ": 772,
}

def _pressure_altitude_ft(field_elev_ft: float, altimeter_inhg: float) -> float:
    return field_elev_ft + (29.92 - altimeter_inhg) * 1000.0

def _isa_temp_c_at_alt_ft(alt_ft: float) -> float:
    return 15.0 - 2.0 * (alt_ft / 1000.0)

def _density_altitude_ft(field_elev_ft: float, altimeter_inhg: float, oat_c: float) -> float:
    pa = _pressure_altitude_ft(field_elev_ft, altimeter_inhg)
    isa = _isa_temp_c_at_alt_ft(pa)
    return pa + 120.0 * (oat_c - isa)

def _density_altitude_station(data: dict[str, Any], icao: str) -> int | None:
    metar = _metar_item(data, icao) or {}
    if not metar:
        return None

    elev_ft = FIELD_ELEV_FT.get(icao)
    if elev_ft is None:
        return None

    alt_inhg = _altim_inhg(metar)
    temp_c = _temp_c(metar)
    if alt_inhg is None or temp_c is None:
        return None

    da_ft = _density_altitude_ft(float(elev_ft), float(alt_inhg), float(temp_c))
    return int(round(da_ft))


    wx = _first_present(metar, ["wxString", "presentWeather", "wx"])
    if isinstance(wx, str) and wx.strip():
        return wx.strip()
    return None


@dataclass(frozen=True)
class AeroWeatherSensorSpec:
    description: SensorEntityDescription
    value_fn: Any
    attrs_fn: Any | None = None



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


DESCRIPTIONS: list[AeroWeatherSensorSpec] = [
    AeroWeatherSensorSpec(
        description=SensorEntityDescription(
            key="metar_raw",
            name="METAR (raw)",
            icon="mdi:weather-windy",
        ),
        value_fn=_raw_metar,
        attrs_fn=lambda d, i: _metar_item(d, i) or {},
    ),
    AeroWeatherSensorSpec(
        description=SensorEntityDescription(
            key="taf_raw",
            name="TAF (raw)",
            icon="mdi:weather-cloudy-clock",
        ),
        value_fn=_raw_taf,
        attrs_fn=lambda d, i: _taf_item(d, i) or {},
    ),
    AeroWeatherSensorSpec(
        description=SensorEntityDescription(
            key="flight_category",
            name="Flight category",
            icon="mdi:airplane",
        ),
        value_fn=lambda d, i: (_flight_category_from_metar(_metar_item(d, i) or {}) if _metar_item(d, i) else None),
    ),
    AeroWeatherSensorSpec(
        description=SensorEntityDescription(
            key="wind_dir",
            name="Wind direction",
            icon="mdi:compass",
            native_unit_of_measurement="°",
        ),
        value_fn=lambda d, i: (_wind_dir_deg(_metar_item(d, i) or {}) if _metar_item(d, i) else None),
    ),
    AeroWeatherSensorSpec(
        description=SensorEntityDescription(
            key="wind_speed",
            name="Wind speed",
            icon="mdi:weather-windy",
            native_unit_of_measurement="kn",
        ),
        value_fn=lambda d, i: (_wind_spd_kt(_metar_item(d, i) or {}) if _metar_item(d, i) else None),
    ),
    AeroWeatherSensorSpec(
        description=SensorEntityDescription(
            key="wind_gust",
            name="Wind gust",
            icon="mdi:weather-windy-variant",
            native_unit_of_measurement="kn",
        ),
        value_fn=lambda d, i: (_wind_gust_kt(_metar_item(d, i) or {}) if _metar_item(d, i) else None),
    ),
    AeroWeatherSensorSpec(
        description=SensorEntityDescription(
            key="visibility",
            name="Visibility",
            icon="mdi:eye",
            native_unit_of_measurement="mi",
        ),
        value_fn=lambda d, i: (_visibility_sm(_metar_item(d, i) or {}) if _metar_item(d, i) else None),
    ),
    AeroWeatherSensorSpec(
        description=SensorEntityDescription(
            key="ceiling",
            name="Ceiling",
            icon="mdi:cloud",
            native_unit_of_measurement="ft",
        ),
        value_fn=lambda d, i: (_ceil_ft_from_layers(_metar_item(d, i) or {}) if _metar_item(d, i) else None),
    ),
    AeroWeatherSensorSpec(
        description=SensorEntityDescription(
            key="altimeter",
            name="Altimeter",
            icon="mdi:gauge",
            native_unit_of_measurement="inHg",
        ),
        value_fn=lambda d, i: (_altim_inhg(_metar_item(d, i) or {}) if _metar_item(d, i) else None),
    ),
    AeroWeatherSensorSpec(
        description=SensorEntityDescription(
            key="temp",
            name="Temperature",
            icon="mdi:thermometer",
            native_unit_of_measurement="°C",
        ),
        value_fn=lambda d, i: (_temp_c(_metar_item(d, i) or {}) if _metar_item(d, i) else None),
    ),
    AeroWeatherSensorSpec(
    description=SensorEntityDescription(
        key="density_altitude",
        name="Density Altitude",
        icon="mdi:arrow-expand-vertical",
        native_unit_of_measurement="ft",
    ),
    value_fn=_density_altitude_station,
),
    AeroWeatherSensorSpec(
        description=SensorEntityDescription(
            key="dewpoint",
            name="Dewpoint",
            icon="mdi:water-percent",
            native_unit_of_measurement="°C",
        ),
        value_fn=lambda d, i: (_dewpoint_c(_metar_item(d, i) or {}) if _metar_item(d, i) else None),
    ),
    AeroWeatherSensorSpec(
        description=SensorEntityDescription(
            key="wx",
            name="Weather",
            icon="mdi:weather-partly-rainy",
        ),
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
    def __init__(self, coordinator, icao: str, spec: AeroWeatherSensorSpec):
        super().__init__(coordinator)
        self._icao = icao
        self._spec = spec

        # Home Assistant uses the inner SensorEntityDescription
        self.entity_description = spec.description

        self._attr_unique_id = f"{coordinator.entry.entry_id}_{icao}_{spec.description.key}"
        self._attr_name = f"{icao} {spec.description.name}"

        key = spec.description.key
        if key in {"wind_speed", "wind_gust"}:
            self._attr_native_unit_of_measurement = UnitOfSpeed.KNOTS
        elif key == "visibility":
            self._attr_native_unit_of_measurement = UnitOfLength.MILES
        elif key == "ceiling":
            self._attr_native_unit_of_measurement = "ft"
        elif key == "density_altitude":
            self._attr_native_unit_of_measurement = UnitOfLength.FEET
        elif key == "altimeter":
            self._attr_native_unit_of_measurement = UnitOfPressure.INHG
        elif key in {"temp", "dewpoint"}:
            self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
        elif key == "wind_dir":
            self._attr_native_unit_of_measurement = "°"

    @property
    def native_value(self):
        return self._spec.value_fn(self.coordinator.data or {}, self._icao)

    @property
    def extra_state_attributes(self):
        if self._spec.attrs_fn is None:
            return {}
        return self._spec.attrs_fn(self.coordinator.data or {}, self._icao)

CONF_ELEVATIONS = "elevations_ft"

DEFAULT_ELEVATIONS_FT = {
    "KCLT": 748,
    "KINT": 969,
    "KRUQ": 772,
    "KEXX": 733,
}
