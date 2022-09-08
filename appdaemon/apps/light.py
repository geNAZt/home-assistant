import appdaemon.plugins.hass.hassapi as hass
import time
import warnings


def _clamp(value, limits):
    lower, upper = limits
    if value is None:
        return None
    elif (upper is not None) and (value > upper):
        return upper
    elif (lower is not None) and (value < lower):
        return lower
    return value


try:
    # Get monotonic time to ensure that time deltas are always positive
    _current_time = time.monotonic
except AttributeError:
    # time.monotonic() not available (using python < 3.3), fallback to time.time()
    _current_time = time.time
    warnings.warn('time.monotonic() not available in python < 3.3, using time.time() as fallback')


class PID(object):
    """A simple PID controller."""

    def __init__(
        self,
        Kp=1.0,
        Ki=0.0,
        Kd=0.0,
        setpoint=0,
        sample_time=0.01,
        output_limits=(None, None),
        auto_mode=True,
        proportional_on_measurement=False,
        differetial_on_measurement=True,
        error_map=None,
    ):
        """
        Initialize a new PID controller.
        :param Kp: The value for the proportional gain Kp
        :param Ki: The value for the integral gain Ki
        :param Kd: The value for the derivative gain Kd
        :param setpoint: The initial setpoint that the PID will try to achieve
        :param sample_time: The time in seconds which the controller should wait before generating
            a new output value. The PID works best when it is constantly called (eg. during a
            loop), but with a sample time set so that the time difference between each update is
            (close to) constant. If set to None, the PID will compute a new output value every time
            it is called.
        :param output_limits: The initial output limits to use, given as an iterable with 2
            elements, for example: (lower, upper). The output will never go below the lower limit
            or above the upper limit. Either of the limits can also be set to None to have no limit
            in that direction. Setting output limits also avoids integral windup, since the
            integral term will never be allowed to grow outside of the limits.
        :param auto_mode: Whether the controller should be enabled (auto mode) or not (manual mode)
        :param proportional_on_measurement: Whether the proportional term should be calculated on
            the input directly rather than on the error (which is the traditional way). Using
            proportional-on-measurement avoids overshoot for some types of systems.
        :param differetial_on_measurement: Whether the differential term should be calculated on
            the input directly rather than on the error (which is the traditional way).
        :param error_map: Function to transform the error value in another constrained value.
        """
        self.Kp, self.Ki, self.Kd = Kp, Ki, Kd
        self.setpoint = setpoint
        self.sample_time = sample_time

        self._min_output, self._max_output = None, None
        self._auto_mode = auto_mode
        self.proportional_on_measurement = proportional_on_measurement
        self.differetial_on_measurement = differetial_on_measurement
        self.error_map = error_map

        self._proportional = 0
        self._integral = 0
        self._derivative = 0

        self._last_time = None
        self._last_output = None
        self._last_error = None
        self._last_input = None

        self.output_limits = output_limits
        self.reset()

    def __call__(self, input_, dt=None):
        """
        Update the PID controller.
        Call the PID controller with *input_* and calculate and return a control output if
        sample_time seconds has passed since the last update. If no new output is calculated,
        return the previous output instead (or None if no value has been calculated yet).
        :param dt: If set, uses this value for timestep instead of real time. This can be used in
            simulations when simulation time is different from real time.
        """
        if not self.auto_mode:
            return self._last_output

        now = _current_time()
        if dt is None:
            dt = now - self._last_time if (now - self._last_time) else 1e-16
        elif dt <= 0:
            raise ValueError('dt has negative value {}, must be positive'.format(dt))

        if self.sample_time is not None and dt < self.sample_time and self._last_output is not None:
            # Only update every sample_time seconds
            return self._last_output

        # Compute error terms
        error = self.setpoint - input_
        d_input = input_ - (self._last_input if (self._last_input is not None) else input_)
        d_error = error - (self._last_error if (self._last_error is not None) else error)

        # Check if must map the error
        if self.error_map is not None:
            error = self.error_map(error)

        # Compute the proportional term
        if not self.proportional_on_measurement:
            # Regular proportional-on-error, simply set the proportional term
            self._proportional = self.Kp * error
        else:
            # Add the proportional error on measurement to error_sum
            self._proportional -= self.Kp * d_input

        # Compute integral and derivative terms
        self._integral += self.Ki * error * dt
        self._integral = _clamp(self._integral, self.output_limits)  # Avoid integral windup

        if self.differetial_on_measurement:
            self._derivative = -self.Kd * d_input / dt
        else:
            self._derivative = self.Kd * d_error / dt

        # Compute final output
        output = self._proportional + self._integral + self._derivative
        output = _clamp(output, self.output_limits)

        # Keep track of state
        self._last_output = output
        self._last_input = input_
        self._last_error = error
        self._last_time = now

        return output

    def output_limits(self, limits):
        """Set the output limits."""
        if limits is None:
            self._min_output, self._max_output = None, None
            return

        min_output, max_output = limits

        if (None not in limits) and (max_output < min_output):
            raise ValueError('lower limit must be less than upper limit')

        self._min_output = min_output
        self._max_output = max_output

        self._integral = _clamp(self._integral, self.output_limits)
        self._last_output = _clamp(self._last_output, self.output_limits)

    def reset(self):
        """
        Reset the PID controller internals.
        This sets each term to 0 as well as clearing the integral, the last output and the last
        input (derivative calculation).
        """
        self._proportional = 0
        self._integral = 0
        self._derivative = 0

        self._integral = _clamp(self._integral, self.output_limits)

        self._last_time = _current_time()
        self._last_output = None
        self._last_input = None

class Light(hass.Hass):

    def initialize(self):
        self._pid = PID(1.5, 0.05, 0.2, setpoint=float(self.args["wantedLux"]), sample_time=1)
        self._pid.output_limits = (-30, 30)

        # We should calibrate the light first
        diffLightWarmth = float(self.args["maxLightTemp"]) - float(self.args["minLightTemp"])
        wantedLightWarmth = float(self.args["wantedLightTemp"]) - float(self.args["minLightTemp"])
        self._lightWarmth = (wantedLightWarmth / diffLightWarmth) * 255

        # Get presence state
        self._presence = self.get_state(self.args["presenceSensor"]) == "on"
        
        presenceEntity = self.get_entity(self.args["presenceSensor"])
        presenceEntity.listen_state(self.onPresenceChange, new = "off", duration = 300)
        presenceEntity.listen_state(self.onPresenceChange, new = "on")

        luxEntity = self.get_entity(self.args["luxSensor"])
        luxEntity.listen_state(self.onLuxChange)

        self.turn_off(self.args["light"])

    def onLuxChange(self, entity, attribute, old, new, kwargs):
        self.recalc(kwargs=None)

    def onPresenceChange(self, entity, attribute, old, new, kwargs):
        if self._presence == False:
            self._presence = new == "on"
            if self._presence:
                self.turn_on(self.args["light"], brightness = self._restoreValue, color_temp = self._lightWarmth)
                self.recalc(kwargs=None)
        else: 
            self._presence = new == "on"
            if self._presence == False:
                # Store old value for restore
                self._restoreValue = float(self.get_state(self.args["light"], attribute="brightness", default=0))
                self.recalc(kwargs=None)

    def recalc(self, kwargs):
        # Get the actual lux
        lux = float(self.get_state(self.args["luxSensor"]))
        power = self._pid(lux)
        self.log("Presence: %r, Lux: %r, Wanted change: %r" % (self._presence, lux, power))

        # Check if presence is triggered
        if self._presence == False:
            self.turn_off(self.args["light"])
            return

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
