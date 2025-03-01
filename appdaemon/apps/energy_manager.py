import appdaemon.plugins.hass.hassapi as hass

from datetime import datetime

class EnergyManager(hass.Hass):

    _phase_control = dict

    # State control
    _state_callbacks = dict
    _state_values = dict

    def initialize(self):
        # Init state system
        self._state_callbacks = {}
        self._state_values = {}

        # Ensure that solaredge is configured correctly
        self.ensure_state("select.pv_storage_ac_charge_policy", "Always Allowed")
        self.ensure_state("select.pv_storage_control_mode", "Remote Control")

        self.run_every(self.run_every_c, "now", 5*60)

    def ensure_state(self, entity_id, state):
        self._state_values[entity_id] = state
        self.set_state(entity_id, state=state)
        if entity_id not in self._state_callbacks:
            self._state_callbacks[entity_id] = self.listen_state(self._ensure_state_callback, entity_id)

    def _ensure_state_callback(self, entity, attribute, old, new, cb_args):
        value = self._state_values[entity]
        if new != value:
            self.set_state(entity, value)

    def run_every_c(self, c):
        self.update()

    def update(self):
        now = datetime.now()

        # Control AC charging
        # 
        # Concept here is that we want to skip pricy hours in the morning by precharging our battery with the kWh needed.
        # When looking intoo tibber pricing data the sweetsspot is around 3a.m for this. We need to charge until we hit PV operation.
        # For this we need to estimate how much energy we need per hour and when sunrise is
