import threading
import appdaemon.plugins.hass.hassapi as hass

from datetime import datetime

class EnergyManager(hass.Hass):

    _lock: threading.Lock

    _phase_control: dict

    # State control
    _state_callbacks: dict
    _state_values: dict

    def initialize(self):
        self._lock = threading.Lock()

        # Init state system
        self._state_callbacks = {}
        self._state_values = {}

        # Ensure that solaredge is configured correctly
        self.ensure_state("select.pv_storage_ac_charge_policy", "Always Allowed")
        self.ensure_state("select.pv_storage_control_mode", "Remote Control")
        self.ensure_state("select.pv_storage_remote_command_mode", "Maximize self consumption")

        self.run_every(self.run_every_c, "now", 5*60)

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

    def add_phase(self, group, phase, key, wanted):
        with self._lock:
            self.log("    > Checking phase %r for %r/%r wanting %r mA" % (phase, group, key, wanted))
            # We want to check if the usage would trip breakers
            if group in self._phase_control:
                # Check if the phase is known
                phases = self._phase_control[group]
                if phase in phases:
                    entities = phases[phase]
                    v = float(0)
                    for skey, value in entities.items(): 
                        if skey != key:
                            v += value

                    if v + wanted > 15500:
                        del entities[key]

                        self.log("%r wanted to use phase %r in group %r but not enough capacity" % (key, phase, group))
                        return False
                    
                    entities[key] = wanted
                else:
                    phases[phase] = {key: wanted}
            else:
                self._phase_control[group] = {phase: {key: wanted}}

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
