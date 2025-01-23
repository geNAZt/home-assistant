import appdaemon.plugins.hass.hassapi as hass
import datetime
import time

from datetime import timedelta, datetime, timezone

ALLOWED_DIFF = 0.1
SECURITY_OFF_RATE = 0.025
WINDOW_OPEN_RATE = -0.02
TIME_SLOT_SECONDS = 20*60

# Alpha feature control
FEATURE_ON_OFF_TIME_MANIPULATION_ENABLED = True
FEATURE_ON_OFF_TIME_MANIPULATION_SECONDS = 5
FEATURE_ON_OFF_TIME_MANIPULATION_COOLDOWN = 5*60
FEATURE_ON_OFF_TIME_MANIPULATION_RATE = 0.006       # 0.2 degree per half hour

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

    _on_time: float
    _off_time: float
    _manipulation_time: float

    def initialize(self):
        self.log("Heating control loaded...")

        self.find_entity("%s_floor_" % self.name)

        # Attach a listener to all security sensors
        for sensor in self.args["securitySensors"]:
            sens = self.get_entity(sensor)
            sens.listen_state(self.onChangeRecalc)

        # Attach a listener to all room sensors
        for sensor in self.args["roomSensors"]:
            sens = self.get_entity(sensor)
            sens.listen_state(self.onChangeRecalc)

        sens = self.get_entity(self.args["wattage"])
        sens.listen_state(self.onWattageChange)

        # Ensure that the heater is off
        self.turn_off(self.args["output"])
        self._heating_started = 0.0
        self._heating_halted_until = 0.0

        # Calc on and off time
        pwm_percent = float(self.args["onTimePWM"])
        if self.entity_exists("input_number.debug_heating_%s_%s" % (self.name, "pwm_percent")):
            pwm_percent = float(self.get_state("input_number.debug_heating_%s_%s" % (self.name, "pwm_percent")))
        
        self._on_time = TIME_SLOT_SECONDS * pwm_percent
        self._off_time = TIME_SLOT_SECONDS - self._on_time
        self._manipulation_time = 0
        self.debug_value("pwm_percent", (self._on_time / TIME_SLOT_SECONDS))

        # Ensure that we run at least once a minute
        self.run_every(self.recalc, "now", 10)

    def find_entity(self, search):
        states = self.get_state()
        for entity in states.keys():
            self.log(entity)

    def manipulateUp(self):
        now = time.time()
        if now >= self._manipulation_time:
            if self._on_time < TIME_SLOT_SECONDS:
                self._on_time = self._on_time + FEATURE_ON_OFF_TIME_MANIPULATION_SECONDS
                self._off_time = self._off_time - FEATURE_ON_OFF_TIME_MANIPULATION_SECONDS
                self.debug_value("pwm_percent", (self._on_time / TIME_SLOT_SECONDS))
                self._manipulation_time = now + FEATURE_ON_OFF_TIME_MANIPULATION_COOLDOWN

    def manipulateDown(self):
        now = time.time()
        if now >= self._manipulation_time:
            if self._on_time > FEATURE_ON_OFF_TIME_MANIPULATION_SECONDS:
                self._on_time = self._on_time - FEATURE_ON_OFF_TIME_MANIPULATION_SECONDS
                self._off_time = self._off_time + FEATURE_ON_OFF_TIME_MANIPULATION_SECONDS
                self.debug_value("pwm_percent", (self._on_time / TIME_SLOT_SECONDS))
                self._manipulation_time = now + FEATURE_ON_OFF_TIME_MANIPULATION_COOLDOWN

    def onChangeRecalc(self, entity, attribute, old, new, kwargs):
        self.recalc(kwargs=None)

    def onWattageChange(self, entity, attribute, old, new, kwargs):
        self.log("Went from %r to %r" % (old, new))

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

    def debug_value(self, name, value):
        entity_id = "input_number.debug_heating_%s_%s" % (self.name, name)
        self.set_state(entity_id, state=value, attributes={
            "min": -500,
            "max": 500,
        })

    def recalc(self, kwargs):
        heating = self.is_heating()
        now_seconds = time.time()

        # Check for heating length
        if heating and now_seconds - self._heating_started > self._on_time:
            self._heating_halted_until = now_seconds + self._off_time
            self._heating_started = 0.0
            self.log("Setting heating pause until %r" % self._heating_halted_until)

        # Check if we are paused
        if self._heating_halted_until > now_seconds:
            if heating:
                self.log("Turning off due to cooldown")
                self.turn_off(self.args["output"])
            return

        self._heating_halted_until = 0.0

        # Check for security shutdown
        if self.is_security_shutdown():
            if heating:
                self.log("Turning off heat due to security")
                self.turn_off(self.args["output"])
            return

        # Check for open window (heat leaking)
        room_temp_rate = self.room_temperature_rate()
        self.debug_value("room_temp_rate", room_temp_rate)
        if room_temp_rate < WINDOW_OPEN_RATE:
            if heating:
                self.log("Room has open window. Not heating...")
                self.turn_off(self.args["output"])
            return

        # Check if diff top to bottom is too strong (heat transfer)
        if self.security_temperature_rate() > SECURITY_OFF_RATE:
            if heating:
                self.log("Wanted to heat but diff between ceiling and floor temp is too high")
                self.turn_off(self.args["output"])
            return

        room_temp = self.room_temperature()

        # Have we reached target temp?
        if room_temp >= self.target_temp():       # We reached target temp
            if heating:
                self.log("Reached target temp")
                self.turn_off(self.args["output"])
            return

        # Do we need to start heating?
        diff = self.target_temp() - room_temp
        if room_temp < self.target_temp():
            # We are now at target temp, reduce PWM one step if possible
            if FEATURE_ON_OFF_TIME_MANIPULATION_ENABLED:
                if room_temp_rate > FEATURE_ON_OFF_TIME_MANIPULATION_RATE:
                    self.manipulateDown()
                
                if room_temp_rate < FEATURE_ON_OFF_TIME_MANIPULATION_RATE:
                    self.manipulateUp()

            if heating == False:
                self.log("Starting to heat")
                self._heating_started = now_seconds
                self.turn_on(self.args["output"])
                
            return

        # We are now at target temp, reduce PWM one step if possible
        if FEATURE_ON_OFF_TIME_MANIPULATION_ENABLED:
            self.manipulateDown()