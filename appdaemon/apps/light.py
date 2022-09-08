import appdaemon.plugins.hass.hassapi as hass

class Light(hass.Hass):

    def initialize(self):
        # We should calibrate the light first
        diffLightWarmth = float(self.args["maxLightTemp"]) - float(self.args["minLightTemp"])
        wantedLightWarmth = float(self.args["wantedLightTemp"]) - float(self.args["minLightTemp"])
        self._lightWarmth = (wantedLightWarmth / diffLightWarmth) * 255

        self._hasRun = False

        # Get presence state
        self._presence = self.get_state(self.args["presenceSensor"]) == "on"
        
        presenceEntity = self.get_entity(self.args["presenceSensor"])
        presenceEntity.listen_state(self.onPresenceChange, new = "off", duration = 300)
        presenceEntity.listen_state(self.onPresenceChange, new = "on")

        luxEntity = self.get_entity(self.args["luxSensor"])
        luxEntity.listen_state(self.onLuxChange)

        self.turn_off(self.args["light"])
        self.run_in(self.start_calibration, 10)

    def onLuxChange(self, entity, attribute, old, new, kwargs):
        self._hasRun = False

    def onPresenceChange(self, entity, attribute, old, new, kwargs):
        if self._presence == False:
            self._presence = new == "on"
            if self._presence:
                self._hasRun = False # A movement should always trigger a recalc
                self.recalc(kwargs=None)
        else: 
            self._presence = new == "on"
            if self._presence == False:
                self._hasRun = False # A movement should always trigger a recalc
                self.recalc(kwargs=None)

    def start_calibration(self, kwargs):
        self._beforeCalibrationLux = float(self.get_state(self.args["luxSensor"]))
        self.turn_on(self.args["light"], brightness = 255 / 2, color_temp = self._lightWarmth)
        self.run_in(self.calibration, 10)

    def calibration(self, kwargs):
        self._maxLux = (float(self.get_state(self.args["luxSensor"])) - self._beforeCalibrationLux) * 2
        self.log("Detected %d lux" % self._maxLux)

        self.turn_off(self.args["light"])
        self.recalc(kwargs=None)
        self.run_every(self.recalc, "now", 60)

    def recalc(self, kwargs):
        # Check if max lux is present (aka calibration is done)
        if hasattr(self, "_maxLux") == False:
            return

        # Ensure that we only run once per lux update
        if self._hasRun:
            return

        self._hasRun = True

        # Get the actual lux
        lux = float(self.get_state(self.args["luxSensor"]))
        neededLux = float(self.args["wantedLux"]) - lux

        self.log("Presence: %r, Lux: %r, Wanted change: %r" % (self._presence, lux, neededLux))

        # Check if presence is triggered
        if self._presence == False:
            self.turn_off(self.args["light"])
            return

        if neededLux > 1:
            currentBrightness = float(self.get_state(self.args["light"], attribute="brightness", default=0))
            adjustedBrightness = currentBrightness + ((abs(neededLux) / self._maxLux) * 255)

            self.turn_on(self.args["light"], brightness = adjustedBrightness, color_temp = self._lightWarmth)
            self.log("Turned light %s to %d" % (self.args["light"], adjustedBrightness))
        elif neededLux < -1:
            currentBrightness = float(self.get_state(self.args["light"], attribute="brightness", default=0))
            adjustedBrightness = currentBrightness - ((abs(neededLux) / self._maxLux) * 255)
            if adjustedBrightness <= 0:
                self.turn_off(self.args["light"])
                self.log("Turned light %s off" % self.args["light"])
            else:
                self.turn_on(self.args["light"], brightness = adjustedBrightness, color_temp = self._lightWarmth)
                self.log("Turned light %s to %d" % (self.args["light"], adjustedBrightness))

