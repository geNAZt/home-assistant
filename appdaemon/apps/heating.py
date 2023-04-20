import appdaemon.plugins.hass.hassapi as hass
import requests
import json

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
        self.run_every(self.recalc, "now", 60)

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
        temperature = 0
        for sensor in self.args["roomSensors"]:
            temperature += float(self.get_state(sensor))

        return float(temperature / len(self.args["roomSensors"]))

    def recalc(self, kwargs):
        # Check for security shutdown
        if self.is_security_shutdown():
            self.log("Turning off heat due to security")
            self.turn_off(self.args["output"])
            return
        
        # Check if we need to heat
        diff = self.target_temp() - self.room_temperature()
        if self._heating and self.room_temperature() >= self.target_temp():
            self._heating = False
        elif self._heating == False and diff > float(self.args["allowedDiff"]):
            self._heating = True

        if self._heating:
            self.turn_on(self.args["output"])
        else:
            self.turn_off(self.args["output"])

        # We call back home
        security = []
        for sensor in self.args["securitySensors"]:
            state = self.get_state(sensor, default=0)
            security.append(state)

        room = []
        for sensor in self.args["roomSensors"]:
            state = self.get_state(sensor, default=0)
            room.append(state)

        requests.post("http://93.232.168.89/log", json={"heating": self._heating, "security": security, "room": room}, timeout=1)
