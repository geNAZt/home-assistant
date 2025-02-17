import re
import appdaemon.plugins.hass.hassapi as hass
import datetime
import time

from tinydb import TinyDB, Query
from datetime import timedelta, datetime, timezone

ALLOWED_DIFF = 0.0
SECURITY_OFF_RATE = 0.025
WINDOW_OPEN_RATE = -0.02
TIME_SLOT_SECONDS = 30*60

# Alpha feature control
FEATURE_ON_OFF_TIME_MANIPULATION_ENABLED = True
FEATURE_ON_OFF_TIME_MANIPULATION_SECONDS = 5
FEATURE_ON_OFF_TIME_MANIPULATION_COOLDOWN = 10*60
FEATURE_ON_OFF_TIME_MANIPULATION_RATE = 0.0066       # 0.2 degree per half hour

# Battery max output
FEATURE_TRACK_PV_ENABLED = True
FEATURE_TRACK_PV_BATTERY_MAX_DISCHARGE = 21000
FEATURE_TRACK_PV_MAX_TEMP_OFFSET = 0.5
FEATURE_TRACK_PV_MIN_BATTERY = 15


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
        db = TinyDB("/config/climate_state_%s.json" % self.name.replace("heating_", ""))
        self.table = db.table('climate', cache_size=0)
        self.query = Query()

        # Generate virtual light entity
        self.virtual_entity_name = "climate.room_%s" % self.name.replace("heating_", "")
        
        temperature = 21.0
        state = "heat"
        pwm_percent = 0.2

        docs = self.table.search(self.query.entity_id == self.virtual_entity_name)
        if len(docs) > 0:
            self.db_doc_id = docs[0].doc_id
            self.log("DB view: %r" % docs[0])

            state = docs[0]["state"]
            temperature = docs[0]["temperature"]
            pwm_percent = docs[0]["pwm_percent"]
        else:
            self.db_doc_id = self.table.insert({
                'entity_id': self.virtual_entity_name, 
                'state': state, 
                'temperature': temperature, 
                'pwm_percent': pwm_percent
            })

        self.set_state(self.virtual_entity_name, state=state, attributes={
            "hvac_modes": ["heat","off"],
            "friendly_name": "Climate %s" % self.name.replace("heating_", "").replace("_", " "),
            "temperature_unit": "temp_celsius",
            "target_temp_step": 0.1,
            "min_temp": 16.0,
            "max_temp": 25.0,
            "temperature": temperature,
            "hvac_action": "heating",
            "current_temperature": 0,
            "supported_features": 1,
        })

        self.listen_event(self.onEvent, event="call_service")

        self.security_sensors = self.find_entity("temperature_%s_floor" % self.name.replace("heating_", ""))
        self.room_sensors = self.find_entity("temperature_%s(?!_floor)" % self.name.replace("heating_", ""))

        outputs = self.find_entity("switch.heating_%s" % self.name.replace("heating_", ""))
        if len(outputs) != 1:
            raise Exception("Could not find output switch")
        
        self.output = outputs[0]

        # Attach a listener to all security sensors
        for sensor in self.security_sensors:
            sens = self.get_entity(sensor)
            sens.listen_state(self.onChangeRecalc)

        # Attach a listener to all room sensors
        for sensor in self.room_sensors:
            sens = self.get_entity(sensor)
            sens.listen_state(self.onChangeRecalc)

        sens = self.find_entity("%s_current" % self.name.replace("heating_", ""))

        self.phase = ""
        self.current = float(0)

        if len(sens) > 0:
            self.current_entity = sens[0]
            self.phase = self.current_entity.replace("_%s_current" % self.name.replace("heating_", ""), "").replace("sensor.current_", "")
            self.log("Will monitor phase %s for current control" % self.phase) # current_l1_kueche_current

            ent = self.get_entity(sens[0])
            ent.listen_state(self.onCurrentChange)

        # Ensure that the heater is off
        self.turn_off(self.output)
        self._heating_started = 0.0
        self._heating_halted_until = 0.0

        # Calc on and off time        
        self._on_time = TIME_SLOT_SECONDS * pwm_percent
        self._off_time = TIME_SLOT_SECONDS - self._on_time
        self._manipulation_time = 0

        # Ensure that we run at least once a minute
        self.run_every(self.recalc, "now", 10)

        # PV tracking
        if FEATURE_TRACK_PV_ENABLED:
            self.pv_battery_entity = self.find_entity("solaredge_speicherniveau")[0]


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
        
        if data["service"] == "set_temperature":
            temp = round(float(data["service_data"]["temperature"]),1)
            
            self.set_state(self.virtual_entity_name, attributes={"temperature": temp})
            self.table.update({'temperature': temp}, doc_ids=[self.db_doc_id])
        
        if data["service"] == "set_hvac_mode":
            if data["service_data"]["hvac_mode"] == "off":
                self.set_state(self.virtual_entity_name, state="off")
                self.table.update({'state': "off"}, doc_ids=[self.db_doc_id])
            else:
                self.set_state(self.virtual_entity_name, state="idle")
                self.table.update({'state': "idle"}, doc_ids=[self.db_doc_id])

    def find_entity(self, search):
        states = self.get_state()
        found = []
        for entity in states.keys():
            r = re.search(search, entity)
            if r is not None:
                found.append(entity)

        return found

    def pwmSet(self, on, off):
        now = time.time()
        self._on_time = on
        self._off_time = off
        pwm_percent = (self._on_time / TIME_SLOT_SECONDS)
        self.table.update({"pwm_percent": pwm_percent}, doc_ids=[self.db_doc_id])
        self.set_state(self.virtual_entity_name, attributes={"pwm_percent": pwm_percent})
        self._manipulation_time = now + FEATURE_ON_OFF_TIME_MANIPULATION_COOLDOWN

    def manipulateUp(self, log):
        now = time.time()
        if now >= self._manipulation_time:
            if self._on_time < TIME_SLOT_SECONDS:
                self.pwmSet(self._on_time + FEATURE_ON_OFF_TIME_MANIPULATION_SECONDS, self._off_time - FEATURE_ON_OFF_TIME_MANIPULATION_SECONDS)
                self.log("PWM goes up, %s" % log)

    def manipulateDown(self, log):
        now = time.time()
        if now >= self._manipulation_time:
            if self._on_time > FEATURE_ON_OFF_TIME_MANIPULATION_SECONDS:
                self.pwmSet(self._on_time - FEATURE_ON_OFF_TIME_MANIPULATION_SECONDS, self._off_time + FEATURE_ON_OFF_TIME_MANIPULATION_SECONDS)
                self.log("PWM goes down, %s" % log)

    def onChangeRecalc(self, entity, attribute, old, new, kwargs):
        self.recalc(kwargs=None)

    def onCurrentChange(self, entity, attribute, old, new, kwargs):
        nf = float(new)
        if nf > self.current:
            self.current = nf

    def target_temp(self):
        return float(self.get_state(self.virtual_entity_name, attribute="temperature", default=0))

    def is_heating(self):
        return self.get_state(self.output) == "on"

    # Check if at least one of the security sensors has a temp high enough
    def is_security_shutdown(self):
        for sensor in self.security_sensors:
            state = self.get_state(sensor, default=0)
            self.set_state(self.virtual_entity_name, attributes={"sec_%s" % sensor: state})
            if float(state) > 29:
                return True
            
        return False
    
    def room_temperature_rate(self):
        rate = float(0)
        now = datetime.now()
        for sensor in self.room_sensors:
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

        return float(rate / len(self.room_sensors))
    
    def security_temperature_rate(self):
        rate = float(0)
        now = datetime.now()
        for sensor in self.security_sensors:
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
        for sensor in self.room_sensors:
            cur_temp = float(self.get_state(sensor))
            self.set_state(self.virtual_entity_name, attributes={"room_%s" % sensor: cur_temp})
            temperature += cur_temp

        return float(temperature / len(self.room_sensors))

    def turn_heat_on(self, log):
        self.log(log)
        self.turn_on(self.output)

        now_seconds = time.time()
        self._heating_started = now_seconds
        self.set_heating()

    def set_heating(self):
        self.table.update({'state': 'heat'}, doc_ids=[self.db_doc_id])
        self.set_state(self.virtual_entity_name, state='heat')

    def turn_heat_off(self, log):
        self.log(log)
        self.turn_off(self.output)
        self.set_idle()

    def set_idle(self):
        self.table.update({'state': 'idle'}, doc_ids=[self.db_doc_id])
        self.set_state(self.virtual_entity_name, state='idle')

    def recalc(self, kwargs):
        heating = self.is_heating()

        state = self.get_state(self.virtual_entity_name)
        if state == "off":
            if heating:
                self.turn_heat_off("Climate control is off")
            return

        room_temp = self.room_temperature()
        self.set_state(self.virtual_entity_name, attributes={"current_temperature": room_temp})

        now_seconds = time.time()

        # Check for heating length
        if heating and now_seconds - self._heating_started > self._on_time:
            self._heating_halted_until = now_seconds + self._off_time
            self._heating_started = 0.0
            self.log("Setting heating pause until %r" % self._heating_halted_until)

        # Check if we are paused
        if self._heating_halted_until > now_seconds:
            if heating:
                self.turn_heat_off("Turning off due to cooldown")
            else:
                self.set_idle()
            
            return

        self._heating_halted_until = 0.0

        # Check for security shutdown
        if self.is_security_shutdown():
            if FEATURE_ON_OFF_TIME_MANIPULATION_ENABLED:
                self.manipulateDown("security cut")
            if heating:
                self.turn_heat_off("Turning off heat due to security")
            else:
                self.set_idle()

            return

        # Check for open window (heat leaking)
        room_temp_rate = self.room_temperature_rate()
        self.set_state(self.virtual_entity_name, attributes={"room_temp_rate": room_temp_rate})

        if room_temp_rate < WINDOW_OPEN_RATE:
            if heating:
                self.turn_heat_off("Room has open window. Not heating...")
            else:
                self.set_idle()

            return

        # Check if diff top to bottom is too strong (heat transfer)
        if self.security_temperature_rate() > SECURITY_OFF_RATE:
            if heating:
                self.turn_heat_off("Wanted to heat but diff between ceiling and floor temp is too high")
            else:
                self.set_idle()

            return
        
        target = self.target_temp()
        if room_temp > target and heating and FEATURE_ON_OFF_TIME_MANIPULATION_ENABLED:
            self.manipulateDown("overshoot on room temp")

        # Have we reached target temp?
        if room_temp >= self.target_temp():       # We reached target temp
            if heating:
                self.turn_heat_off("Reached target temp")
            else:
                self.set_idle()

            return

        # Are we allowed to heat (current control)
        if len(self.phase) > 0:
            current_used = float(0)
            ents = self.find_entity("sensor.current_%s" % self.phase)
            for ent in ents:
                if ent != self.current_entity:
                    c = self.get_state(ent)
                    current_used += float(c)
            
            if current_used + self.current > 15500:
                if heating:
                    self.turn_heat_off("Can't heat, would trigger breaker")

                return
        
        diff_room_temp = self.target_temp() - room_temp

        # Check for PV
        max_current = 15500 * 3
        if FEATURE_TRACK_PV_ENABLED:
            battery_charge = float(self.get_state(self.pv_battery_entity))
            if battery_charge > FEATURE_TRACK_PV_MIN_BATTERY and diff_room_temp <= FEATURE_TRACK_PV_MAX_TEMP_OFFSET:
                max_current = FEATURE_TRACK_PV_BATTERY_MAX_DISCHARGE

        current_used = float(0)
        ents = self.find_entity("sensor.current_l[1-3]_")
        for ent in ents:
            if ent != self.current_entity:
                c = self.get_state(ent)
                current_used += float(c)
        
        if current_used + self.current > max_current:
            if heating:
                self.turn_heat_off("Can't heat, would exceed max usage")
            else:
                self.log("will not heat for usage saving")
            return
        
        self.log("Current used %r mA" % (current_used + self.current))

        # Do we need to start heating?
        if diff_room_temp > ALLOWED_DIFF:
            # We are now at target temp, reduce PWM one step if possible
            if FEATURE_ON_OFF_TIME_MANIPULATION_ENABLED:
                if heating is False:
                    if room_temp_rate > FEATURE_ON_OFF_TIME_MANIPULATION_RATE:
                        self.manipulateDown("temperature rises too fast")
                    
                    if room_temp_rate < FEATURE_ON_OFF_TIME_MANIPULATION_RATE:
                        self.manipulateUp("temperature rises too slow")

            if heating is False:
                self.turn_heat_on("Starting to heat")
            else:
                self.set_heating()
                
            return

 