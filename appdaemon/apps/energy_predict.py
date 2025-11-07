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
        for entry in history:
            if entry["last_changed"] - first["last_changed"] > timedelta(minutes=15):
                first = entry
                new_history.append(first)
            
        # Convert the new history to a string
        new_history_str = "Data given in 15 minute intervals. Starting at %s\n" % (new_history[0].last_changed)
        for entry in new_history:
            new_history_str += "%s," % (entry.state)
        return new_history_str

    def run_every_c(self, kwargs):
        self.log("=== Energy Predict Run Every C Started (Thread: %s) ===" % threading.current_thread().name)

        # Get the current time
        now = self.get_now()

        his = self.get_history(entity_id = "sensor.solar_house_consumption_daily", start_time = now - timedelta(days=1), end_time = now)
        daily_history = self.compress_history(his[0])
        self.log("Daily history: %s" % daily_history)

        