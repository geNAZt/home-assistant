import appdaemon.plugins.hass.hassapi as hass

from datetime import timedelta, datetime, timezone

EXPORT_SENSOR = "sensor.solaredge_exportierte_energies"

class EnergyCalc(hass.Hass):

    def initialize(self):
        self.update()
        self.run_every(self.run_every_c, "now", 5*60)

    def run_every_c(self, **kwargs):
        self.update()

    def update(self):
        now = datetime.now()
        current_value = float(self.get_state(EXPORT_SENSOR))
        start_time =  now - timedelta(minutes = 5)
        data = self.get_history(entity_id = EXPORT_SENSOR, start_time = start_time)
        self.log(data)