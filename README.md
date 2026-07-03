# prometheus-ecowitt-exporter

A Docker-based Prometheus exporter for Ecowitt weather station gateways (GW3000/GW3010 family).

[![Project Status: WIP – Initial development is in progress, but there has not yet been a stable, usable release suitable for the public.](https://www.repostatus.org/badges/latest/wip.svg)](https://www.repostatus.org/#wip)

**IMPORTANT:** This is a personal project only. PRs are accepted, but this is not supported and "issues" will likely not be fixed or responded to. This is only for people who understand the details of everything involved.

It polls the gateway's local HTTP JSON API (the same undocumented endpoints the device's own web UI uses) and exposes normalized, **base-SI** weather and sensor-health metrics for Prometheus to scrape. Metrics are generated on-demand per scrape (pull-through — there is no background polling thread), so the scrape interval is entirely up to Prometheus.

Because the gateway returns readings already converted to whatever display units it is configured for (imperial *or* metric), this exporter normalizes everything to a single base unit per dimension (Celsius, m/s, pascals, millimeters, meters, etc.).

## Usage

### Docker

```
docker run -p 8000:8000 \
    -e ECOWITT_HOST=ecowitt.example.com \
    ghcr.io/jantman/prometheus-ecowitt-exporter:latest
```

Then scrape `http://<host>:8000/metrics`.

### Environment Variables

* `ECOWITT_HOST` (**required**) — hostname or IP of the Ecowitt gateway, e.g. `ecowitt.example.com` or `10.0.0.5`. The base URL used is `http://{ECOWITT_HOST}/`.
* `PORT` (*optional*, default `8000`) — port the exporter listens on.

### Running Locally

```
python3 -mvenv venv
source venv/bin/activate
pip install -r requirements.txt
ECOWITT_HOST=ecowitt.example.com PORT=8000 ./main.py
```

## Debugging

For debugging, append `-v` (INFO) or `-vv` (DEBUG) to your `docker run` command or local invocation, to increase log verbosity. Debug logging includes each gateway URL fetched and any skipped/unmapped fields.

## Metrics Exposed

All metrics are gauges with the `ecowitt_` prefix and base-unit-suffixed names. An example of the full `/metrics` output can be seen in [example.prom](example.prom).

### Weather

Temperature/humidity carry a `sensor` label (`indoor`/`outdoor`, or a sensor-family tag such as `aisle`/`temp`/`co2` for multi-channel sensors) plus `channel` and `name` labels (empty for single-value sensors).

| Metric | Description |
|--------|-------------|
| `ecowitt_temperature_celsius` | Temperature |
| `ecowitt_dew_point_celsius` | Dew point |
| `ecowitt_heat_index_celsius` | Heat index |
| `ecowitt_wind_chill_celsius` | Wind chill |
| `ecowitt_apparent_temperature_celsius` | Apparent ("feels like") temperature |
| `ecowitt_humidity_percent` | Relative humidity |
| `ecowitt_pressure_absolute_pascals` | Absolute barometric pressure |
| `ecowitt_pressure_relative_pascals` | Relative (sea-level) barometric pressure |
| `ecowitt_wind_speed_mps` | Wind speed |
| `ecowitt_wind_gust_mps` | Wind gust speed |
| `ecowitt_wind_max_daily_mps` | Daily maximum wind speed |
| `ecowitt_wind_direction_degrees` | Wind direction (instantaneous) |
| `ecowitt_wind_direction_10min_average_degrees` | 10-minute average wind direction (gateway id `0x6D`) |
| `ecowitt_solar_radiation_wm2` | Solar radiation |
| `ecowitt_uv_index` | UV index (0–15) |
| `ecowitt_uv_microwatts_per_m2` | UV radiation (if reported) |
| `ecowitt_vapor_pressure_deficit_pascals` | Vapor pressure deficit (gateway id `"5"`) |
| `ecowitt_rain_millimeters{period,gauge}` | Rain accumulation; `period` = `event`/`hourly`/`day`/`week`/`month`/`year`/`total`, `gauge` = `traditional`/`piezo` |
| `ecowitt_rain_rate_mm_per_hour{gauge}` | Rain rate |
| `ecowitt_piezo_rain_state` | Piezo (haptic) rain sensor state (1 = rain currently detected) |
| `ecowitt_soil_moisture_percent{channel,name}` | Soil moisture |
| `ecowitt_leaf_wetness_percent{channel,name}` | Leaf wetness (WH35) |
| `ecowitt_water_leak_state{channel,name}` | Water leak state (WH55; 0 = normal) |
| `ecowitt_pm25_micrograms_per_m3{channel,name}` | PM2.5 concentration |
| `ecowitt_lds_air_distance_millimeters{channel,name}` | Laser distance sensor air gap (WH54) |
| `ecowitt_lds_depth_millimeters{channel,name}` | Laser distance sensor depth (WH54) |
| `ecowitt_co2_ppm`, `ecowitt_co2_24h_ppm`, `ecowitt_pm1/pm4/pm10_micrograms_per_m3` | Air-quality combo (WH45) |
| `ecowitt_lightning_distance_meters` | Distance to last lightning strike |
| `ecowitt_lightning_strike_count` | Lightning strike count |
| `ecowitt_lightning_last_strike_timestamp_seconds` | Unix timestamp of last strike |

### Sensor Health

Signal/battery/presence for the registry metrics come from `get_sensors_info`, which the exporter reads across **all** of its pages (the page count comes from `get_version`'s `sensorid_page`), so every sensor slot — paired or not — is represented.

| Metric | Description |
|--------|-------------|
| `ecowitt_sensor_rssi_dbm{sensor,name,id}` | Received signal strength in dBm (registry) |
| `ecowitt_sensor_signal{sensor,name,id}` | Signal quality, 0–4 bars (registry) |
| `ecowitt_sensor_present{sensor,name,id}` | `1` if a sensor is paired in this slot, `0` otherwise |
| `ecowitt_sensor_info{sensor,name,id,version}` | Sensor firmware version info (constant `1`) |
| `ecowitt_sensor_battery_volts{sensor,channel,name}` | Battery voltage from a live-data `voltage` field (the unambiguous case) |
| `ecowitt_sensor_battery_level{sensor,channel,name}` | Battery level (`0–5`) from a live-data `battery` field |
| `ecowitt_sensor_registry_battery{sensor,name,id}` | Raw `batt` from the registry (`0–5` level for most families, a `0`/`1` flag for some) |
| `ecowitt_sensor_battery_flag{source_id}` | Inline low-battery flag from a `common_list` entry (typically `1` = low) |
| `ecowitt_sensor_capacitor_volts{sensor,channel,name}` | Supercapacitor voltage (WS90) |

> **Battery note:** battery encoding varies by sensor family and by source (a `0–5` level, a `voltage` in volts, or a `0`/`1` low-voltage flag). Rather than over-normalize, this exporter surfaces each source as its own metric (above); the same physical sensor may therefore appear in more than one. Low-battery *thresholding* is left to the alerting layer. Note the `sensor`/`name` labels differ by source: registry metrics use the gateway's slot name (e.g. `Soil moisture CH1`) while live-data metrics use your custom name (e.g. `BerriesFront`) — both are reported verbatim from the API.

### Gateway

Gateway-internal stats (from the `debug` group of `get_livedata_info` and from `get_version`):

| Metric | Description |
|--------|-------------|
| `ecowitt_gateway_free_heap_bytes` | Free heap memory in bytes |
| `ecowitt_gateway_runtime_seconds` | Uptime in seconds since boot |
| `ecowitt_gateway_sensor_interval_seconds` | Sensor data update interval in seconds |
| `ecowitt_gateway_is_cnip` | Gateway `is_cnip` flag (`1`/`0`) |
| `ecowitt_gateway_firmware_update_available` | `1` if the gateway reports an available firmware update |

### Meta

| Metric | Description |
|--------|-------------|
| `ecowitt_scrape_success` | `1` if the last scrape succeeded, `0` otherwise |
| `ecowitt_scrape_duration_seconds` | Duration of the gateway scrape |
| `ecowitt_unmapped_fields` | Count of fields skipped this scrape because their id or unit was unrecognized |
| `ecowitt_firmware_info{version,platform}` | Gateway firmware version info (constant `1`) |

A non-zero `ecowitt_unmapped_fields` means the gateway returned a field id or unit this exporter doesn't recognize yet; run with `-vv` to see which ones in the logs, and file/fix a mapping.

## Development

Clone the repo, then:

```
python3 -mvenv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Release Process

Tag the repo. [GitHub Actions](../../actions) will build a multi-arch (amd64 + arm64) image, push it to `ghcr.io/jantman/prometheus-ecowitt-exporter`, and create a GitHub release. After the first release, set the ghcr package visibility to **public** so the `:latest` image can be pulled unauthenticated.
