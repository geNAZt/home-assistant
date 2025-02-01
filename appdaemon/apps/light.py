import appdaemon.plugins.hass.hassapi as hass
import time
import re

from simple_pid import PID

class Light(hass.Hass):

    _restoreValue: float
    _presence: bool
    _pid: PID
    _lastUpdate: int

    def initialize(self):
        # Get state for lux
        wanted_lux = float(300)
        if self.entity_exists("input_number.state_light_%s_%s" % (self.name, "wanted_lux")):
            wanted_lux = float(self.get_state("input_number.state_light_%s_%s" % (self.name, "wanted_lux")))

        # Setup the PID controller
        self._pid = PID(2.0, 0.5, 2.0, setpoint=wanted_lux)
        self._pid.output_limits = (-10, 40)

        # Generate virtual light entity
        self.virtual_entity_name = "light.room_%s" % self.name.replace("light_", "")
        if not self.entity_exists(self.virtual_entity_name):
            self.set_state(self.virtual_entity_name, state="off", attributes={
                "min_color_temp_kelvin": 2700,
                "max_color_temp_kelvin": 6700,
                "supported_color_modes": ["color_temp"],
                "friendly_name": "Licht %s" % self.name.replace("light_", ""),
                "supported_features": 0,
                "min_mireds": 149,
                "max_mireds": 370,
            })

        self.listen_event(self.onEvent, event="call_service")
    
        # Attach a listener to all presence sensors
        self.presence_sensors = self.find_entity("binary_sensor.presence_%s[_0-9]*" % self.name.replace("light_", ""))
        for sensor in self.presence_sensors:
            presenceEntity = self.get_entity(sensor)
            presenceEntity.listen_state(self.onPresenceChange, new = "off")
            presenceEntity.listen_state(self.onPresenceChange, new = "on")

        self.lux_sensors = self.find_entity("sensor.lux_%s[_0-9]*" % self.name.replace("light_", ""))
        for sensor in self.lux_sensors:
            luxEntity = self.get_entity(sensor)
            luxEntity.listen_state(self.onLuxChange)

        # Listen if input changes so we can set setpoint of pid accordingly
        inputEntity = self.get_entity("input_number.state_light_%s_%s" % (self.name, "wanted_lux"))
        inputEntity.listen_state(self.onWantedLuxChange)

        # Get lights
        self.lights = self.find_entity("light.light_[0-9]_%s" % self.name.replace("light_", ""))

        # Get presence state
        self._presence = self.is_present()
        self._restoreValue = 0    

        # Kick it off
        self._lastUpdate = 0
        self.set_light_to(90)

    def find_entity(self, search, contains_not=""):
        states = self.get_state()
        found = []
        for entity in states.keys():
            r = re.search(search, entity)
            if r is not None:
                if len(contains_not) > 0:
                    if entity.find(contains_not) == -1:
                        found.append(entity)
                else:
                    found.append(entity)

        return found
    
    def state_value(self, name, value):
        entity_id = "input_number.state_light_%s_%s" % (self.name, name)
        self.set_state(entity_id, state=value, attributes={
            "min": -500,
            "max": 500,
        })

    def onEvent(self, event_name, data, kwargs):
        found = False
        for entity in data["service_data"]["entity_id"]:
            if entity == self.virtual_entity_name:
                found = True
                break

        if not found:
            return
        
        self.log(data)

    def set_light_to(self, brightness):
        if brightness > 255:
            brightness = 255

        #self.log("Setting light level to %d" % brightness)
        for light in self.lights:
            if brightness == 0:
                self.turn_off(light)
            else:
                self.turn_on(light, brightness = brightness, color_temp_kelvin = 6700)

    def is_present(self):
        for sensor in self.presence_sensors:
            if self.get_state(sensor) == "on":
                return True
        
        return False

    def onWantedLuxChange(self, entity, attribute, old, new, kwargs):
        self.log("Wanted lux changed: %r" % new)
        self._pid.setpoint = float(new)

    def onLuxChange(self, entity, attribute, old, new, kwargs):
        now = time.monotonic()
        dt = now - self._lastUpdate if (now - self._lastUpdate) else 1e-16
        if dt > 4:
            self.recalc(kwargs=None)
            self._lastUpdate = now

    def onPresenceChange(self, entity, attribute, old, new, kwargs):
        if self._presence == False:
            self._presence = self.is_present()
            if self._presence:
                self.set_light_to(self._restoreValue)
        else: 
            self._presence = self.is_present()
            if self._presence == False:
                self.set_light_to(0)

    def recalc(self, kwargs):
        # Check if presence is triggered
        if self._presence == False:
            self.set_light_to(0)
            return

        # Get the actual lux
        lux = 0
        for luxSensor in self.lux_sensors:
            lux += float(self.get_state(luxSensor))

        lux = lux / len(self.lux_sensors)
        power = self._pid(lux)

        #self.log("Presence: %r, Lux: %r, Wanted change: %r" % (self._presence, lux, power))

        # Calc new brightness
        currentBrightness = self.get_state(self.lights[0], attribute="brightness", default=0)
        if currentBrightness == None:
            currentBrightness = 0

        adjustedBrightness = float(currentBrightness) + power

        # Check what we change
        if adjustedBrightness <= 0:
            self.set_light_to(0)
            #self.log("Turned light off")
        else:
            diff = abs(adjustedBrightness - currentBrightness)

            if diff > 1:
                self._restoreValue = adjustedBrightness
                self.set_light_to(adjustedBrightness)
