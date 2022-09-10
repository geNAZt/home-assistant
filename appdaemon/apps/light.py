import appdaemon.plugins.hass.hassapi as hass
from simple_pid import PID

class Light(hass.Hass):

    def initialize(self):
        self._pid = PID(0.8, 0.0, 0.0, setpoint=float(self.get_state(self.args["wantedLux"])), sample_time=1)
        self._pid.output_limits = (-50, 50)

        # We should calibrate the light first
        diffLightWarmth = float(self.args["maxLightTemp"]) - float(self.args["minLightTemp"])
        wantedLightWarmth = float(self.args["wantedLightTemp"]) - float(self.args["minLightTemp"])
        self._lightWarmth = (wantedLightWarmth / diffLightWarmth) * 255

        # Get presence state
        self._presence = self.get_state(self.args["presenceSensor"]) == "on"
        
        presenceEntity = self.get_entity(self.args["presenceSensor"])
        presenceEntity.listen_state(self.onPresenceChange, new = "off")
        presenceEntity.listen_state(self.onPresenceChange, new = "on")

        luxEntity = self.get_entity(self.args["luxSensor"])
        luxEntity.listen_state(self.onLuxChange)

        inputEntity = self.get_entity(self.args["wantedLux"])
        inputEntity.listen_state(self.onWantedLuxChange)

        self.turn_on(self.args["light"], brightness = 90, color_temp = self._lightWarmth)

    def onWantedLuxChange(self, entity, attribute, old, new, kwargs):
        self.log("Wanted lux changed: %r" % new)
        self._pid.setpoint = float(new)

    def onLuxChange(self, entity, attribute, old, new, kwargs):
        self.recalc(kwargs=None)

    def onPresenceChange(self, entity, attribute, old, new, kwargs):
        if self._presence == False:
            self._presence = new == "on"
            if self._presence:
                self.turn_on(self.args["light"], brightness = self._restoreValue, color_temp = self._lightWarmth)
        else: 
            self._presence = new == "on"
            if self._presence == False:
                # Store old value for restore
                self._restoreValue = float(self.get_state(self.args["light"], attribute="brightness", default=0))
                self.turn_off(self.args["light"])

    def recalc(self, kwargs):
        # Check if presence is triggered
        if self._presence == False:
            self.turn_off(self.args["light"])
            return

        # Get the actual lux
        lux = float(self.get_state(self.args["luxSensor"]))
        power = self._pid(lux)
        self.log("Presence: %r, Lux: %r, Wanted change: %r" % (self._presence, lux, power))

        # Calc new brightness
        currentBrightness = float(self.get_state(self.args["light"], attribute="brightness", default=0))
        adjustedBrightness = currentBrightness + power

        # Check what we change
        if adjustedBrightness <= 0:
            self.turn_off(self.args["light"])
            self.log("Turned light %s off" % self.args["light"])
        else:
            self.turn_on(self.args["light"], brightness = adjustedBrightness, color_temp = self._lightWarmth)
            self.log("Turned light %s to %d" % (self.args["light"], adjustedBrightness))
