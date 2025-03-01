Iimport threading
import appdaemon.plugins.hass.hassapi as hass
import adbase as ad

from datetime import datetime
from dataclasses import dataclass

@dataclass
class EnergyConsumer:
    group: str
    name: str
    phase: str
    current: float

    turn_on: callable
    turn_off: callable

    def update_current(self, current):
        if current > self.current:
            self.current = current

class EnergyManager(hass.Hass):

    _phase_control: dict

    # State control
    _state_callbacks: dict
    _state_values: dict

    # 

    _turned_on: list

    def initialize(self):
        # Init state system
        self._state_callbacks = {}
        self._state_values = {}

        # Ensure that solaredge is configured correctly
        self.ensure_state("select.pv_storage_ac_charge_policy", "Always Allowed")
        self.ensure_state("select.pv_storage_control_mode", "Remote Control")
        self.ensure_state("select.pv_storage_remote_command_mode", "Maximize self consumption")

        # Phase control
        self._phase_control = {}

        # 
        self._turned_on = []

        self.run_every(self.run_every_c, "now", 5*60)

    def register_consumer(self, group, name, phase, current, turn_on, turn_off):
        return EnergyConsumer(group, name, phase, current, turn_on, turn_off)

    def ensure_state(self, entity_id, state):
        self._state_values[entity_id] = state
        self._set_state(entity_id, state)
        if entity_id not in self._state_callbacks:
            self._state_callbacks[entity_id] = self.listen_state(self._ensure_state_callback, entity_id)

    def _ensure_state_callback(self, entity, attribute, old, new, cb_args):
        value = self._state_values[entity]
        if new != value:
            self._set_state(entity, value)

    def _set_state(self, entity, value):
        if entity.startswith("select."):
            self.select_option(entity, value)
        else:
            self.set_state(entity, state=value)

    @ad.global_lock
    def turn_on(self, ec: EnergyConsumer):
        # Check if already turned on
        if ec in self._turned_on:
            return

        self.log("  > Checking for turn on: %r, %r, %r, %r" % (ec.group, ec.name, ec.phase, ec.current))

        # Check for phase control
        if len(ec.phase) > 0:
            if not self._check_phase(ec):
                return

        # Check for additional max consumption
        if self._allowed_to_consume(ec):
            if len(ec.phase) > 0:
                self._add_phase(ec)

            ec.turn_on()
            self._turned_on.append(ec)

    @ad.global_lock
    def turn_off(self, ec: EnergyConsumer):
        ec.turn_off()

        # Check if already turned on
        if ec not in self._turned_on:
            return
        
        if len(ec.phase) > 0:
            self._remove_phase(ec)

        self._turned_on.remove(ec)

    def _add_phase(self, ec: EnergyConsumer):
        self.log("    > Checking phase %s for %s/%s wanting %d mA" % (ec.phase, ec.group, ec.name, ec.current))

        # We want to check if the usage would trip breakers
        if ec.group in self._phase_control:
            # Check if the phase is known
            phases = self._phase_control[ec.group]
            if ec.phase in phases:
                entities = phases[ec.phase]
                entities[ec.name] = ec.current
            else:
                phases[ec.phase] = {ec.name: ec.current}
        else:
            self._phase_control[ec.group] = {ec.phase: {ec.name: ec.current}}

        return True

    def _check_phase(self, ec: EnergyConsumer):
        # We want to check if the usage would trip breakers
        if ec.group in self._phase_control:
            # Check if the phase is known
            phases = self._phase_control[ec.group]
            if ec.phase in phases:
                entities = phases[ec.phase]
                v = float(0)
                for skey, value in entities.items(): 
                    if skey != ec.name:
                        v += value

                if v + ec.current > 15500:
                    self.log("    > %s wanted to use phase %s in group %s but not enough capacity" % (ec.name, ec.phase, ec.group))
                    return False

        return True
    
    def _remove_phase(self, ec: EnergyConsumer):
        with self._lock:
            phases = self._phase_control[ec.group]
            entities = phases[ec.phase]
            del entities[ec.name]

    def _allowed_to_consume(self, ec: EnergyConsumer):
        max_current = 15500 * 3
        new_current = float(0)

        # Check for battery
        battery_charge = float(self.get_state("sensor.pv_battery1_state_of_charge"))
        if battery_charge > 10:
            self.log("    > Battery charge over 10% - adding 5000 Wh")
            new_current += 21000
        
        # Check for additional PV input
        pv_production = float(self.get_state("sensor.solar_panel_to_house_w"))
        if pv_production > 100:
            pv_current = pv_production / float(230) # Rough estimate since we don't have a voltage tracker on PV
            self.log("    > PV detected - adding %d Wh" % pv_production)
            new_current += pv_current * 1000
                
        if new_current > 0:
            max_current = new_current

        current_used = float(0)
        for ent in self._turned_on:
            if ent.group != ec.group or ent.name != ec.name:
                current_used += ent.current
        
        self.log("    > Current used %d, wanting to add %d. Checking against %d" % (current_used, ec.current, max_current))

        if current_used + ec.current > max_current:
            return False
        
        return True

    def run_every_c(self, c):
        self.update()

    def update(self):
        now = datetime.now()

        # Control AC charging
        # 
        # Concept here is that we want to skip pricy hours in the morning by precharging our battery with the kWh needed.
        # When looking intoo tibber pricing data the sweetsspot is around 3a.m for this. We need to charge until we hit PV operation.
        # For this we need to estimate how much energy we need per hour and when sunrise is
        if now.hour <= 4:
            battery_charge = float(self.get_state("sensor.pv_battery1_state_of_charge"))
            if battery_charge < 60:
                self.ensure_state("select.pv_storage_remote_command_mode", "Charge from PV and AC")
            else:
                self.ensure_state("select.pv_storage_remote_command_mode", "Maximize self consumption")
        else:
            self.ensure_state("select.pv_storage_remote_command_mode", "Maximize self consumption")
