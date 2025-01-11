import appdaemon.plugins.hass.hassapi as hass
import datetime
import time

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

    _heating_started: float
    _heating_halted_until: float

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
        self._heating_started = 0.0
        self._heating_halted_until = 0.0

        # Ensure that we run at least once a minute
        self.run_every(self.recalc, "now", 5)

    def onChangeRecalc(self, entity, attribute, old, new, kwargs):
        self.recalc(kwargs=None)

    def target_temp(self):
        return float(self.get_state(self.args["targetTemp"], default=0))

    def is_heating(self):
        return self.get_state(self.args["output"]) == "on"

    # Check if at least one of the security sensors has a temp high enough
    def is_security_shutdown(self):
        for sensor in self.args["securitySensors"]:
            state = self.get_state(sensor, default=0)
            if float(state) > 26.5:
                return True
            
        return False
    
    def room_temperature_rate(self):
        rate = float(0)
        now = datetime.now()
        for sensor in self.args["roomSensors"]:
            current_value = float(self.get_state(sensor))
            start_time =  now - timedelta(minutes = 30)
            data = self.get_history(entity_id = sensor, start_time = start_time)
            if len(data) > 0:
                if len(data[0]) > 0:
                    try:
                        state = float(data[0][0]['state'])
                        date = datetime.fromisoformat(data[0][0]['last_changed'])
                        diffTime = now.astimezone(timezone.utc) - date
                        rate_current = ((current_value - state) / float(diffTime.seconds)) * 60.0
                        rate += rate_current
                    except ValueError:
                        pass

        return float(rate / len(self.args["roomSensors"]))
    
    def security_temperature_rate(self):
        rate = float(0)
        now = datetime.now()
        for sensor in self.args["securitySensors"]:
            current_value = float(self.get_state(sensor))
            start_time =  now - timedelta(minutes = 30)
            data = self.get_history(entity_id = sensor, start_time = start_time)
            if len(data) > 0:
                if len(data[0]) > 0:
                    try:
                        state = float(data[0][0]['state'])
                        date = datetime.fromisoformat(data[0][0]['last_changed'])
                        diffTime = now.astimezone(timezone.utc) - date
                        rate_current = ((current_value - state) / float(diffTime.seconds)) * 60.0
                        if rate_current > rate:
                            rate = rate_current
                    except ValueError:
                        pass

        return rate

    def room_temperature(self):
        temperature = float(0)
        for sensor in self.args["roomSensors"]:
            temperature += float(self.get_state(sensor))

        return float(temperature / len(self.args["roomSensors"]))
    
    def security_temperature(self):
        temperature = float(0)
        for sensor in self.args["securitySensors"]:
            try:
                temp = float(self.get_state(sensor))
                if temp > temperature:
                    temperature = temp
            except TypeError:
                self.log("Could not get %r" % sensor)
                return 9999

        return temperature

    def recalc(self, kwargs):
        heating = self.is_heating()

        # Check for heating length
        now_seconds = time.time()
        if heating and now_seconds - self._heating_started > 900:
            self._heating_halted_until = now_seconds
            self._heating_started = 0.0
            self.log("Setting heating pause until %r" % self._heating_halted_until)

        # Check if we are paused
        if self._heating_halted_until > now_seconds:
            self._heating_halted_until = 0.0
            self.turn_off(self.args["output"])
            return

        # Check for security shutdown
        if heating and self.is_security_shutdown():
            self.log("Turning off heat due to security")
            self.turn_off(self.args["output"])
            return

        # Check for open window (heat leaking)
        if heating:
            room_temp_rate = self.room_temperature_rate()
            if room_temp_rate < -0.045:
                self.log("Room has open window. Not heating...")
                self.turn_off(self.args["output"])
                return

        # Check if diff top to bottom is too strong (heat transfer)
        if heating and self.security_temperature_rate() > 0.025:
            self.log("Wanted to heat but diff between ceiling and floor temp is too high")
            self.turn_off(self.args["output"])
            return

        room_temp = self.room_temperature()

        # Have we reached target temp?
        if heating and room_temp >= self.target_temp():       # We reached target temp
            self.turn_off(self.args["output"])
            return

        # Do we need to start heating?
        diff = self.target_temp() - room_temp
        if heating == False and diff > float(self.args["allowedDiff"]):
            self._heating_started = now_seconds
            self.turn_on(self.args["output"])
            return
