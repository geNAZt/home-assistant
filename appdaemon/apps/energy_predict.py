# App which predicts the energy usage and tries to give recommendations about charging the battery or not

import appdaemon.plugins.hass.hassapi as hass
import adbase as ad
import threading
import time

from datetime import datetime, timedelta, timezone 

class EnergyPredict(hass.Hass):
    def initialize(self):
        # Setup timer
        self.run_every(self.run_every_c, "now", 30*60)

    def compress_history(self, history: list[dict]) -> str:
        # We iterate over the whole history and check if we have a history entry in the same 15 minute bucket
        # We then generate a new 15 minute based history which can be used for the AI model
        # We return the new history
        first = history[0]
        new_history = [first]
        unit_of_measurement = first["unit_of_measurement"]
        for entry in history:
            if entry["last_changed"] - first["last_changed"] > timedelta(minutes=15):
                first = entry
                new_history.append(first)
            
        # Convert the new history to a string
        new_history_str = ""
        for entry in new_history:
            new_history_str += "%s," % (entry["state"])
        return new_history_str, unit_of_measurement 

    def run_every_c(self, kwargs):
        self.log("=== Energy Predict Run Every C Started (Thread: %s) ===" % threading.current_thread().name)

        # Get the current time
        now = self.get_now()

        prompt = "Data given in 15 minute intervals. Starting at %s\n" % (now - timedelta(days=1))

        entities = [
            # Energy
            "sensor.solar_house_consumption_daily",
            # Heating
            "climate.room_bad"
        ]

        for entity in entities:
            his = self.get_history(entity_id = entity, start_time = now - timedelta(days=1), end_time = now)
            daily_history, unit = self.compress_history(his[0])
            prompt += "%s(%s): %s\n" % (entity, unit, daily_history)

        self.log("Prompt: %s" % prompt)
        