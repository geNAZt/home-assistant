DOMAIN = "ortsnetz_auslastung"
CONF_API_URL = "api_url"
CONF_L1_ENTITY = "l1_entity"
CONF_L2_ENTITY = "l2_entity"
CONF_L3_ENTITY = "l3_entity"
CONF_LATITUDE = "latitude"
CONF_LONGITUDE = "longitude"
CONF_PLANT_CAPACITY_KWP = "plant_capacity_kwp"
CONF_PV_FORECAST_ENTITY = "pv_forecast_entity"
CONF_SMARTMETER_MODEL = "smartmeter_model"
CONF_GRID_FREQUENCY_ENTITY = "grid_frequency_entity"
CONF_BATTERY_CAPACITY = "battery_capacity"
BASIC_MEASUREMENT_UNITS = {"battery_soc_percent": "%", "grid_power_w": "W"}
ADDITIONAL_MEASUREMENT_UNITS = {
    "power_factor": "1",
    "apparent_power_va": "VA",
    **{f"l{phase}_a": "A" for phase in (1, 2, 3)},
    **{f"l{phase}_v_{suffix}": "V" for phase in (1, 2, 3) for suffix in ("min", "max", "avg")},
    **{f"grid_frequency_hz_{suffix}": "Hz" for suffix in ("min", "max", "avg")},
}
OPTIONAL_MEASUREMENT_UNITS = BASIC_MEASUREMENT_UNITS | ADDITIONAL_MEASUREMENT_UNITS
PLATFORMS = ["sensor"]
MIN_PHASE_VOLTAGE_V = 150.0
MAX_PHASE_VOLTAGE_V = 300.0


def status_signal(entry_id: str) -> str:
    return f"{DOMAIN}_{entry_id}_status"


def storage_recommendation_signal(entry_id: str) -> str:
    return f"{DOMAIN}_{entry_id}_storage_recommendation"
