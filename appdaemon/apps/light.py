import appdaemon.plugins.hass.hassapi as hass
import re
import math
import time

from datetime import timedelta, datetime, timezone
from tinydb import TinyDB, Query
from simple_pid import PID


##
#
# Lights should try to illuminate a room to a minimum target lux. That target lux can be overriden by the virtual
# light entity which will be created per room. 
# 
# To properly light a room the lux of the room should be tracked and even if no person is present a simulation should 
# be made to ensure that when a light turns on the target lux of the room can be reached ASAP

FEATURE_SIMULATION_OFF_TIME = 60 # Training basis to track illumination
FEATURE_SIMULATION_ON_TIME = 60  # Training basis for on time

class Light(hass.Hass):

    _presence: bool
    _pid: PID
    _lastUpdate: int

    _simulation_init: int
    _state: int

    _lux_history = []
    _lux_history_size = 5
    _last_brightness_change = 0
    _min_brightness_change_interval = 30  # seconds

    def initialize(self):
        self._state = 0

        # Open database
        db = TinyDB("/config/light_state_%s.json" % self.name.replace("light_", ""))
        self.table = db.table('lights', cache_size=0)
        self.query = Query()

        # Generate virtual light entity
        self.virtual_entity_name = "light.room_%s" % self.name.replace("light_", "")

        brightness = 255
        color = 6700
        state = "on"

        docs = self.table.search(self.query.entity_id == self.virtual_entity_name)
        if len(docs) > 0:
            self.db_doc_id = docs[0].doc_id

            state = docs[0]["state"]
            color = docs[0]["color"]
            brightness = docs[0]["brightness"]
        else:
            self.db_doc_id = self.table.insert({
                'entity_id': self.virtual_entity_name, 
                'state': state, 
                'color': color, 
                'brightness': brightness,
                'restore': 0
            })

        self.log("We are doc id %r" % self.db_doc_id)

        r,g,b = self.convert_K_to_RGB(color)
        self.set_state(self.virtual_entity_name, state=state, attributes={
            "min_color_temp_kelvin": 2700,
            "max_color_temp_kelvin": 6700,
            "supported_color_modes": ["color_temp"],
            "friendly_name": "Licht %s" % self.name.replace("light_", "").replace("_", " "),
            "supported_features": 0,
            "brightness": brightness,
            "color_temp_kelvin": color,
            "rgb_color": [round(r),round(g),round(b)]
        })

        self.listen_event(self.onEvent, event="call_service")

        # Setup the PID controller
        self._pid = PID(0.5, 0.1, 0.5, setpoint=float(brightness / 2.55) * 3)
        self._pid.output_limits = (-5, 15)
    
        # Attach a listener to all presence sensors
        self.presence_sensors = self.find_entity("binary_sensor.presence_%s[_0-9]*" % self.name.replace("light_", ""))
        if len(self.presence_sensors) == 0:
            raise Exception("not enough presence sensors")

        for sensor in self.presence_sensors:
            presenceEntity = self.get_entity(sensor)
            presenceEntity.listen_state(self.onPresenceChange, new = "off")
            presenceEntity.listen_state(self.onPresenceChange, new = "on")

        self.lux_sensors = self.find_entity("sensor.lux_%s[_0-9]*" % self.name.replace("light_", ""))
        if len(self.lux_sensors) == 0:
            raise Exception("not enough lux sensors")
        
        for sensor in self.lux_sensors:
            luxEntity = self.get_entity(sensor)
            luxEntity.listen_state(self.onLuxChange)

        # Get lights
        self.lights = self.find_entity("light.light_[0-9]_%s" % self.name.replace("light_", ""))
        if len(self.lights) == 0:
            raise Exception("not enough lights")
        
        # 
        if self.is_feature_enabled("simulation", False):
            # We need to turn off lights, wait for off time
            self.set_light_to(0)
            self._simulation_init = 255
            self.run_in(self._simulation_off_init, FEATURE_SIMULATION_OFF_TIME)
        else:
            self._state = 1

        # Get presence state
        self._presence = self.is_present()

        # Kick it off
        self._lastUpdate = 0

    def _simulation_off_init(self, c):
        self._lux_off = self.avg_lux()
        self.log("Off lux: %d" % self._lux_off)
        self.set_light_to(self._simulation_init)
        self.run_in(self._simulation_on_init, FEATURE_SIMULATION_ON_TIME)

    def _simulation_on_init(self, c):
        lux = self.avg_lux()
        self.log("On %d lux: %d - Abs %d" % (self._simulation_init, lux, lux - self._lux_off))
        self._simulation_init -= 25.5
        if self._simulation_init > 0:
            self.set_light_to(self._simulation_init)
            self.run_in(self._simulation_on_init, FEATURE_SIMULATION_ON_TIME)
        else:
            self._state = 1

    def is_feature_enabled(self, feature, default=False):
        if "features" not in self.args:
            return default
        
        fe = self.args["features"]
        if feature not in fe:
            return default
        
        return bool(fe[feature])

    def find_entity(self, search):
        states = self.get_state()
        found = []
        for entity in states.keys():
            r = re.search(search, entity)
            if r is not None:
                found.append(entity)

        return found

    def convert_K_to_RGB(self, colour_temperature):
        """
        Converts from K to RGB, algorithm courtesy of 
        http://www.tannerhelland.com/4435/convert-temperature-rgb-algorithm-code/
        """
        #range check
        if colour_temperature < 1000: 
            colour_temperature = 1000
        elif colour_temperature > 40000:
            colour_temperature = 40000
        
        tmp_internal = colour_temperature / 100.0
        
        # red 
        if tmp_internal <= 66:
            red = 255
        else:
            tmp_red = 329.698727446 * math.pow(tmp_internal - 60, -0.1332047592)
            if tmp_red < 0:
                red = 0
            elif tmp_red > 255:
                red = 255
            else:
                red = tmp_red
        
        # green
        if tmp_internal <=66:
            tmp_green = 99.4708025861 * math.log(tmp_internal) - 161.1195681661
            if tmp_green < 0:
                green = 0
            elif tmp_green > 255:
                green = 255
            else:
                green = tmp_green
        else:
            tmp_green = 288.1221695283 * math.pow(tmp_internal - 60, -0.0755148492)
            if tmp_green < 0:
                green = 0
            elif tmp_green > 255:
                green = 255
            else:
                green = tmp_green
        
        # blue
        if tmp_internal >=66:
            blue = 255
        elif tmp_internal <= 19:
            blue = 0
        else:
            tmp_blue = 138.5177312231 * math.log(tmp_internal - 10) - 305.0447927307
            if tmp_blue < 0:
                blue = 0
            elif tmp_blue > 255:
                blue = 255
            else:
                blue = tmp_blue
        
        return red, green, blue

    def onEvent(self, event_name, data, kwargs):
        if "service_data" not in data:
            return
        
        if "entity_id" not in data["service_data"]:
            return

        # This is a nasty hack since it can happen that an array or string is given
        found = data["service_data"]["entity_id"] == self.virtual_entity_name
        if not found:
            for entity in data["service_data"]["entity_id"]:
                if entity == self.virtual_entity_name:
                    found = True
                    break

        if not found:
            return
        
        # Should we turn on?
        if data["service"] == "turn_on":
            # Take over attributes
            attr = data["service_data"].copy()
            del attr["entity_id"]

            if "brightness_pct" in attr:
                self._pid.setpoint = round(attr["brightness_pct"] * 3)
                attr["brightness"] = round(attr["brightness_pct"] * 2.55)
                del attr["brightness_pct"]

                self.table.update({'brightness': attr["brightness"]}, doc_ids=[self.db_doc_id])

            if "color_temp_kelvin" in attr:
                r,g,b = self.convert_K_to_RGB(attr["color_temp_kelvin"])
                attr["rgb_color"] = [round(r),round(g),round(b)]

                self.table.update({'color': attr["color_temp_kelvin"]}, doc_ids=[self.db_doc_id])

            self.table.update({'state': 'on'}, doc_ids=[self.db_doc_id])
            self.set_state(self.virtual_entity_name, state="on", attributes=attr)
            

        if data["service"] == "turn_off":
            self.table.update({'state': 'off'}, doc_ids=[self.db_doc_id])
            self.set_state(self.virtual_entity_name, state="off")

        self.update()

    def set_light_to(self, brightness):
        color = float(self.get_state(self.virtual_entity_name, attribute="color_temp_kelvin", default=6700))
        if brightness > 255:
            brightness = 255

        for light in self.lights:
            if brightness == 0:
                self.turn_off(light)
            else:
                self.turn_on(light, brightness = brightness, color_temp_kelvin = color)

    def is_present(self):
        for sensor in self.presence_sensors:
            if self.get_state(sensor) == "on":
                return True
        
        return False

    def onLuxChange(self, entity, attribute, old, new, kwargs):        
        now = time.monotonic()
        dt = now - self._lastUpdate if (now - self._lastUpdate) else 1e-16
        if dt > 30:
            self.update()
            self._lastUpdate = now

    def onPresenceChange(self, entity, attribute, old, new, kwargs):
        self.update()

    def avg_lux(self):
        rate = float(0)
        amount = 0

        now = datetime.now()
        for sensor in self.lux_sensors:
            current_lux = float(self.get_state(sensor))
            rate += current_lux
            amount += 1

            start_time = now - timedelta(seconds=30)
            data = self.get_history(entity_id=sensor, start_time=start_time)
            for d in data:
                for da in d:
                    if da["state"] != "unavailable" and da["state"] != "unknown":
                        rate += float(da["state"])
                        amount += 1

        # Calculate average and add to history
        avg = rate / float(amount)
        self._lux_history.append(avg)
        if len(self._lux_history) > self._lux_history_size:
            self._lux_history.pop(0)
        
        # Return smoothed average
        return sum(self._lux_history) / len(self._lux_history)

    def update(self):
        # Are we inited yet?
        if self._state != 1:
            return

        # Check if we got disabled
        active = self.get_state(self.virtual_entity_name)
        if active == "off":
            self.set_light_to(0)
            return

        # Check for presence
        if not self.is_present():
            self.set_light_to(0)
            return

        # Get the actual lux
        #avg_lux = self.avg_lux()
        #power = self._pid(avg_lux)

        # If lux diff is not big enough ignore PID output
        #diff = abs(self._pid.setpoint - avg_lux)
        #if diff <= 15:  # Increased threshold from 5 to 15
        #    return

        # Check if enough time has passed since last brightness change
        #now = time.monotonic()
        #if now - self._last_brightness_change < self._min_brightness_change_interval:
        #    return

        # Calc new brightness
        #currentBrightness = self.get_state(self.lights[0], attribute="brightness", default=0)
        #if currentBrightness is None:
        #    doc = self.table.get(doc_id=self.db_doc_id)
        #    currentBrightness = float(doc["restore"])

        #adjustedBrightness = float(currentBrightness) + power
        #if adjustedBrightness <= 0:
        #    self.set_light_to(0)
        #    return

        #diff = abs(adjustedBrightness - currentBrightness)
        #if diff > 5:  # Increased threshold from 1 to 5
        #    self._last_brightness_change = now
        #    self.table.update({'restore': adjustedBrightness}, doc_ids=[self.db_doc_id])
        #    self.set_light_to(adjustedBrightness)
        
        self.set_light_to(300)

