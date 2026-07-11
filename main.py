#!/usr/bin/env python
"""
https://github.com/jantman/prometheus-ecowitt-exporter

MIT License

Copyright (c) 2026 Jason Antman

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

import sys
import os
import argparse
import logging
import socket
import time
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Generator, Dict, Optional, Tuple, List, Callable

import requests
from wsgiref.simple_server import make_server, WSGIServer
from prometheus_client.core import REGISTRY, GaugeMetricFamily, Metric
from prometheus_client.exposition import make_wsgi_app, _SilentHandler
from prometheus_client.samples import Sample

FORMAT = "[%(asctime)s %(levelname)s] %(message)s"
logging.basicConfig(level=logging.WARNING, format=FORMAT)
logger = logging.getLogger()

#: Default timezone assumed for the gateway's naive local timestamps.
DEFAULT_TZ = 'America/New_York'


def _resolve_tz() -> ZoneInfo:
    """Resolve the timezone used to interpret the gateway's local timestamps.

    The gateway emits naive wall-clock strings (no offset) in its own local
    timezone; we must attach that zone before converting to a Unix epoch, since
    the container's own timezone is typically UTC. Configurable via ``ECOWITT_TZ``
    (an IANA name, e.g. ``America/New_York``), defaulting to ``DEFAULT_TZ``.
    """
    name = os.environ.get('ECOWITT_TZ') or DEFAULT_TZ
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning(
            'Invalid ECOWITT_TZ %r; falling back to %s', name, DEFAULT_TZ
        )
        return ZoneInfo(DEFAULT_TZ)


#: Timezone applied to naive gateway timestamps (see ``_resolve_tz``).
ECOWITT_TZ = _resolve_tz()

#: HTTP request timeout, in seconds, for calls to the gateway.
REQUEST_TIMEOUT: int = 10

#: ``get_sensors_info`` ``id`` values that indicate an absent/unusable slot.
UNPAIRED_ID: str = 'FFFFFFFF'
DISABLED_ID: str = 'FFFFFFFE'

#: Fallback number of ``get_sensors_info`` pages if /get_version doesn't say.
SENSOR_PAGES_FALLBACK: int = 4


class LabeledGaugeMetricFamily(Metric):
    """Not sure why the upstream one doesn't allow labels..."""

    def __init__(
        self,
        name: str,
        documentation: str,
        value: Optional[float] = None,
        labels: Dict[str, str] = None,
        unit: str = '',
    ):
        Metric.__init__(self, name, documentation, 'gauge', unit)
        if labels is None:
            labels = {}
        self._labels = labels
        if value is not None:
            self.add_metric(labels, value)

    def add_metric(self, labels: Dict[str, str], value: float) -> None:
        """Add a metric to the metric family.
        Args:
          labels: A dictionary of labels
          value: A float
        """
        self.samples.append(
            Sample(self.name, dict(labels | self._labels), value, None)
        )


class UnknownUnitException(Exception):
    """Raised when a value's unit string has no entry in ``UNIT_TO_BASE``."""

    def __init__(self, unit: str):
        self.unit = unit
        super().__init__(f'Unknown unit string: {unit!r}')


#: Map of gateway unit string -> callable converting that reading to its base
#: SI unit. The gateway returns readings already converted to whatever display
#: units it is configured for (imperial *or* metric), so we normalize here to a
#: single base unit per dimension. A ``None`` unit (bare number: degrees,
#: index, count) is treated as identity by :func:`convert_to_base`.
UNIT_TO_BASE: Dict[str, Callable[[float], float]] = {
    # temperature -> celsius
    'F': lambda x: (x - 32) * 5 / 9,
    'C': lambda x: x,
    # wind speed -> meters per second
    'mph': lambda x: x * 0.44704,
    'km/h': lambda x: x * 0.277778,
    'm/s': lambda x: x,
    # pressure -> pascals
    'inHg': lambda x: x * 3386.389,
    'hPa': lambda x: x * 100,
    'mbar': lambda x: x * 100,
    'kPa': lambda x: x * 1000,
    'mmHg': lambda x: x * 133.322,
    # rain depth / rate -> millimeters (rate: mm per hour)
    'in': lambda x: x * 25.4,
    'in/Hr': lambda x: x * 25.4,
    'mm': lambda x: x,
    'mm/Hr': lambda x: x,
    # distance -> meters
    'mi': lambda x: x * 1609.344,
    'km': lambda x: x * 1000,
    # dimensionless / already-base
    '%': lambda x: x,
    'W/m2': lambda x: x,
}


def convert_to_base(value: float, unit: Optional[str]) -> float:
    """Convert ``value`` in ``unit`` to its base SI unit.

    :raises UnknownUnitException: if ``unit`` is not identity and not in
        :data:`UNIT_TO_BASE`.
    """
    if unit is None:
        return value
    try:
        return UNIT_TO_BASE[unit](value)
    except KeyError:
        raise UnknownUnitException(unit)


def parse_measurement(s: str) -> Tuple[float, Optional[str]]:
    """Parse a standalone reading string into ``(float_value, unit|None)``.

    Handles the three "unit is embedded in the value" shapes the gateway uses:
    a space-separated ``"1.34 mph"``, a percent-suffixed ``"72%"``, or a bare
    number ``"199"``.
    """
    s = str(s).strip()
    if ' ' in s:
        num, unit = s.split(None, 1)
        return float(num), unit.strip()
    if s.endswith('%'):
        return float(s[:-1]), '%'
    return float(s), None


def parse_value(
    entry: Dict[str, str], val_key: str = 'val'
) -> Tuple[float, Optional[str]]:
    """Parse ``entry[val_key]`` into ``(float_value, unit|None)``.

    If the entry carries a separate ``unit`` key it applies to ``val_key``;
    otherwise the unit (if any) is embedded in the value string itself. Note:
    only use this when the entry's ``unit`` key, if present, actually describes
    ``val_key`` (true for ``common_list``/``rain`` entries, but not for the
    ``wh25`` group where ``unit`` describes only ``intemp``).
    """
    if 'unit' in entry:
        return float(entry[val_key]), entry['unit']
    return parse_measurement(entry[val_key])


class MetricStore:
    """Accumulates samples into labeled gauge families, created on demand.

    ``collect()`` builds one of these per scrape, funnels every reading through
    :meth:`add` (which converts to base units) or :meth:`add_raw`, then yields
    the resulting families. ``unmapped`` counts readings skipped because their
    id or unit was unrecognized.
    """

    def __init__(self):
        self._families: Dict[str, LabeledGaugeMetricFamily] = {}
        self.unmapped: int = 0

    def _family(self, name: str, documentation: str) -> LabeledGaugeMetricFamily:
        fam = self._families.get(name)
        if fam is None:
            fam = LabeledGaugeMetricFamily(name, documentation)
            self._families[name] = fam
        return fam

    def add_raw(
        self, name: str, documentation: str, value: float,
        labels: Optional[Dict[str, str]] = None
    ) -> None:
        """Add an already-base-unit ``value`` as a sample of ``name``."""
        self._family(name, documentation).add_metric(labels or {}, value)

    def add(
        self, name: str, documentation: str, value: float,
        unit: Optional[str], labels: Optional[Dict[str, str]] = None
    ) -> None:
        """Convert ``value`` from ``unit`` to base units, then store it.

        On an unknown unit, logs a warning, increments :attr:`unmapped`, and
        drops the reading rather than raising.
        """
        try:
            base = convert_to_base(value, unit)
        except UnknownUnitException:
            logger.warning(
                'Unknown unit %r for metric %s (labels=%s); skipping',
                unit, name, labels
            )
            self.unmapped += 1
            return
        self.add_raw(name, documentation, base, labels)

    def families(self) -> List[LabeledGaugeMetricFamily]:
        return list(self._families.values())


#: ``common_list`` id -> (metric_name, documentation, labels). The unit is read
#: from each entry and normalized, so only the target metric + labels live here.
COMMON_ID_MAP: Dict[str, Tuple[str, str, Dict[str, str]]] = {
    '0x01': ('ecowitt_temperature_celsius', 'Temperature in degrees Celsius',
             {'sensor': 'indoor', 'channel': '', 'name': ''}),
    '0x02': ('ecowitt_temperature_celsius', 'Temperature in degrees Celsius',
             {'sensor': 'outdoor', 'channel': '', 'name': ''}),
    '0x03': ('ecowitt_dew_point_celsius', 'Dew point in degrees Celsius',
             {'sensor': 'outdoor'}),
    '0x04': ('ecowitt_wind_chill_celsius', 'Wind chill in degrees Celsius',
             {'sensor': 'outdoor'}),
    '0x05': ('ecowitt_heat_index_celsius', 'Heat index in degrees Celsius',
             {'sensor': 'outdoor'}),
    '3': ('ecowitt_apparent_temperature_celsius',
          'Apparent ("feels like") temperature in degrees Celsius',
          {'sensor': 'outdoor'}),
    # id "5" is vapor pressure deficit in kPa: confirmed against the live unit
    # by computing VPD from outdoor temp + humidity (Tetens), which matched the
    # reported value to three decimals.
    '5': ('ecowitt_vapor_pressure_deficit_pascals',
          'Vapor pressure deficit in pascals', {}),
    '0x06': ('ecowitt_humidity_percent', 'Relative humidity in percent',
             {'sensor': 'indoor', 'channel': '', 'name': ''}),
    '0x07': ('ecowitt_humidity_percent', 'Relative humidity in percent',
             {'sensor': 'outdoor', 'channel': '', 'name': ''}),
    '0x08': ('ecowitt_pressure_absolute_pascals',
             'Absolute barometric pressure in pascals', {'sensor': 'outdoor'}),
    '0x09': ('ecowitt_pressure_relative_pascals',
             'Relative barometric pressure in pascals', {'sensor': 'outdoor'}),
    '0x0A': ('ecowitt_wind_direction_degrees', 'Wind direction in degrees', {}),
    '0x0B': ('ecowitt_wind_speed_mps', 'Wind speed in meters per second', {}),
    '0x0C': ('ecowitt_wind_gust_mps', 'Wind gust speed in meters per second',
             {}),
    '0x19': ('ecowitt_wind_max_daily_mps',
             'Daily maximum wind speed in meters per second', {}),
    '0x15': ('ecowitt_solar_radiation_wm2',
             'Solar radiation in watts per square meter', {}),
    '0x16': ('ecowitt_uv_microwatts_per_m2',
             'Ultraviolet radiation in microwatts per square meter', {}),
    '0x17': ('ecowitt_uv_index', 'Ultraviolet index (0-15)', {}),
    # id 0x6D confirmed against the gateway UI as the 10-minute average wind
    # direction (the UI's "10 Min. Avg Wind Direction"); a bearing in degrees.
    '0x6D': ('ecowitt_wind_direction_10min_average_degrees',
             '10-minute average wind direction in degrees', {}),
}

#: ``rain``/``piezoRain`` id -> period label. ``rate`` becomes its own rate
#: metric; every other period is a sample of ``ecowitt_rain_millimeters``.
RAIN_ID_MAP: Dict[str, str] = {
    '0x0D': 'event',
    '0x0E': 'rate',
    # 0x7C = hourly rain: it is the one live accumulation id with no counterpart
    # in the gateway's labeled get_rain_totals / get_piezo_rain endpoints (which
    # expose only day/week/month/year), leaving hourly as the sole fit.
    '0x7C': 'hourly',
    '0x10': 'day',
    '0x11': 'week',
    '0x12': 'month',
    '0x13': 'year',
    '0x14': 'total',
}

RAIN_MM_DOC = 'Rain accumulation in millimeters'
RAIN_RATE_DOC = 'Rain rate in millimeters per hour'
BATTERY_VOLTS_DOC = 'Sensor battery voltage in volts'
BATTERY_LEVEL_DOC = 'Sensor battery level (raw; 0-5 where applicable)'
TEMP_DOC = 'Temperature in degrees Celsius'
HUMIDITY_DOC = 'Relative humidity in percent'


class EcowittCollector:
    """prometheus_client Collector that pull-scrapes an Ecowitt gateway.

    Each Prometheus scrape drives one :meth:`collect`, which fetches the live
    data and sensor registry over the gateway's local HTTP JSON API, normalizes
    every reading to base SI units, and yields the resulting metric families.
    """

    def _env_or_err(self, name: str) -> str:
        s: str = os.environ.get(name)
        if not s:
            raise RuntimeError(
                f'ERROR: You must set the "{name}" environment variable.'
            )
        return s

    def __init__(self):
        logger.debug('Instantiating EcowittCollector')
        self.host: str = self._env_or_err('ECOWITT_HOST')
        self.base_url: str = f'http://{self.host}'
        logger.info('Ecowitt gateway base URL: %s', self.base_url)
        # /get_version is static per firmware; fetched once and cached. It
        # supplies both the firmware info and the sensor-registry page count.
        self._version_data: Optional[dict] = None

    def _get(self, path: str) -> dict:
        url = self.base_url + path
        logger.debug('GET %s', url)
        r = requests.get(url, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.json()

    def _get_version_data(self) -> dict:
        """Fetch and cache /get_version (static per firmware)."""
        if self._version_data is None:
            self._version_data = self._get('/get_version')
        return self._version_data

    def _get_all_sensors(self) -> List[dict]:
        """Fetch every page of the sensor registry, deduplicated.

        The registry is paginated; the number of pages is reported by
        /get_version's ``sensorid_page`` (this firmware has 4 pages of 16 slots).
        Paging past the last page keeps returning data (it wraps) rather than an
        empty list, so we page by that count and dedupe on the per-slot ``type``
        enum defensively.
        """
        try:
            pages = int(self._get_version_data().get('sensorid_page'))
        except (TypeError, ValueError):
            pages = SENSOR_PAGES_FALLBACK
        if pages < 1:
            pages = SENSOR_PAGES_FALLBACK
        entries: List[dict] = []
        seen = set()
        for page in range(1, pages + 1):
            data = self._get(f'/get_sensors_info?page={page}')
            if not isinstance(data, list):
                continue
            for e in data:
                key = e.get('type')
                if key in seen:
                    continue
                seen.add(key)
                entries.append(e)
        return entries

    def collect(self) -> Generator[Metric, None, None]:
        """Yield all metric families for one scrape.

        Never raises: any failure is logged, reflected in
        ``ecowitt_scrape_success 0``, and the meta metrics are still emitted so
        ``/metrics`` stays up even when the gateway is unreachable.
        """
        logger.debug('Beginning collection')
        store = MetricStore()
        success = 1
        start = time.time()
        try:
            self._handle_livedata(store, self._get('/get_livedata_info'))
            self._handle_sensors(store, self._get_all_sensors())
            self._handle_firmware(store)
        except Exception as ex:
            logger.error('Scrape failed: %s', ex, exc_info=True)
            success = 0
        duration = time.time() - start

        for fam in store.families():
            yield fam
        yield GaugeMetricFamily(
            'ecowitt_scrape_duration_seconds',
            'Duration of the gateway scrape in seconds', value=duration
        )
        yield GaugeMetricFamily(
            'ecowitt_scrape_success',
            '1 if the last scrape succeeded, 0 otherwise', value=success
        )
        yield GaugeMetricFamily(
            'ecowitt_unmapped_fields',
            'Count of fields skipped this scrape because their id or unit '
            'was unrecognized', value=store.unmapped
        )
        logger.debug('Finished collection (success=%s)', success)

    # -- live data (get_livedata_info) ------------------------------------

    def _handle_livedata(self, store: MetricStore, data: dict) -> None:
        handlers: List[Tuple[str, Callable]] = [
            ('common_list', self._handle_common),
            ('wh25', self._handle_wh25),
            ('lightning', self._handle_lightning),
            ('ch_soil', self._handle_soil),
            ('ch_aisle', lambda s, g: self._handle_th_channels(s, g, 'aisle')),
            ('ch_temp', lambda s, g: self._handle_temp_channels(s, g, 'temp')),
            ('ch_leaf', self._handle_leaf),
            ('ch_leak', self._handle_leak),
            ('ch_pm25', self._handle_pm25_channels),
            ('ch_lds', self._handle_lds),
            ('co2', self._handle_co2),
            ('debug', self._handle_debug),
        ]
        for key, handler in handlers:
            if data.get(key):
                handler(store, data[key])
        # rain groups share a handler, distinguished by the "gauge" label
        if data.get('rain'):
            self._handle_rain(store, data['rain'], 'traditional')
        if data.get('piezoRain'):
            self._handle_rain(store, data['piezoRain'], 'piezo')

    def _handle_common(self, store: MetricStore, entries: List[dict]) -> None:
        for e in entries:
            _id = e.get('id')
            mapped = COMMON_ID_MAP.get(_id)
            if mapped is None:
                logger.warning(
                    'Unmapped common_list id %s (val=%s)', _id, e.get('val')
                )
                store.unmapped += 1
                continue
            name, doc, labels = mapped
            try:
                value, unit = parse_value(e)
            except (ValueError, KeyError) as ex:
                logger.warning('Cannot parse common_list %s: %s', _id, ex)
                store.unmapped += 1
                continue
            store.add(name, doc, value, unit, dict(labels))
            # some common_list entries carry an inline low-voltage flag (e.g.
            # the outdoor array on 0x03); a 0/1 flag, distinct from a 0-5 level,
            # so expose it separately, tagged with its source id for provenance.
            if 'battery' in e:
                try:
                    store.add_raw(
                        'ecowitt_sensor_battery_flag',
                        'Inline low-battery flag from a common_list entry '
                        '(typically 1 = low); source id in the label',
                        float(e['battery']), {'source_id': _id}
                    )
                except ValueError:
                    pass

    def _handle_rain(
        self, store: MetricStore, entries: List[dict], gauge: str
    ) -> None:
        for e in entries:
            _id = e.get('id')
            if _id == 'srain_piezo':
                # not an accumulation: a state flag on the piezo gauge
                try:
                    store.add_raw(
                        'ecowitt_piezo_rain_state',
                        'Piezo (haptic) rain sensor state '
                        '(1 = rain currently detected, 0 = dry)',
                        float(e['val']), {}
                    )
                except (ValueError, KeyError):
                    store.unmapped += 1
                continue
            period = RAIN_ID_MAP.get(_id)
            if period is None:
                logger.warning(
                    'Unmapped %s rain id %s (val=%s)', gauge, _id, e.get('val')
                )
                store.unmapped += 1
            else:
                try:
                    value, unit = parse_value(e)
                except (ValueError, KeyError) as ex:
                    logger.warning('Cannot parse rain %s: %s', _id, ex)
                    store.unmapped += 1
                else:
                    if period == 'rate':
                        store.add(
                            'ecowitt_rain_rate_mm_per_hour', RAIN_RATE_DOC,
                            value, unit, {'gauge': gauge}
                        )
                    else:
                        store.add(
                            'ecowitt_rain_millimeters', RAIN_MM_DOC, value,
                            unit, {'period': period, 'gauge': gauge}
                        )
            # rain gauge sensors report their battery inline (e.g. on 0x13)
            self._emit_battery(
                store, e, {'sensor': f'rain_{gauge}', 'channel': '', 'name': ''}
            )

    def _handle_wh25(self, store: MetricStore, entries: List[dict]) -> None:
        # {intemp, unit, inhumi, abs, rel} -- the "unit" key describes intemp
        # only, so parse the other fields individually.
        labels = {'sensor': 'indoor', 'channel': '', 'name': ''}
        for e in entries:
            if 'intemp' in e and 'unit' in e:
                store.add(
                    'ecowitt_temperature_celsius', TEMP_DOC,
                    float(e['intemp']), e['unit'], dict(labels)
                )
            if 'inhumi' in e:
                val, unit = parse_measurement(e['inhumi'])
                store.add(
                    'ecowitt_humidity_percent', HUMIDITY_DOC, val, unit,
                    dict(labels)
                )
            if 'abs' in e:
                val, unit = parse_measurement(e['abs'])
                store.add(
                    'ecowitt_pressure_absolute_pascals',
                    'Absolute barometric pressure in pascals', val, unit,
                    {'sensor': 'indoor'}
                )
            if 'rel' in e:
                val, unit = parse_measurement(e['rel'])
                store.add(
                    'ecowitt_pressure_relative_pascals',
                    'Relative barometric pressure in pascals', val, unit,
                    {'sensor': 'indoor'}
                )

    def _handle_lightning(self, store: MetricStore, entries: List[dict]) -> None:
        for e in entries:
            if 'distance' in e:
                val, unit = parse_measurement(e['distance'])
                store.add(
                    'ecowitt_lightning_distance_meters',
                    'Distance to last detected lightning strike in meters',
                    val, unit, {}
                )
            if 'count' in e:
                try:
                    store.add_raw(
                        'ecowitt_lightning_strike_count',
                        'Lightning strike count', float(e['count'])
                    )
                except ValueError:
                    pass
            if e.get('date'):
                ts = self._parse_iso_timestamp(e['date'])
                if ts is not None:
                    store.add_raw(
                        'ecowitt_lightning_last_strike_timestamp_seconds',
                        'Unix timestamp of the last detected lightning strike',
                        ts
                    )
            self._emit_battery(
                store, e, {'sensor': 'lightning', 'channel': '', 'name': ''}
            )

    def _handle_soil(self, store: MetricStore, entries: List[dict]) -> None:
        for e in entries:
            ch = e.get('channel', '')
            name = e.get('name', '')
            if 'humidity' in e:
                val, unit = parse_measurement(e['humidity'])
                store.add(
                    'ecowitt_soil_moisture_percent',
                    'Soil moisture in percent', val, unit,
                    {'channel': ch, 'name': name}
                )
            self._emit_battery(
                store, e, {'sensor': 'soil', 'channel': ch, 'name': name}
            )

    def _handle_th_channels(
        self, store: MetricStore, entries: List[dict], sensor: str
    ) -> None:
        """Multi-channel temp+humidity sensors (WH31 ``ch_aisle``)."""
        for e in entries:
            ch = e.get('channel', '')
            name = e.get('name', '')
            labels = {'sensor': sensor, 'channel': ch, 'name': name}
            if 'temp' in e and 'unit' in e:
                store.add(
                    'ecowitt_temperature_celsius', TEMP_DOC,
                    float(e['temp']), e['unit'], dict(labels)
                )
            if 'humidity' in e:
                val, unit = parse_measurement(e['humidity'])
                store.add(
                    'ecowitt_humidity_percent', HUMIDITY_DOC, val, unit,
                    dict(labels)
                )
            self._emit_battery(store, e, dict(labels))

    def _handle_temp_channels(
        self, store: MetricStore, entries: List[dict], sensor: str
    ) -> None:
        """Multi-channel temperature-only sensors (WH34 ``ch_temp``)."""
        for e in entries:
            ch = e.get('channel', '')
            name = e.get('name', '')
            labels = {'sensor': sensor, 'channel': ch, 'name': name}
            if 'temp' in e and 'unit' in e:
                store.add(
                    'ecowitt_temperature_celsius', TEMP_DOC,
                    float(e['temp']), e['unit'], dict(labels)
                )
            self._emit_battery(store, e, dict(labels))

    def _handle_leaf(self, store: MetricStore, entries: List[dict]) -> None:
        """Leaf wetness sensors (WH35 ``ch_leaf``)."""
        for e in entries:
            ch = e.get('channel', '')
            name = e.get('name', '')
            if 'humidity' in e:
                val, unit = parse_measurement(e['humidity'])
                store.add(
                    'ecowitt_leaf_wetness_percent',
                    'Leaf wetness in percent', val, unit,
                    {'channel': ch, 'name': name}
                )
            self._emit_battery(
                store, e, {'sensor': 'leaf', 'channel': ch, 'name': name}
            )

    def _handle_leak(self, store: MetricStore, entries: List[dict]) -> None:
        """Water leak sensors (WH55 ``ch_leak``)."""
        for e in entries:
            ch = e.get('channel', '')
            name = e.get('name', '')
            # field name for the leak state varies; expose any present.
            for key in ('status', 'leak'):
                if key in e:
                    try:
                        store.add_raw(
                            'ecowitt_water_leak_state',
                            'Water leak state (0 = normal, non-zero = leak)',
                            float(e[key]), {'channel': ch, 'name': name}
                        )
                    except ValueError:
                        pass
                    break
            self._emit_battery(
                store, e, {'sensor': 'leak', 'channel': ch, 'name': name}
            )

    def _handle_pm25_channels(
        self, store: MetricStore, entries: List[dict]
    ) -> None:
        """Multi-channel PM2.5 sensors (WH41 ``ch_pm25``)."""
        for e in entries:
            ch = e.get('channel', '')
            name = e.get('name', '')
            for key in ('PM25', 'pm25'):
                if key in e:
                    val, unit = parse_measurement(str(e[key]))
                    store.add(
                        'ecowitt_pm25_micrograms_per_m3',
                        'PM2.5 concentration in micrograms per cubic meter',
                        val, unit, {'channel': ch, 'name': name}
                    )
                    break
            self._emit_battery(
                store, e, {'sensor': 'pm25', 'channel': ch, 'name': name}
            )

    def _handle_lds(self, store: MetricStore, entries: List[dict]) -> None:
        """Laser distance sensors (WH54 ``ch_lds``); ``air`` + ``depth``."""
        for e in entries:
            ch = e.get('channel', '')
            name = e.get('name', '')
            unit = e.get('unit')
            labels = {'channel': ch, 'name': name}
            for field, metric, doc in (
                ('air', 'ecowitt_lds_air_distance_millimeters',
                 'LDS air gap distance in millimeters'),
                ('depth', 'ecowitt_lds_depth_millimeters',
                 'LDS measured depth in millimeters'),
            ):
                if field in e:
                    try:
                        store.add(
                            metric, doc, float(e[field]), unit, dict(labels)
                        )
                    except ValueError:
                        pass
            self._emit_battery(
                store, e, {'sensor': 'lds', 'channel': ch, 'name': name}
            )

    def _handle_co2(self, store: MetricStore, entries: List[dict]) -> None:
        """Air-quality combo sensor (WH45 ``co2``)."""
        # field-name (lowercased) -> (metric, documentation)
        field_map = {
            'co2': ('ecowitt_co2_ppm',
                    'CO2 concentration in parts per million'),
            'co2_24h': ('ecowitt_co2_24h_ppm',
                        '24-hour average CO2 concentration in ppm'),
            'pm1': ('ecowitt_pm1_micrograms_per_m3',
                    'PM1 concentration in micrograms per cubic meter'),
            'pm25': ('ecowitt_pm25_micrograms_per_m3',
                     'PM2.5 concentration in micrograms per cubic meter'),
            'pm4': ('ecowitt_pm4_micrograms_per_m3',
                    'PM4 concentration in micrograms per cubic meter'),
            'pm10': ('ecowitt_pm10_micrograms_per_m3',
                     'PM10 concentration in micrograms per cubic meter'),
        }
        for e in entries:
            for key, val in e.items():
                lkey = key.lower()
                if lkey in ('temp', 'temperature') and 'unit' in e:
                    store.add(
                        'ecowitt_temperature_celsius', TEMP_DOC,
                        float(val), e['unit'],
                        {'sensor': 'co2', 'channel': '', 'name': ''}
                    )
                elif lkey == 'humidity':
                    v, u = parse_measurement(str(val))
                    store.add(
                        'ecowitt_humidity_percent', HUMIDITY_DOC, v, u,
                        {'sensor': 'co2', 'channel': '', 'name': ''}
                    )
                elif lkey in field_map:
                    name, doc = field_map[lkey]
                    try:
                        v, u = parse_measurement(str(val))
                    except ValueError:
                        continue
                    store.add(name, doc, v, u, {})
            self._emit_battery(
                store, e, {'sensor': 'co2', 'channel': '', 'name': ''}
            )

    def _handle_debug(self, store: MetricStore, entries: List[dict]) -> None:
        """Gateway-internal stats from the ``debug`` group."""
        # numeric field -> (metric, documentation)
        field_map = {
            'heap': ('ecowitt_gateway_free_heap_bytes',
                     'Gateway free heap memory in bytes'),
            'runtime': ('ecowitt_gateway_runtime_seconds',
                        'Gateway uptime in seconds since boot'),
            'usr_interval': ('ecowitt_gateway_sensor_interval_seconds',
                             'Gateway sensor data update interval in seconds'),
        }
        for e in entries:
            for key, (metric, doc) in field_map.items():
                if key in e:
                    try:
                        store.add_raw(metric, doc, float(e[key]))
                    except (ValueError, TypeError):
                        pass
            if 'is_cnip' in e:
                store.add_raw(
                    'ecowitt_gateway_is_cnip',
                    'Gateway is_cnip flag (1 = true, 0 = false)',
                    1.0 if e['is_cnip'] else 0.0
                )

    def _emit_battery(
        self, store: MetricStore, entry: dict, labels: Dict[str, str]
    ) -> None:
        """Emit battery voltage/level metrics for any sensor entry that has them.

        ``voltage`` (volts) is the unambiguous one; ``battery`` is a raw level
        (typically 0-5) whose exact semantics vary by sensor family, so it is
        exposed as-is and left for the alert layer to threshold.
        """
        if 'voltage' in entry:
            try:
                store.add_raw(
                    'ecowitt_sensor_battery_volts', BATTERY_VOLTS_DOC,
                    float(entry['voltage']), dict(labels)
                )
            except ValueError:
                pass
        if 'battery' in entry:
            try:
                store.add_raw(
                    'ecowitt_sensor_battery_level', BATTERY_LEVEL_DOC,
                    float(entry['battery']), dict(labels)
                )
            except ValueError:
                pass
        # WS90 (piezo/haptic array) reports its supercapacitor voltage inline
        if 'ws90cap_volt' in entry:
            try:
                store.add_raw(
                    'ecowitt_sensor_capacitor_volts',
                    'Sensor supercapacitor voltage in volts (WS90)',
                    float(entry['ws90cap_volt']), dict(labels)
                )
            except ValueError:
                pass

    # -- sensor registry (get_sensors_info) -------------------------------

    def _handle_sensors(self, store: MetricStore, entries: List[dict]) -> None:
        for e in entries:
            sensor = e.get('img', '')
            name = e.get('name', '')
            _id = e.get('id', '')
            labels = {'sensor': sensor, 'name': name, 'id': _id}
            present = 1.0 if _id not in (UNPAIRED_ID, DISABLED_ID) else 0.0
            store.add_raw(
                'ecowitt_sensor_present',
                '1 if a sensor is paired in this slot, 0 otherwise',
                present, dict(labels)
            )
            if present:
                rssi = e.get('rssi')
                # absent sensors report rssi/signal as "--"; only real values here
                if rssi not in (None, '', '--'):
                    try:
                        store.add_raw(
                            'ecowitt_sensor_rssi_dbm',
                            'Sensor received signal strength in dBm',
                            float(rssi), dict(labels)
                        )
                    except ValueError:
                        pass
                signal = e.get('signal')
                if signal not in (None, '', '--'):
                    try:
                        store.add_raw(
                            'ecowitt_sensor_signal',
                            'Sensor signal quality (0-4 bars) from the registry',
                            float(signal), dict(labels)
                        )
                    except ValueError:
                        pass
                # registry battery: raw "batt" from get_sensors_info. Distinct
                # from the live-data battery level/voltage -- its semantics vary
                # by sensor family (0-5 level for most, a 0/1 flag for some).
                batt = e.get('batt')
                if batt not in (None, ''):
                    try:
                        store.add_raw(
                            'ecowitt_sensor_registry_battery',
                            'Raw battery value from the sensor registry '
                            '(get_sensors_info "batt"; semantics vary by family)',
                            float(batt), dict(labels)
                        )
                    except ValueError:
                        pass
                if e.get('version'):
                    store.add_raw(
                        'ecowitt_sensor_info',
                        'Sensor firmware version info (constant 1)',
                        1.0,
                        {**labels, 'version': e['version']}
                    )

    # -- firmware (get_version) -------------------------------------------

    def _handle_firmware(self, store: MetricStore) -> None:
        data = self._get_version_data()
        # e.g. "Version: GW3000B_V1.2.1" -> "GW3000B_V1.2.1"
        version = data.get('version', '')
        if ':' in version:
            version = version.split(':', 1)[1].strip()
        platform = data.get('platform', '')
        store.add_raw(
            'ecowitt_firmware_info',
            'Gateway firmware version info (constant 1)', 1.0,
            {'version': version, 'platform': platform}
        )
        # newVersion is "1" when the gateway sees an available firmware update
        try:
            store.add_raw(
                'ecowitt_gateway_firmware_update_available',
                '1 if the gateway reports an available firmware update, else 0',
                float(data.get('newVersion', 0))
            )
        except (ValueError, TypeError):
            pass

    @staticmethod
    def _parse_iso_timestamp(value: str) -> Optional[float]:
        """Parse the gateway's local ISO date (no tz) to a Unix timestamp.

        The gateway emits a naive local wall-clock string with no timezone
        (e.g. ``2026-07-11T15:14:03``). We attach the configured local zone
        (``ECOWITT_TZ``) before converting, so the resulting epoch is correct
        year-round (DST-aware) regardless of the container's own timezone --
        which is otherwise assumed by ``datetime.timestamp()`` and is typically
        UTC, yielding a timestamp offset by the local UTC offset.
        """
        try:
            dt = datetime.fromisoformat(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=ECOWITT_TZ)
            return dt.timestamp()
        except (ValueError, TypeError):
            logger.warning('Cannot parse timestamp %r', value)
            return None


def _get_best_family(address, port):
    """
    Automatically select address family depending on address
    copied from prometheus_client.exposition.start_http_server
    """
    # HTTPServer defaults to AF_INET, which will not start properly if
    # binding an ipv6 address is requested.
    # This function is based on what upstream python did for http.server
    # in https://github.com/python/cpython/pull/11767
    infos = socket.getaddrinfo(address, port)
    family, _, _, _, sockaddr = next(iter(infos))
    return family, sockaddr[0]


def serve_exporter(port: int, addr: str = '0.0.0.0'):
    """
    Copied from prometheus_client.exposition.start_http_server, but doesn't run
    in a thread because we're just a proxy.
    """

    class TmpServer(WSGIServer):
        """Copy of WSGIServer to update address_family locally"""

    TmpServer.address_family, addr = _get_best_family(addr, port)
    app = make_wsgi_app(REGISTRY)
    httpd = make_server(
        addr, port, app, TmpServer, handler_class=_SilentHandler
    )
    httpd.serve_forever()


def parse_args(argv):
    p = argparse.ArgumentParser(description='Prometheus Ecowitt exporter')
    p.add_argument(
        '-v', '--verbose', dest='verbose', action='count', default=0,
        help='verbose output. specify twice for debug-level output.'
    )
    port_def = int(os.environ.get('PORT', '8000'))
    p.add_argument(
        '-p', '--port', dest='port', action='store', type=int,
        default=port_def, help=f'Port to listen on (default: {port_def})'
    )
    args = p.parse_args(argv)
    return args


def set_log_info():
    set_log_level_format(
        logging.INFO, '%(asctime)s %(levelname)s:%(name)s:%(message)s'
    )


def set_log_debug():
    set_log_level_format(
        logging.DEBUG,
        "%(asctime)s [%(levelname)s %(filename)s:%(lineno)s - "
        "%(name)s.%(funcName)s() ] %(message)s"
    )


def set_log_level_format(level: int, fmt: str):
    """
    Set logger level and format.

    :param level: logging level; see the :py:mod:`logging` constants.
    :type level: int
    :param fmt: logging formatter format string
    :type fmt: str
    """
    formatter = logging.Formatter(fmt=fmt)
    logger.handlers[0].setFormatter(formatter)
    logger.setLevel(level)


if __name__ == "__main__":
    args = parse_args(sys.argv[1:])
    if args.verbose > 1:
        set_log_debug()
    elif args.verbose == 1:
        set_log_info()
    logger.debug('Registering collector...')
    REGISTRY.register(EcowittCollector())
    logger.info('Starting HTTP server on port %d', args.port)
    serve_exporter(args.port)
