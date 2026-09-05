#!/usr/bin/env python3
"""
SolarEdge Optimizer Irradiance Fetcher

This script logs in to the SolarEdge Monitoring portal, downloads site layout
and optimizer power data, computes peak solar irradiance (W/m²) based on a 415W
module rating at STC (1000 W/m²), and outputs JSON results for Home Assistant.

Rated Capacity per Module: 415 W
Formula: Irradiance = (Max_Optimizer_Watts / 415.0) * 1000.0
"""

import sys
import os
import json
import urllib.request
import urllib.parse
import http.cookiejar

MODULE_RATING_WATTS = 415.0
MAX_IRRADIANCE_CAP = 1200.0


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
        # Simple line/key-value parser fallback if yaml package is unavailable
        with open(secrets_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and ":" in line:
                    k, v = line.split(":", 1)
                    secrets[k.strip()] = v.strip().strip("'\"")

    return secrets


def fetch_solaredge_optimizer_power(site_id, username, password, api_key=None):
    """
    Authenticate with SolarEdge and fetch optimizer power data using standard library urllib.
    Returns peak optimizer wattage (float).
    """
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    headers = [
        ("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
        ("Accept", "application/json, text/plain, */*")
    ]
    opener.addheaders = headers

    optimizer_powers = []

    # 1. SolarEdge Portal Web Login
    login_url = "https://monitoring.solaredge.com/solaredge-apigw/api/login"
    login_data = urllib.parse.urlencode({
        "j_username": username,
        "j_password": password
    }).encode("utf-8")

    try:
        req = urllib.request.Request(login_url, data=login_data, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with opener.open(req, timeout=15) as resp:
            pass
    except Exception as e:
        # Fallback to legacy j_security_check form login
        try:
            legacy_login_url = "https://monitoring.solaredge.com/solaredge-web/j_security_check"
            req = urllib.request.Request(legacy_login_url, data=login_data, method="POST")
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
            with opener.open(req, timeout=15) as resp:
                pass
        except Exception as ex:
            sys.stderr.write(f"Web login warning: {ex}\n")

    # 2. Query Logical Layout / Optimizer Data
    layout_urls = [
        f"https://monitoring.solaredge.com/solaredge-apigw/api/sites/{site_id}/layout/logical",
        f"https://monitoring.solaredge.com/solaredge-apigw/api/sites/{site_id}/layout/energy/current",
        f"https://monitoring.solaredge.com/solaredge-web/p/site/{site_id}/layout/logical",
    ]

    for url in layout_urls:
        try:
            req = urllib.request.Request(url)
            with opener.open(req, timeout=15) as resp:
                if resp.status == 200:
                    raw = resp.read().decode("utf-8")
                    data = json.loads(raw)
                    powers = extract_powers_from_json(data)
                    if powers:
                        optimizer_powers.extend(powers)
                        break
        except Exception:
            continue

    # 3. Official API fallback if API key is provided and web portal yields no optimizers
    if not optimizer_powers and api_key:
        try:
            overview_url = f"https://monitoringapi.solaredge.com/site/{site_id}/overview?api_key={api_key}"
            req = urllib.request.Request(overview_url)
            with opener.open(req, timeout=15) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    current_power_w = data.get("overview", {}).get("currentPower", {}).get("power", 0.0)
                    if current_power_w > 0:
                        optimizer_powers.append(current_power_w)
        except Exception as e:
            sys.stderr.write(f"Official API fallback warning: {e}\n")

    if not optimizer_powers:
        return 0.0

    return max(optimizer_powers)


def extract_powers_from_json(obj):
    """Recursively traverse JSON structure to extract optimizer power measurements."""
    powers = []
    if isinstance(obj, dict):
        for key in ("power", "currentPower", "currentPowerW", "watts", "w"):
            if key in obj and isinstance(obj[key], (int, float)):
                val = float(obj[key])
                if val > 0:
                    powers.append(val)
        for v in obj.values():
            powers.extend(extract_powers_from_json(v))
    elif isinstance(obj, list):
        for item in obj:
            powers.extend(extract_powers_from_json(item))
    return powers


def main():
    try:
        secrets = load_secrets()
        site_id = secrets.get("solaredge_site_id", "")
        username = secrets.get("solaredge_username", "")
        password = secrets.get("solaredge_password", "")
        api_key = secrets.get("solaredge_api_key", None)

        if not site_id or not username:
            print(json.dumps({"irradiance": 0, "status": "unconfigured", "error": "Missing solaredge_site_id or solaredge_username in secrets.yaml"}))
            sys.exit(0)

        max_power_w = fetch_solaredge_optimizer_power(site_id, username, password, api_key)
        irradiance = round((max_power_w / MODULE_RATING_WATTS) * 1000.0)
        irradiance = max(0, min(int(MAX_IRRADIANCE_CAP), irradiance))

        result = {
            "irradiance": irradiance,
            "max_power_w": round(max_power_w, 2),
            "status": "success"
        }
        print(json.dumps(result))
    except Exception as e:
        print(json.dumps({"irradiance": 0, "status": "error", "error": str(e)}))
        sys.exit(0)


if __name__ == "__main__":
    main()
