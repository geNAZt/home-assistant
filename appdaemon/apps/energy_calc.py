import appdaemon.plugins.hass.hassapi as hass

from datetime import timedelta, datetime, timezone

class EnergyCalc(hass.Hass):

    def initialize(self):
        self.update()
        self.run_daily(self.run_daily_c, "15:30:00")

    def run_daily_c(self, **kwargs):
        self.update()

    def update(self):
        present = datetime.now()
        future = datetime(present.year, present.month, present.day, 12) + timedelta(1)
        
        self.log("Getting updated energy prices until %s" % future.isoformat())
        resp = self.call_service("tibber/get_prices", end=future.isoformat(), return_result=True)
        self.log("Response %r" % resp)