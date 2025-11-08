# App which predicts the energy usage and tries to give recommendations about charging the battery or not

import appdaemon.plugins.hass.hassapi as hass
import adbase as ad
import threading
import time

from datetime import datetime, timedelta, timezone 

class EnergyPredict(hass.Hass):
    def initialize(self):
        # Setup timer
        self.run_every(self.run_every_c, "now", 30)

    def compress_history(self, history: list[dict]) -> str:
        # We iterate over the whole history and check if we have a history entry in the same 15 minute bucket
        # We then generate a new 15 minute based history which can be used for the AI model
        # We return the new history
        first = history[0]
        new_history = [first]

        unit_of_measurement = first["attributes"]["unit_of_measurement"] if "unit_of_measurement" in first["attributes"] else ""
        for entry in history:
            if entry["last_changed"] - first["last_changed"] > timedelta(minutes=15):
                first = entry
                new_history.append(first)
            
        # Convert the new history to a string
        new_history_str = ""
        new_unit_of_measurement = ""
        for entry in new_history:
            if unit_of_measurement != "":
                new_history_str += "%s:%s," % (entry["last_changed"],entry["state"])
            elif first["entity_id"].startswith("climate.") and "current_temperature" in entry["attributes"]:
                new_unit_of_measurement = "C"
                new_history_str += "%s:%s," % (entry["last_changed"],entry["attributes"]["current_temperature"])

        if new_unit_of_measurement != "":
            unit_of_measurement = new_unit_of_measurement
        return new_history_str, unit_of_measurement 

    def run_every_c(self, kwargs):
        self.log("=== Energy Predict Run Every C Started (Thread: %s) ===" % threading.current_thread().name)

        # Get the current time
        now = self.get_now()

        prompt = "Data in general is given in ISO timestamp:value format seperated by commas\n\n"

        entities = [
            # Energy
            "sensor.solar_house_consumption_daily",
            "sensor.pv_battery1_state_of_charge",
            "sensor.water_heating_pump_usage",
            "sensor.heating_usage_bad",
            "sensor.heating_usage_buero_fabian",
            "sensor.heating_usage_buero_merja",
            "sensor.heating_usage_flur",
            "sensor.heating_usage_kueche",
            "sensor.heating_usage_schlafzimmer",
            "sensor.heating_usage_speisekammer",
            "sensor.heating_usage_wohnzimmer",
            # Heating
            "climate.room_bad",
            "climate.room_buero_fabian",
            "climate.room_buero_merja",
            "climate.room_flur",
            "climate.room_kueche",
            "climate.room_schlafzimmer",
            "climate.room_speisekammer",
            "climate.room_wohnzimmer"
        ]

        for entity in entities:
            his = self.get_history(entity_id = entity, start_time = now - timedelta(days=1), end_time = now)
            daily_history, unit = self.compress_history(his[0])
            prompt += "%s(%s): %s\n" % (entity, unit, daily_history)

        forecasts = [
            "tibber.get_prices",
            "solcast_solar.query_forecast_data"
        ]

        ret = self.call_service("tibber/get_prices", service_data={
            "start": now, 
            "end": now+timedelta(days=1)
        })

        tibber_resp = ret["result"]["response"]["prices"]["Suthfeld "]
        prompt += "\n\nTibber price forecast (price per kWh in euro):\n"

        for resp_item in tibber_resp:
            prompt += "%s:%s," % (resp_item["start_time"], resp_item["price"])

        ret = self.call_service("solcast_solar/query_forecast_data", service_data={
            "start_date_time": now, 
            "end_date_time": now+timedelta(days=1)
        })

        prompt += "\n\nPV generation forecast (kWh):\n"
        pv_resp = ret["result"]["response"]["data"]
        for resp_item in pv_resp:
            prompt += "%s:%s," % (resp_item["period_start"], resp_item["pv_estimate"])

        prompt += "\n\nOther facts:\nHeatloss: 1.01 kW/h/k for the whole house\nWe take a bath in the evening, heat pump with heat during the night\n\nCan you suggest if and when we should buy power? Can you also suggest when we should heat the house? Please respond in easy JSON format."

        self.log("Prompt: \n\n\n%s" % prompt)
        