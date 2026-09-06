#!/usr/bin/env python3
"""
SolarEdge Individual Optimizer Irradiance Fetcher

Logs in directly to SolarEdge Monitoring Portal, requests real-time 15-minute
production power telemetry for all 31 site optimizers via the devices-measurements
endpoint, finds the peak optimizer wattage for the MOST RECENT time slot (unclipped by inverter AC limits),
and calculates real-time solar irradiance (W/m²) based on a 415W STC module rating.

Formula: Irradiance = (Latest_Slot_Max_Optimizer_Watts / 415.0) * 1000.0
"""

import sys
import os
import json
import urllib.request
import urllib.parse
import http.cookiejar
from datetime import datetime

MODULE_RATING_WATTS = 415.0
MAX_IRRADIANCE_CAP = 1200.0

# Pre-configured 31 site optimizers for Site 4514701
OPTIMIZER_DEVICES_PAYLOAD = [
    {"device":{"itemType":"OPTIMIZER","id":"15804E50-33","originalSerial":"15804E50-33","identifier":"15804E50","connectedToInverter":"7E1FDC4B-C4"},"deviceName":"Optimizer 1.1.1","measurementTypes":["PRODUCTION_POWER"]},
    {"device":{"itemType":"OPTIMIZER","id":"15804E4F-32","originalSerial":"15804E4F-32","identifier":"15804E4F","connectedToInverter":"7E1FDC4B-C4"},"deviceName":"Optimizer 1.1.2","measurementTypes":["PRODUCTION_POWER"]},
    {"device":{"itemType":"OPTIMIZER","id":"15804E4C-2F","originalSerial":"15804E4C-2F","identifier":"15804E4C","connectedToInverter":"7E1FDC4B-C4"},"deviceName":"Optimizer 1.1.3","measurementTypes":["PRODUCTION_POWER"]},
    {"device":{"itemType":"OPTIMIZER","id":"15804C6D-4E","originalSerial":"15804C6D-4E","identifier":"15804C6D","connectedToInverter":"7E1FDC4B-C4"},"deviceName":"Optimizer 1.1.4","measurementTypes":["PRODUCTION_POWER"]},
    {"device":{"itemType":"OPTIMIZER","id":"15804C6C-4D","originalSerial":"15804C6C-4D","identifier":"15804C6C","connectedToInverter":"7E1FDC4B-C4"},"deviceName":"Optimizer 1.1.5","measurementTypes":["PRODUCTION_POWER"]},
    {"device":{"itemType":"OPTIMIZER","id":"158036FB-C6","originalSerial":"158036FB-C6","identifier":"158036FB","connectedToInverter":"7E1FDC4B-C4"},"deviceName":"Optimizer 1.1.6","measurementTypes":["PRODUCTION_POWER"]},
    {"device":{"itemType":"OPTIMIZER","id":"157F7B42-51","originalSerial":"157F7B42-51","identifier":"157F7B42","connectedToInverter":"7E1FDC4B-C4"},"deviceName":"Optimizer 1.1.7","measurementTypes":["PRODUCTION_POWER"]},
    {"device":{"itemType":"OPTIMIZER","id":"15804EEF-D2","originalSerial":"15804EEF-D2","identifier":"15804EEF","connectedToInverter":"7E1FDC4B-C4"},"deviceName":"Optimizer 1.1.8","measurementTypes":["PRODUCTION_POWER"]},
    {"device":{"itemType":"OPTIMIZER","id":"15804F8D-71","originalSerial":"15804F8D-71","identifier":"15804F8D","connectedToInverter":"7E1FDC4B-C4"},"deviceName":"Optimizer 1.1.9","measurementTypes":["PRODUCTION_POWER"]},
    {"device":{"itemType":"OPTIMIZER","id":"15804FA4-88","originalSerial":"15804FA4-88","identifier":"15804FA4","connectedToInverter":"7E1FDC4B-C4"},"deviceName":"Optimizer 1.1.10","measurementTypes":["PRODUCTION_POWER"]},
    {"device":{"itemType":"OPTIMIZER","id":"15805259-40","originalSerial":"15805259-40","identifier":"15805259","connectedToInverter":"7E1FDC4B-C4"},"deviceName":"Optimizer 1.1.11","measurementTypes":["PRODUCTION_POWER"]},
    {"device":{"itemType":"OPTIMIZER","id":"15805213-FA","originalSerial":"15805213-FA","identifier":"15805213","connectedToInverter":"7E1FDC4B-C4"},"deviceName":"Optimizer 1.1.12","measurementTypes":["PRODUCTION_POWER"]},
    {"device":{"itemType":"OPTIMIZER","id":"15805377-5F","originalSerial":"15805377-5F","identifier":"15805377","connectedToInverter":"7E1FDC4B-C4"},"deviceName":"Optimizer 1.1.13","measurementTypes":["PRODUCTION_POWER"]},
    {"device":{"itemType":"OPTIMIZER","id":"15805440-29","originalSerial":"15805440-29","identifier":"15805440","connectedToInverter":"7E1FDC4B-C4"},"deviceName":"Optimizer 1.1.14","measurementTypes":["PRODUCTION_POWER"]},
    {"device":{"itemType":"OPTIMIZER","id":"158053D1-B9","originalSerial":"158053D1-B9","identifier":"158053D1","connectedToInverter":"7E1FDC4B-C4"},"deviceName":"Optimizer 1.1.15","measurementTypes":["PRODUCTION_POWER"]},
    {"device":{"itemType":"OPTIMIZER","id":"158053A7-8F","originalSerial":"158053A7-8F","identifier":"158053A7","connectedToInverter":"7E1FDC4B-C4"},"deviceName":"Optimizer 1.1.16","measurementTypes":["PRODUCTION_POWER"]},
    {"device":{"itemType":"OPTIMIZER","id":"158053A3-8B","originalSerial":"158053A3-8B","identifier":"158053A3","connectedToInverter":"7E1FDC4B-C4"},"deviceName":"Optimizer 1.1.17","measurementTypes":["PRODUCTION_POWER"]},
    {"device":{"itemType":"OPTIMIZER","id":"158053A5-8D","originalSerial":"158053A5-8D","identifier":"158053A5","connectedToInverter":"7E1FDC4B-C4"},"deviceName":"Optimizer 1.1.18","measurementTypes":["PRODUCTION_POWER"]},
    {"device":{"itemType":"OPTIMIZER","id":"15805388-70","originalSerial":"15805388-70","identifier":"15805388","connectedToInverter":"7E1FDC4B-C4"},"deviceName":"Optimizer 1.1.19","measurementTypes":["PRODUCTION_POWER"]},
    {"device":{"itemType":"OPTIMIZER","id":"158053CF-B7","originalSerial":"158053CF-B7","identifier":"158053CF","connectedToInverter":"7E1FDC4B-C4"},"deviceName":"Optimizer 1.1.20","measurementTypes":["PRODUCTION_POWER"]},
    {"device":{"itemType":"OPTIMIZER","id":"158053CE-B6","originalSerial":"158053CE-B6","identifier":"158053CE","connectedToInverter":"7E1FDC4B-C4"},"deviceName":"Optimizer 1.1.21","measurementTypes":["PRODUCTION_POWER"]},
    {"device":{"itemType":"OPTIMIZER","id":"158053A4-8C","originalSerial":"158053A4-8C","identifier":"158053A4","connectedToInverter":"7E1FDC4B-C4"},"deviceName":"Optimizer 1.1.22","measurementTypes":["PRODUCTION_POWER"]},
    {"device":{"itemType":"OPTIMIZER","id":"1580530A-F2","originalSerial":"1580530A-F2","identifier":"1580530A","connectedToInverter":"7E1FDC4B-C4"},"deviceName":"Optimizer 1.1.23","measurementTypes":["PRODUCTION_POWER"]},
    {"device":{"itemType":"OPTIMIZER","id":"15805212-F9","originalSerial":"15805212-F9","identifier":"15805212","connectedToInverter":"7E1FDC4B-C4"},"deviceName":"Optimizer 1.1.24","measurementTypes":["PRODUCTION_POWER"]},
    {"device":{"itemType":"OPTIMIZER","id":"15804EED-D0","originalSerial":"15804EED-D0","identifier":"15804EED","connectedToInverter":"7E1FDC4B-C4"},"deviceName":"Optimizer 1.1.25","measurementTypes":["PRODUCTION_POWER"]},
    {"device":{"itemType":"OPTIMIZER","id":"1580530C-F4","originalSerial":"1580530C-F4","identifier":"1580530C","connectedToInverter":"7E1FDC4B-C4"},"deviceName":"Optimizer 1.1.26","measurementTypes":["PRODUCTION_POWER"]},
    {"device":{"itemType":"OPTIMIZER","id":"158036FC-C7","originalSerial":"158036FC-C7","identifier":"158036FC","connectedToInverter":"7E1FDC4B-C4"},"deviceName":"Optimizer 1.1.27","measurementTypes":["PRODUCTION_POWER"]},
    {"device":{"itemType":"OPTIMIZER","id":"15804C17-F8","originalSerial":"15804C17-F8","identifier":"15804C17","connectedToInverter":"7E1FDC4B-C4"},"deviceName":"Optimizer 1.1.28","measurementTypes":["PRODUCTION_POWER"]},
    {"device":{"itemType":"OPTIMIZER","id":"15804E4B-2E","originalSerial":"15804E4B-2E","identifier":"15804E4B","connectedToInverter":"7E1FDC4B-C4"},"deviceName":"Optimizer 1.1.29","measurementTypes":["PRODUCTION_POWER"]},
    {"device":{"itemType":"OPTIMIZER","id":"15804E4D-30","originalSerial":"15804E4D-30","identifier":"15804E4D","connectedToInverter":"7E1FDC4B-C4"},"deviceName":"Optimizer 1.1.30","measurementTypes":["PRODUCTION_POWER"]},
    {"device":{"itemType":"OPTIMIZER","id":"1580544A-33","originalSerial":"1580544A-33","identifier":"1580544A","connectedToInverter":"7E1FDC4B-C4"},"deviceName":"Optimizer 1.1.31","measurementTypes":["PRODUCTION_POWER"]}
]


def load_secrets():
    """Load SolarEdge credentials from Home Assistant secrets.yaml."""
    possible_paths = [
        "/config/secrets.yaml",
        os.path.join(os.path.dirname(__file__), "..", "secrets.yaml"),
        os.path.join(os.getcwd(), "secrets.yaml"),
    ]

    secrets_file = None
    for p in possible_paths:
        if os.path.exists(p):
            secrets_file = p
            break

    if not secrets_file:
        return {}

    secrets = {}
    try:
        import yaml
        with open(secrets_file, "r", encoding="utf-8") as f:
            secrets = yaml.safe_load(f) or {}
    except Exception:
        with open(secrets_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and ":" in line:
                    k, v = line.split(":", 1)
                    secrets[k.strip()] = v.strip().strip("'\"")

    return secrets


def fetch_optimizer_measurements(site_id, username, password):
    """
    Authenticate with SolarEdge Monitoring APIGW and fetch live optimizer measurements.
    Returns (peak_optimizer_name, peak_optimizer_watts, timestamp).
    """
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    opener.addheaders = [
        ("User-Agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"),
        ("Accept", "application/json, text/plain, */*"),
        ("Origin", "https://monitoring.solaredge.com"),
        ("Referer", "https://monitoring.solaredge.com/one")
    ]

    # 1. Login to APIGW
    login_url = "https://monitoring.solaredge.com/solaredge-apigw/api/login"
    login_data = urllib.parse.urlencode({"j_username": username, "j_password": password}).encode("utf-8")
    l_req = urllib.request.Request(login_url, data=login_data, method="POST")
    l_req.add_header("Content-Type", "application/x-www-form-urlencoded")

    csrf_token = None
    with opener.open(l_req, timeout=15) as resp:
        csrf_token = dict(resp.headers).get("x-csrf-token")

    cookie_header = "; ".join([f"{c.name}={c.value}" for c in cj])

    # 2. Query devices-measurements
    today_str = datetime.now().strftime("%Y-%m-%d")
    measurements_url = f"https://monitoring.solaredge.com/services/charts/site/{site_id}/devices-measurements?start-date={today_str}&end-date={today_str}"

    body_data = json.dumps(OPTIMIZER_DEVICES_PAYLOAD).encode("utf-8")
    m_req = urllib.request.Request(measurements_url, data=body_data, method="POST")
    m_req.add_header("Content-Type", "application/json")
    m_req.add_header("Cookie", cookie_header)
    if csrf_token:
        m_req.add_header("X-CSRF-TOKEN", csrf_token)

    with opener.open(m_req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    # Group measurements by time slot
    ts_map = {}
    for opt in data:
        d_name = opt.get("deviceName", "")
        measurements = opt.get("measurements", [])
        for m in measurements:
            val = m.get("measurement")
            t_str = m.get("time")
            if val is not None and isinstance(val, (int, float)):
                if t_str not in ts_map:
                    ts_map[t_str] = []
                ts_map[t_str].append((d_name, float(val)))

    if not ts_map:
        return "", 0.0, datetime.now().isoformat()

    # Sort available timestamps and pick the MOST RECENT time slot
    sorted_ts = sorted(list(ts_map.keys()))
    latest_ts = sorted_ts[-1]

    # Find the peak optimizer wattage for this latest time slot
    latest_measurements = ts_map[latest_ts]
    peak_opt_name, peak_opt_watts = max(latest_measurements, key=lambda x: x[1])

    return peak_opt_name, peak_opt_watts, latest_ts


def main():
    try:
        secrets = load_secrets()
        site_id = secrets.get("solaredge_site_id", "")
        username = secrets.get("solaredge_username", "")
        password = secrets.get("solaredge_password", "")

        if not site_id or not username or not password:
            print(json.dumps({"irradiance": 0, "status": "unconfigured", "error": "Missing credentials in secrets.yaml"}))
            sys.exit(0)

        peak_opt_name, peak_opt_watts, timestamp = fetch_optimizer_measurements(site_id, username, password)

        irradiance = round((peak_opt_watts / MODULE_RATING_WATTS) * 1000.0)
        irradiance = max(0, min(int(MAX_IRRADIANCE_CAP), irradiance))

        result = {
            "irradiance": irradiance,
            "peak_optimizer": peak_opt_name,
            "peak_power_w": round(peak_opt_watts, 2),
            "telemetry_timestamp": timestamp,
            "status": "success"
        }
        print(json.dumps(result))
    except Exception as e:
        print(json.dumps({"irradiance": 0, "status": "error", "error": str(e)}))
        sys.exit(0)


if __name__ == "__main__":
    main()
