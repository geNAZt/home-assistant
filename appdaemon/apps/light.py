import appdaemon.plugins.hass.hassapi as hass
import time
from simple_pid import PID

class Light(hass.Hass):

    _restoreValue: float
    _presence: bool
    _pid: PID
    _lastUpdate: int

    def initialize(self):
        # Setup the PID controller
        self._pid = PID(2.0, 0.5, 2.0, setpoint=float(self.get_state(self.args["wantedLux"])))
        self._pid.output_limits = (-10, 40)

        # Get presence state
        self._presence = self.is_present()
        self._restoreValue = 0    
    
        # Attach a listener to all presence sensors
        for sensor in self.args["presenceSensor"]:
            presenceEntity = self.get_entity(sensor)
            presenceEntity.listen_state(self.onPresenceChange, new = "off")
            presenceEntity.listen_state(self.onPresenceChange, new = "on")

        for sensor in self.args["luxSensor"]:
            luxEntity = self.get_entity(sensor)
            luxEntity.listen_state(self.onLuxChange)

        # Listen if input changes so we can set setpoint of pid accordingly
        inputEntity = self.get_entity(self.args["wantedLux"])
        inputEntity.listen_state(self.onWantedLuxChange)

        # Kick it off
        self._lastUpdate = 0
        self.set_light_to(90)

    def set_light_to(self, brightness):
        if brightness > 255:
            brightness = 255

        #self.log("Setting light level to %d" % brightness)
        for light in self.args["light"]:
            if brightness == 0:
                self.turn_off(light)
            else:
                self.turn_on(light, brightness = brightness, color_temp_kelvin = int(self.args["wantedLightTemp"]))

    def is_present(self):
        for sensor in self.args["presenceSensor"]:
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
        for luxSensor in self.args["luxSensor"]:
            lux += float(self.get_state(luxSensor))

        lux = lux / len(self.args["luxSensor"])
        power = self._pid(lux)

        #self.log("Presence: %r, Lux: %r, Wanted change: %r" % (self._presence, lux, power))

        # Calc new brightness
        currentBrightness = self.get_state(self.args["light"][0], attribute="brightness", default=0)
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
