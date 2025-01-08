import appdaemon.plugins.hass.hassapi as hass
import datetime

from datetime import timedelta, datetime, timezone

#
# Heating control
#
# This should control one or multiple zones of heating
#
# There are security temperature sensors and room temperature sensors
# The security sensors are in the floor to protect the floor itself from overheating (since we use in floor heating)
# The room temperature tells the general system when to stop heating completly

class Heating(hass.Hass):

    _heating: bool

    def initialize(self):
        self.log("Heating control loaded...")

        # Attach a listener to all security sensors
        for sensor in self.args["securitySensors"]:
            sens = self.get_entity(sensor)
            sens.listen_state(self.onChangeRecalc)

        # Attach a listener to all room sensors
        for sensor in self.args["roomSensors"]:
            sens = self.get_entity(sensor)
            sens.listen_state(self.onChangeRecalc)

        # Ensure that the heater is off
        self.turn_off(self.args["output"])
        self._heating = False

        # Ensure that we run at least once a minute
        self.run_every(self.recalc, "now", 5)

    def onChangeRecalc(self, entity, attribute, old, new, kwargs):
        self.recalc(kwargs=None)

    def target_temp(self):
        return float(self.get_state(self.args["targetTemp"], default=0))

    # Check if at least one of the security sensors has a temp high enough
    def is_security_shutdown(self):
        for sensor in self.args["securitySensors"]:
            state = self.get_state(sensor, default=0)
            if float(state) > 26.5:
                self.log("Security sensor %r is %r" % (sensor, state))
                return True
            
        return False
    
    def room_temperature(self):
        temperature = float(0)
        now = datetime.now()
        for sensor in self.args["roomSensors"]:
            current_value = float(self.get_state(sensor))
            temperature += current_value
            start_time =  now - timedelta(minutes = 30)
            data = self.get_history(entity_id = sensor, start_time = start_time)
            for point in data[0]:
                try:
                    state = float(point['state'])
                    date = datetime.fromisoformat(point['last_changed'])
                    diffTime = now.astimezone(timezone.utc) - date
                    rate = ((current_value - state) / float(diffTime)) * 60.0
                    self.log("> %r: %r" % (sensor, rate))
                except ValueError:
                    pass

        return float(temperature / len(self.args["roomSensors"]))
    
    def security_temperature(self):
        temperature = float(0)
        for sensor in self.args["securitySensors"]:
            temp = float(self.get_state(sensor))
            if temp > temperature:
                temperature = temp

        return temperature

    def recalc(self, kwargs):
        # Check for security shutdown
        if self.is_security_shutdown():
            self.log("Turning off heat due to security")
            self._heating = False
        else:        
            room_temp = self.room_temperature()
            sec_temp = self.security_temperature()

            # Check if we need to heat
            diff = self.target_temp() - room_temp
            if self._heating and room_temp >= self.target_temp():       # We reached target temp
                self._heating = False
            elif self._heating == False and diff > float(self.args["allowedDiff"]):   # The room is too cold we need to heat it
                self.log("Room too cold starting to heat. Diff was %r" % diff)
                self._heating = True

            diffSecurity = sec_temp - room_temp
            if self._heating and diffSecurity > 6.0 and sec_temp >= 10:
                self._heating = False
                self.log("Wanted to heat but diff between ceiling and floor temp is too high: %r" % diffSecurity)

        switch_state = self.get_state(self.args["output"])
        if self._heating and switch_state == "off":
            self.turn_on(self.args["output"])
        elif self._heating == False and switch_state == "on":
            self.turn_off(self.args["output"])
