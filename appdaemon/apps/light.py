import appdaemon.plugins.hass.hassapi as hass

class Light(hass.Hass):

    def initialize(self):
        # We should calibrate the light first
        self.turn_off(self.args["light"])
        self.run_in(self.start_calibration, 10)

    def start_calibration(self, kwargs):
        self._beforeCalibrationLux = float(self.get_state(self.args["luxSensor"]))
        self.turn_on(self.args["light"], brightness = 255 / 2)
        self.run_in(self.calibration, 10)

    def calibration(self, kwargs):
        self._maxLux = (float(self.get_state(self.args["luxSensor"])) - self._beforeCalibrationLux) * 2
        self.log("Detected %d lux" % self._maxLux)

        self.turn_off(self.args["light"])
        self.recalc(kwargs=None)
        self.run_every(self.recalc, "now", 60)

    def recalc(self, kwargs):
        # Get the actual lux
        lux = self.get_state(self.args["luxSensor"])
        neededLux = self.args["wantedLux"] - lux

        if neededLux > 10:
            neededBrightness = (neededLux / self._maxLux) * 255
            self.turn_on(self.args["light"], brightness = neededBrightness)
            self.log("Turned light %s to %d" % (self.args["light"], neededBrightness))
        elif neededLux < -10:
            currentBrightness = self.get_state(self.args["light"], attribute="brightness")
            adjustedBrightness = currentBrightness - ((abs(neededLux) / self._maxLux) * 255)
            if adjustedBrightness <= 0:
                self.turn_off(self.args["light"])
                self.log("Turned light %s off" % self.args["light"])
            else:
                self.turn_on(self.args["light"], brightness = adjustedBrightness)
                self.log("Turned light %s to %d" % (self.args["light"], adjustedBrightness))

