# App which predicts the energy usage and tries to give recommendations about charging the battery or not

import appdaemon.plugins.hass.hassapi as hass
import adbase as ad
import threading
import time

from datetime import datetime, timedelta, timezone 
from dataclasses import dataclass

class EnergyPredict(hass.Hass):
    def initialize(self):
        # Setup timer
        self.run_every(self.run_every_c, "now", 15*60)

    def run_every_c(self, kwargs):
        self.log("=== Energy Predict Run Every C Started (Thread: %s) ===" % threading.current_thread().name)

        # Get the current time
        now = self.get_now()

        # Check for the next 48 hours
        #future = now + timedelta(hours=48)

        # Get the forecast for the next 48 hours

        # Calculate the last 3 days
        last_3_days = now - timedelta(days=4)
        last_3_days = last_3_days.replace(hour=0, minute=0, second=0, microsecond=0)
        
        # Get history power usage
        history = self.get_history(entity_id = "sensor.solar_house_consumption_daily", start_time = last_3_days)
        if len(history) > 0:
            for entry in history[0]:

                self.log("History entry: %s" % entry)
        else:
            self.log("No history data available")

        