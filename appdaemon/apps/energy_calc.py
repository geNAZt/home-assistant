import sys
import appdaemon.plugins.hass.hassapi as hass

from datetime import timedelta, datetime, timezone

EXPORT_SENSOR = "sensor.solaredge_exportierte_energie"
PV_GEN_SENSOR = "sensor.solar_pv_generation"
CHARGE_BATTERY_SENSOR = "sensor.solar_battery_input"
BATTERY_CHARGE = "sensor.solaredge_speicherniveau"
USAGE_SENSOR = "sensor.solaredge_verbrauchte_energie"


class EnergyCalc(hass.Hass):

    def initialize(self):
        self.run_every(self.run_every_c, "now", 5*60)

    def run_every_c(self, c):
        self.update()

    def update(self):
        now = datetime.now()

        # Do we export?

            
        generation = self.calc_wattage(PV_GEN_SENSOR, now)
        battery_charge = self.calc_wattage(CHARGE_BATTERY_SENSOR, now)
        usage = self.calc_wattage(USAGE_SENSOR, now)

        self.log("Generation: %r, Battery Charge: %r, Usage: %r" % (generation, battery_charge, usage))

        ##left = generation - battery_charge

    def battery_is_chargeable(self):
        return 100 - float(self.get_state(BATTERY_CHARGE)) <= sys.float_info.epsilon

    def calc_wattage(self, entity, now):
        current_value = float(self.get_state(entity))
        start_time =  now - timedelta(minutes = 6)
        data = self.get_history(entity_id = entity, start_time = start_time)
        history_state = float(data[0][0]['state'])

        if current_value >= history_state:
            hourly = (current_value - history_state) * 10
            if data[0][0]['attributes']['unit_of_measurement'] == 'kWh':
                hourly *= 1000

            self.log("%r %r Wh (h %r, c %r)" % (entity, hourly, history_state, current_value))
            return hourly
        
        return 0