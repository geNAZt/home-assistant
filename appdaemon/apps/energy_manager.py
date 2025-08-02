import appdaemon.plugins.hass.hassapi as hass
import adbase as ad

from datetime import datetime 
from dataclasses import dataclass

MIN_BATTERY_CHARGE = 0.1

@dataclass
class AdditionalConsumer:
    stage: int
    usage: float

@dataclass
class VirtualEntity:
    switched: bool
    events: dict

@dataclass
class EnergyConsumer:
    group: str
    name: str
    phase: str
    current: float

    turn_on: callable
    turn_off: callable
    can_be_delayed: callable

    consume_more: callable

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
    _known: list

    _consumptions: dict

    _solar_panel_production: float
    _solar_panel_amount: float

    _exported_power: float
    _exported_power_amount: float

    # Virtual entities
    _virtual_entities: dict

    def initialize(self):
        # Init state system
        self._state_callbacks = {}
        self._state_values = {}

        self._solar_panel_production = 0
        self._solar_panel_amount = 0

        self._exported_power = 0
        self._exported_power_amount = 0


        # Ensure that solaredge is configured correctly
        self.ensure_state("select.pv_storage_ac_charge_policy", "Always Allowed")
        self.ensure_state("select.pv_storage_control_mode", "Remote Control")
        self.ensure_state("select.pv_storage_remote_command_mode", "Maximize self consumption")

        # Phase control
        self._phase_control = {}

        # 
        self._turned_on = []
        self._known = []
        self._consumptions = {}

        # Virtual entities
        self._virtual_entities = {}

        # Register virtual entities
        for key, value in self.args["virtuals"].items():
            self._virtual_entities[key] = VirtualEntity(False, value["events"])

        # Listen for state changes
        self.listen_state(self.onSolarPanelProduction, "sensor.solar_panel_production_w")
        self.listen_state(self.onExportedPower, "sensor.solar_exported_power_w")
        self.listen_state(self.onImportedPower, "sensor.solar_imported_power_w")

        self.run_every(self.run_every_c, "now", 5*60)

    def call_all_active_virtual_entities(self, event, v):
        for key, value in self._virtual_entities.items():
            if value.switched:
                self.call_virtual_entity(key, event, v)

    def call_virtual_entity(self, entity, event, value):
        self.log("Calling virtual entity %s for event %s with value %s" % (entity, event, value))

        e = self._virtual_entities[entity]
        if event in e.events:
            if entity in self._consumptions:
                consumption_entity = self._consumptions[entity]
                exec(e.events[event]["code"], {"self": self, "value": value, "entity": consumption_entity})
            else:
                exec(e.events[event]["code"], {"self": self, "value": value})

    def onExportedPower(self, entity, attribute, old, new, cb_args):
        # Check if new is a number and update to 0 if not
        try:
            v = float(new)
        except ValueError:
            v = 0

        self._exported_power += v
        self._exported_power_amount += 1

        self.call_all_active_virtual_entities("exported_power_update", v)

    def onImportedPower(self, entity, attribute, old, new, cb_args):
        # Check if new is a number and update to 0 if not
        try:
            v = float(new)
        except ValueError:
            v = 0

        self.call_all_active_virtual_entities("imported_power_update", v)

    # Callback for solar panel production, we update the internal state of the panel
    def onSolarPanelProduction(self, entity, attribute, old, new, cb_args):
        # Check if new is a number and update to 0 if not
        try:
            v = float(new)
        except ValueError:
            v = 0
            
        self._solar_panel_production += v
        self._solar_panel_amount += 1

    def register_consumer(self, group, name, phase, current, turn_on, turn_off, can_be_delayed, consume_more):
        ec = EnergyConsumer(group, name, phase, current, turn_on, turn_off, can_be_delayed, consume_more)
        self._known.append(ec)
        return ec

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
            self.call_service("select/select_option", entity_id=entity, option=value)
        else:
            self.set_state(entity, state=value)

    @ad.global_lock
    def em_turn_on(self, ec: EnergyConsumer):
        # Check if already turned on
        if ec in self._turned_on:
            return

        # Check for phase control
        if len(ec.phase) > 0:
            if not self._check_phase(ec):
                return

        # Check for additional max consumption
        if self._allowed_to_consume(ec):
            if len(ec.phase) > 0:
                self._add_phase(ec)

            self.log("  > Turning on %s/%s" % (ec.name, ec.group))
            ec.turn_on()
            self._turned_on.append(ec)

    @ad.global_lock
    def em_turn_off(self, ec: EnergyConsumer):
        ec.turn_off()

        # Check if already turned on
        if ec not in self._turned_on:
            return
        
        if len(ec.phase) > 0:
            self._remove_phase(ec)

        self._turned_on.remove(ec)

    def _add_phase(self, ec: EnergyConsumer):
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
        phases = self._phase_control[ec.group]
        entities = phases[ec.phase]
        del entities[ec.name]

    def _can_be_delayed(self, ec: EnergyConsumer):
        return ec.can_be_delayed()

    def _get_remaining_battery_capacity(self):
        battery_max_capacity = float(self.get_state("sensor.pv_battery1_size_max")) / float(1000) # Given in Wh
        # We need to remove MIN_BATTERY_CHARGE as buffer since we can't deep discharge the battery
        battery_min_capacity = battery_max_capacity * MIN_BATTERY_CHARGE
        battery_charge = float(self.get_state("sensor.pv_battery1_state_of_charge")) / float(100) # Given in full number percent
        battery_capacity_used = battery_max_capacity * battery_charge
        return (battery_max_capacity - battery_capacity_used) - battery_min_capacity
    
    def _get_current_battery_capacity(self):
        battery_max_capacity = float(self.get_state("sensor.pv_battery1_size_max")) / float(1000) # Given in Wh
        battery_min_capacity = battery_max_capacity * MIN_BATTERY_CHARGE
        battery_charge = float(self.get_state("sensor.pv_battery1_state_of_charge")) / float(100) # Given in full number percent
        battery_capacity_used = battery_max_capacity * battery_charge
        return battery_capacity_used - battery_min_capacity

    def _allowed_to_consume(self, ec: EnergyConsumer):
        max_current = 15500 * 3
        new_current = float(0)

        # Check for battery
        battery_charge = float(self.get_state("sensor.pv_battery1_state_of_charge"))
        if battery_charge > 15:
            new_current += 21000
        
        # Check for additional PV input
        pv_production = float(self.get_state("sensor.solar_panel_production_w"))
        battery_charge = float(self.get_state("sensor.solar_panel_to_battery_w"))
        pv_over_production = pv_production - battery_charge
        if pv_over_production > 100:
            pv_current = pv_over_production / float(230) # Rough estimate since we don't have a voltage tracker on PV
            new_current += pv_current * 1000

        current_used = float(0)
        for ent in self._turned_on:
            if ent.group != ec.group or ent.name != ec.name:
                current_used += ent.current

        if new_current > 0:
            # Check if PV is enough, if not can we delay?
            if current_used + ec.current > new_current:
                if self._can_be_delayed(ec):
                    max_current = new_current
        else:
            # Check if this consumption can be delayed
            if self._can_be_delayed(ec):
                tomorrow_estimate = self._estimated_production_tomorrow()
                battery_remaining_capacity = self._get_remaining_battery_capacity()
                if tomorrow_estimate >= battery_remaining_capacity:
                    max_current = 0
        
        if current_used + ec.current > max_current:
            return False
        
        return True

    def run_every_c(self, c):
        self.update()

    def _estimated_production_tomorrow(self):
        now = self.get_now()

        if now.hour < 2:
            return float(self.get_state("sensor.solcast_pv_forecast_prognose_heute"))
        else:
            return float(self.get_state("sensor.solcast_pv_forecast_prognose_morgen"))

    def _turn_on(self, entity):
        if entity.startswith("virtual."):
            ent = entity.split(".")[1]
            self.call_virtual_entity(ent, "switched", True)
            self._virtual_entities[ent].switched = True
        else:
            self.turn_on(entity)

    def _turn_off(self, entity):
        if entity.startswith("virtual."):   
            ent = entity.split(".")[1]
            self.call_virtual_entity(ent, "switched", False)
            self._virtual_entities[ent].switched = False
        else:
            self.turn_off(entity)

    def update(self):
        # Get proper solar panel production
        panel_to_house_w = self._solar_panel_production / self._solar_panel_amount

        self._solar_panel_production = 0
        self._solar_panel_amount = 0

        exported_watt = self._exported_power / self._exported_power_amount
        
        self._exported_power = 0
        self._exported_power_amount = 0

        self.log("Checking for additional consumption, exported %.2f w, produced %.2f w" % (exported_watt, panel_to_house_w))

        now = self.get_now()

        # Control AC charging
        # 
        # Concept here is that we want to skip pricy hours in the morning by precharging our battery with the kWh needed.
        # When looking intoo tibber pricing data the sweetsspot is around 3a.m for this. We need to charge until we hit PV operation.
        # For this we need to estimate how much energy we need per hour and when sunrise is
        if now.hour < 2:
            stop_charging = datetime(now.year, now.month, now.day, 2, 0, 0, 0, now.tzinfo)

            tomorrow_estimate = self._estimated_production_tomorrow()
            battery_remaining_capacity = self._get_remaining_battery_capacity()
            battery_charge_in_kwh = self._get_current_battery_capacity()

            time_sunrise = datetime.fromisoformat(self.get_state("sensor.sun_next_rising"))

            self.log("sunrise %s" % time_sunrise)
            time_till_sunrise = (time_sunrise - stop_charging).total_seconds()

            minutes, rest = divmod(time_till_sunrise, 60)

            self.log("m %d, t %d, te %.3f" % (minutes, time_till_sunrise, tomorrow_estimate))

            # We simply asssume that we consume 1000 watt per hour for now until we found a way to predict this
            needed_watt_per_minute = 1000 / 60
            needed_kwh = (minutes * needed_watt_per_minute) / 1000

            self.log("Wanting to charge %.3f kWh, having %.3f kWh in battery" % (needed_kwh, battery_charge_in_kwh))

            if tomorrow_estimate / 2 < battery_remaining_capacity:
                if battery_charge_in_kwh < needed_kwh:
                    self.ensure_state("select.pv_storage_remote_command_mode", "Charge from PV and AC")
                else:
                    self.ensure_state("select.pv_storage_remote_command_mode", "Off")
            else:
                self.ensure_state("select.pv_storage_remote_command_mode", "Maximize self consumption")
        else:
            self.ensure_state("select.pv_storage_remote_command_mode", "Maximize self consumption")

        # Check for additional consumption
        if "consumption" in self.args:
            consumptions = self.args["consumption"]
            
            for key, value in consumptions.items():
                if key in self._consumptions:
                    c = self._consumptions[key]
                    if c.usage > panel_to_house_w:
                        # We need to turn off
                        stage = value[c.stage]
                        self._turn_off(stage["switch"])
                        del self._consumptions[key]

                        self.log("Removing consumption: %s" % key)
                    else:
                        panel_to_house_w -= c.usage

            for key, value in consumptions.items():
                if key in self._consumptions:
                    c = self._consumptions[key]
                    for ik, iv in enumerate(value):
                        if iv["usage"] > c.usage:
                            # Do we have enough capacity?
                            diff = iv["usage"] - c.usage
                            if exported_watt > diff:
                                self.log("Leveling up consumption: %s, %d, %d" % (key, ik, iv["usage"]))

                                stage = value[c.stage]
                                self._turn_off(stage["switch"])

                                c.stage = ik
                                c.usage = iv["usage"]

                                self._turn_on(iv["switch"])
                                exported_watt -= iv["usage"]
                else:
                    for ik, iv in enumerate(value):
                        if exported_watt > iv["usage"]:
                            self._turn_on(iv["switch"])
                            self.log("Adding consumption: %s, %d, %d" % (key, ik, iv["usage"]))
                            self._consumptions[key] = AdditionalConsumer(ik, iv["usage"])
                            exported_watt -= iv["usage"]
                            break

        if exported_watt > 0:                
            for ec in self._known:
                ec.consume_more()    

                    
