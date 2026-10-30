from __future__ import annotations

import math
from urllib.parse import urlsplit

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.data_entry_flow import section
from homeassistant.helpers import selector
from homeassistant.helpers import config_validation as cv

from .const import CONF_API_URL, CONF_GRID_FREQUENCY_ENTITY, CONF_L1_ENTITY, CONF_L2_ENTITY, CONF_L3_ENTITY, CONF_LATITUDE, CONF_LONGITUDE, CONF_PLANT_CAPACITY_KWP, CONF_PV_FORECAST_ENTITY, CONF_SMARTMETER_MODEL, DOMAIN
from .const import ADDITIONAL_MEASUREMENT_UNITS, CONF_BATTERY_CAPACITY, OPTIONAL_MEASUREMENT_UNITS
from .smartmeters import SMARTMETER_MODELS

PHASE_VOLTAGES_SECTION = "phase_voltages"
ADDITIONAL_MEASUREMENTS_SECTION = "additional_measurements"
PHASE_FIELDS = (CONF_L1_ENTITY, CONF_L2_ENTITY, CONF_L3_ENTITY)
PHASE_NOT_MEASURED = "not_measured"
OPTIONAL_FIELDS = (
    CONF_GRID_FREQUENCY_ENTITY, CONF_PLANT_CAPACITY_KWP, CONF_PV_FORECAST_ENTITY,
    CONF_SMARTMETER_MODEL, CONF_BATTERY_CAPACITY,
    *(f"{field}_entity" for field in OPTIONAL_MEASUREMENT_UNITS),
)


def _required_field(key: str, validator, values: dict, fallback=None):
    """Preserve saved values as form defaults without inventing empty ones."""
    if key in values or fallback is not None:
        return vol.Required(key, default=values.get(key, fallback)), validator
    return vol.Required(key), validator


def _optional_field(key: str, validator, values: dict):
    """Suggest a saved value without a default, so clearing the field removes it."""
    if values.get(key) is not None and values[key] != "":
        return vol.Optional(key, description={"suggested_value": values[key]}), validator
    return vol.Optional(key), validator


def _valid_api_url(url: str) -> bool:
    """Accept only HTTPS addresses with a host so coordinates are not sent in clear text."""
    try:
        parts = urlsplit(url)
        parts.port  # raises ValueError for an invalid port
    except ValueError:
        return False
    return parts.scheme == "https" and bool(parts.hostname) and not any(char.isspace() for char in url)


def _validate_settings(data: dict) -> dict:
    """Return form errors for settings that the schema alone cannot check."""
    data[CONF_API_URL] = data[CONF_API_URL].strip()
    if not _valid_api_url(data[CONF_API_URL]):
        return {CONF_API_URL: "invalid_api_url"}
    for key in PHASE_FIELDS:
        value = data[key].strip() or "-1"
        if value == PHASE_NOT_MEASURED:
            value = "-1"
        data[key] = value
        if value == "-1":
            continue
        try:
            data[key] = cv.entity_id(value)
        except vol.Invalid:
            return {"base": "invalid_phase_selection"}
        if not data[key].startswith("sensor."):
            return {"base": "invalid_phase_selection"}
    if all(data[key] == "-1" for key in PHASE_FIELDS):
        return {"base": "no_measured_phase"}
    capacity = data.get(CONF_BATTERY_CAPACITY)
    if capacity is not None and (not math.isfinite(capacity) or not 0 <= capacity <= 150):
        return {CONF_BATTERY_CAPACITY: "invalid_battery_capacity"}
    model = data.get(CONF_SMARTMETER_MODEL)
    if model is not None and (not isinstance(model, str) or len(model) > 120):
        return {CONF_SMARTMETER_MODEL: "invalid_smartmeter_model"}
    return {}


def _settings_schema(hass, values: dict) -> vol.Schema:
    """Build setup and options fields for the token-free measurement API."""
    entity_selector = selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))
    phase_options = set(hass.states.async_entity_ids("sensor"))
    phase_options.update(values[key] for key in PHASE_FIELDS if isinstance(values.get(key), str) and values[key].startswith("sensor."))
    phase_labels = []
    for entity_id in phase_options:
        state = hass.states.get(entity_id)
        name = state.name if state else entity_id
        label = f"{name} ({entity_id})" if name != entity_id else entity_id
        phase_labels.append({"value": entity_id, "label": label})
    phase_labels.sort(key=lambda option: option["label"].casefold())
    phase_selector = selector.SelectSelector(selector.SelectSelectorConfig(
        options=[{"value": PHASE_NOT_MEASURED, "label": "Phase not measured (-1)"}, *phase_labels],
        translation_key="phase_sensor", custom_value=True,
        mode=selector.SelectSelectorMode.DROPDOWN,
    ))
    # Translate the UI choice without changing the persisted/API exclusion marker.
    phase_values = {key: PHASE_NOT_MEASURED if values.get(key) == "-1" else values.get(key) for key in PHASE_FIELDS}
    phase_fields = dict(_optional_field(key, phase_selector, phase_values) for key in PHASE_FIELDS)
    additional_fields = dict(
        _optional_field(f"{field}_entity", entity_selector, values)
        for field in ADDITIONAL_MEASUREMENT_UNITS
    )
    fields = dict([
        _required_field(CONF_API_URL, str, values, "https://www.ortsnetz-auslastung.de/v1/measurements"),
        (vol.Required(PHASE_VOLTAGES_SECTION), section(vol.Schema(phase_fields), {"collapsed": False})),
        _optional_field(CONF_GRID_FREQUENCY_ENTITY, entity_selector, values),
        _optional_field("battery_soc_percent_entity", entity_selector, values),
        _optional_field(CONF_BATTERY_CAPACITY, selector.NumberSelector(selector.NumberSelectorConfig(
            min=0, max=150, step="any", unit_of_measurement="kWh", mode=selector.NumberSelectorMode.BOX,
        )), values),
        _optional_field("grid_power_w_entity", entity_selector, values),
        _optional_field(CONF_PLANT_CAPACITY_KWP, vol.All(vol.Coerce(float), vol.Range(min=0.1, max=1000)), values),
        _optional_field(CONF_PV_FORECAST_ENTITY, entity_selector, values),
        _optional_field(CONF_SMARTMETER_MODEL, selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=list(SMARTMETER_MODELS),
                mode=selector.SelectSelectorMode.DROPDOWN,
                custom_value=True,
                sort=True,
            )
        ), values),
        (vol.Optional(CONF_LATITUDE, default=values.get(CONF_LATITUDE, hass.config.latitude)), vol.All(vol.Coerce(float), vol.Range(min=-90, max=90))),
        (vol.Optional(CONF_LONGITUDE, default=values.get(CONF_LONGITUDE, hass.config.longitude)), vol.All(vol.Coerce(float), vol.Range(min=-180, max=180))),
        (vol.Optional(ADDITIONAL_MEASUREMENTS_SECTION), section(vol.Schema(additional_fields), {"collapsed": True})),
    ])
    return vol.Schema(fields)


def _flatten_sections(user_input: dict) -> dict:
    """Keep the stored config flat although the form groups phase inputs."""
    # Store cleared optional fields as None so they override values from the initial setup.
    data = dict.fromkeys(PHASE_FIELDS, "-1") | dict.fromkeys(OPTIONAL_FIELDS) | user_input
    data.update(data.pop(PHASE_VOLTAGES_SECTION, {}))
    data.update(data.pop(ADDITIONAL_MEASUREMENTS_SECTION, {}))
    return data


class OrtsnetzConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 3

    async def async_step_user(self, user_input=None):
        values, errors = {}, {}
        if user_input is not None:
            values = _flatten_sections(user_input)
            errors = _validate_settings(values)
            if not errors:
                return self.async_create_entry(title="Ortsnetz-Auslastung", data=values)

        return self.async_show_form(step_id="user", data_schema=_settings_schema(self.hass, values), errors=errors)

    @staticmethod
    def async_get_options_flow(config_entry):
        return OrtsnetzOptionsFlow()


class OrtsnetzOptionsFlow(config_entries.OptionsFlow):
    """Edit non-secret settings after setup."""

    async def async_step_init(self, user_input=None):
        values, errors = {**self.config_entry.data, **self.config_entry.options}, {}
        if user_input is not None:
            data = _flatten_sections(user_input)
            errors = _validate_settings(data)
            if not errors:
                return self.async_create_entry(title="", data=data)
            values.update(data)

        return self.async_show_form(
            step_id="init",
            data_schema=_settings_schema(self.hass, values),
            errors=errors,
        )
