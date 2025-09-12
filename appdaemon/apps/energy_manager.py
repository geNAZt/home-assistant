import appdaemon.plugins.hass.hassapi as hass
import adbase as ad

from datetime import datetime, timedelta, timezone 
from dataclasses import dataclass

MIN_BATTERY_CHARGE = 0.1

@dataclass
class AdditionalConsumer:
    stage: int
    usage: float
    real_usage: float

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

    # Consumptions based on priority
    # Lower number = higher priority
    # Key is the priority number
    # Value is a dict of consumptions
    #   Key is the consumption name
    #   Value is the consumption object
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

        # Disable all consumptions to ensure clean state
        consumptions = self.args["consumption"]
        for key, value in consumptions.items():
            stages = value["stages"]
            for stage in stages:
                self._turn_off(stage["switch"])
                self.call_virtual_entity(value["switch"], "usage_change", 0)
                self.log("Disabled consumption '%s' with switch '%s'" % (key, stage["switch"]))

        self.run_every(self.run_every_c, "now", 60)

    def call_all_active_virtual_entities(self, event, v):
        for key, value in self._virtual_entities.items():
            if value.switched:
                self.call_virtual_entity(key, event, v)

    def call_virtual_entity(self, entity, event, value):
        if entity not in self._virtual_entities:
            return

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

    def energy_consumption_rate(self, tracker):
        now = datetime.now()

        current_value = float(self.get_state(tracker))
        self.log("Current value for %s: %.2f w" % (tracker, current_value))

        # Check unit
        unit = self.get_state(tracker, attribute = "unit_of_measurement")
        if unit == "W":
            return current_value
        elif unit == "kW":
            return current_value * 1000

        start_time =  now - timedelta(minutes = 10)
        data = self.get_history(entity_id = tracker, start_time = start_time)
        if len(data) > 0:
            if len(data[0]) > 0:
                try:
                    state = float(data[0][0]['state'])
                    date = data[0][0]['last_changed']

                    self.log("State value for %s: %.2f w" % (tracker, state))

                    diffTime = now.astimezone(timezone.utc) - date
                    rate_current = ((current_value - state) / float(diffTime.seconds)) * 3600.0 
                    return rate_current
                except ValueError:
                    pass

        return float(0)
    
    def update(self):
        self.log("=== Energy Manager Update Method Started ===")
        

        # Update all consumption trackers
        for priority, consumptions in self._consumptions.items():
            for key, value in consumptions.items():                
                # Get the consumption configuration
                consumption_config = self.args["consumption"][key]
                energy_consumption_rate = self.energy_consumption_rate(consumption_config["tracker"])

                self.log("Energy consumption rate for %s: %.2f w" % (key, energy_consumption_rate))

                value.real_usage = energy_consumption_rate

        # Get proper solar panel production
        panel_to_house_w = self._solar_panel_production / self._solar_panel_amount if self._solar_panel_amount > 0 else 0
        self.log("Solar panel production calculation: production=%.2f, amount=%.2f, result=%.2f w" % 
                (self._solar_panel_production, self._solar_panel_amount, panel_to_house_w))

        self._solar_panel_production = 0
        self._solar_panel_amount = 0

        exported_watt = self._exported_power / self._exported_power_amount if self._exported_power_amount > 0 else 0
        self.log("Exported power calculation: exported=%.2f, amount=%.2f, result=%.2f w" % 
                (self._exported_power, self._exported_power_amount, exported_watt))
        
        self._exported_power = 0
        self._exported_power_amount = 0

        self.log("Checking for additional consumption, exported %.2f w, produced %.2f w" % (exported_watt, panel_to_house_w))

        now = self.get_now()
        self.log("Current time: %s (hour: %d)" % (now, now.hour))

        # Control AC charging
        # 
        # Concept here is that we want to skip pricy hours in the morning by precharging our battery with the kWh needed.
        # When looking intoo tibber pricing data the sweetsspot is around 3a.m for this. We need to charge until we hit PV operation.
        # For this we need to estimate how much energy we need per hour and when sunrise is
        if now.hour < 2:
            self.log("=== AC Charging Logic (Early Morning) ===")
            stop_charging = datetime(now.year, now.month, now.day, 2, 0, 0, 0, now.tzinfo)
            self.log("Stop charging time: %s" % stop_charging)

            tomorrow_estimate = self._estimated_production_tomorrow()
            battery_remaining_capacity = self._get_remaining_battery_capacity()
            battery_charge_in_kwh = self._get_current_battery_capacity()

            self.log("Battery status: remaining=%.3f kWh, current=%.3f kWh, tomorrow_estimate=%.3f kWh" % 
                    (battery_remaining_capacity, battery_charge_in_kwh, tomorrow_estimate))

            time_sunrise = datetime.fromisoformat(self.get_state("sensor.sun_next_rising"))
            self.log("Sunrise time: %s" % time_sunrise)
            
            time_till_sunrise = (time_sunrise - stop_charging).total_seconds()
            minutes, rest = divmod(time_till_sunrise, 60)

            self.log("Time calculations: minutes=%d, total_seconds=%.2f, rest=%.2f" % (minutes, time_till_sunrise, rest))

            # We simply asssume that we consume 1000 watt per hour for now until we found a way to predict this
            needed_watt_per_minute = 1000 / 60
            needed_kwh = (minutes * needed_watt_per_minute) / 1000

            self.log("Energy needs: watt_per_minute=%.2f, needed_kwh=%.3f" % (needed_watt_per_minute, needed_kwh))
            self.log("Wanting to charge %.3f kWh, having %.3f kWh in battery" % (needed_kwh, battery_charge_in_kwh))

            if tomorrow_estimate / 2 < battery_remaining_capacity:
                self.log("Condition met: tomorrow_estimate/2 (%.3f) < battery_remaining_capacity (%.3f)" % 
                        (tomorrow_estimate / 2, battery_remaining_capacity))
                if battery_charge_in_kwh < needed_kwh:
                    self.log("Setting PV storage mode to 'Charge from PV and AC' (need %.3f, have %.3f)" % 
                            (needed_kwh, battery_charge_in_kwh))
                    self.ensure_state("select.pv_storage_remote_command_mode", "Charge from PV and AC")
                else:
                    self.log("Setting PV storage mode to 'Off' (have sufficient charge)")
                    self.ensure_state("select.pv_storage_remote_command_mode", "Off")
            else:
                self.log("Condition not met: tomorrow_estimate/2 (%.3f) >= battery_remaining_capacity (%.3f)" % 
                        (tomorrow_estimate / 2, battery_remaining_capacity))
                self.log("Setting PV storage mode to 'Maximize self consumption'")
                self.ensure_state("select.pv_storage_remote_command_mode", "Maximize self consumption")
        else:
            self.log("Setting PV storage mode to 'Maximize self consumption' (not early morning)")
            self.ensure_state("select.pv_storage_remote_command_mode", "Maximize self consumption")

        # Check for additional consumption
        if "consumption" in self.args:
            self.log("=== Additional Consumption Logic ===")
            consumptions = self.args["consumption"]
            self.log("Available consumptions: %s" % list(consumptions.keys()))

            for priority, cur_consumptions in self._consumptions.items():
                self.log("Current active consumptions for priority %d: %s" % (priority, list(cur_consumptions.keys())))
                for key, value in cur_consumptions.items():
                    self.log("Current active consumption '%s' for priority %d: %d" % (key, priority, value.real_usage))

            # If we have export power check if we can use it
            if exported_watt > 300:
                self.log("Exported power (%.2f w) > 300w threshold, checking for additional consumption opportunities" % exported_watt)
                
                for key, value in consumptions.items():
                    try:
                        self.log("Checking consumption key: %s" % key)

                        priority = value["priority"]
                        if priority not in self._consumptions:
                            self._consumptions[priority] = {}

                        # Check if higher priority consumptions are active
                        if priority > 0:
                            for higher_priority, higher_priority_consumptions in self._consumptions.items():
                                if higher_priority < priority:
                                    self.log("Checking higher priority consumptions for priority %d" % higher_priority)
                                    for higher_priority_key, higher_priority_value in higher_priority_consumptions.items():
                                        self.log("Checking higher priority consumption '%s' for priority %d, real usage: %.2f" % (higher_priority_key, higher_priority, higher_priority_value.real_usage))
                                        if higher_priority_value.real_usage > 50:
                                            self.log("Higher priority consumption '%s' is active, skipping" % higher_priority_key)
                                            raise Exception("Higher priority consumption found")
                                
                        if key not in self._consumptions[priority]:
                            self.log("Consumption '%s' not currently active, evaluating for activation" % key)

                            # Get the lowest usage stage
                            lowest_usage = float(99999999)
                            lowest_stage = 0
                            for ik, iv in enumerate(value["stages"]): 
                                if iv["usage"] < lowest_usage:
                                    lowest_usage = iv["usage"]
                                    lowest_stage = ik

                            self.log("Lowest usage for '%s': stage=%d, usage=%.2f" % (key, lowest_stage, lowest_usage))

                            if lowest_usage < exported_watt:
                                self.log("Condition met: lowest_usage (%.2f) > exported_watt (%.2f)" % (lowest_usage, exported_watt))
                                
                                # We need to turn on
                                stage = value["stages"][lowest_stage]
                                self.log("Activating consumption '%s' with switch '%s'" % (key, stage["switch"]))
                                self._turn_on(stage["switch"])

                                self.log("Adding consumption: %s, %d, %d" % (key, lowest_stage, lowest_usage))
                                self._consumptions[priority][key] = AdditionalConsumer(lowest_stage, lowest_usage, lowest_usage)
                                
                                self.call_virtual_entity(key, "usage_change", lowest_usage)

                                exported_watt -= lowest_usage
                                self.log("Remaining exported power after activation: %.2f w" % exported_watt)

                                return
                            else:
                                self.log("Condition not met: lowest_usage (%.2f) >= exported_watt (%.2f), skipping" % (lowest_usage, exported_watt))
                        else:
                            self.log("Consumption '%s' already active, skipping" % key)
                    except Exception as e:
                        self.log("Exception: %s" % e)
                        continue
                        
                # We have enabled all consumptions, check if we can level up to a next stage
                self.log("All available consumptions evaluated, checking for level-up opportunities")
                for key, value in consumptions.items():
                    try:
                        priority = value["priority"]
                        if priority not in self._consumptions:
                            self._consumptions[priority] = {}

                        # Check if higher priority consumptions are active
                        if priority > 0:
                            for higher_priority, higher_priority_consumptions in self._consumptions.items():
                                if higher_priority < priority:
                                    self.log("Checking higher priority consumptions for priority %d" % higher_priority)
                                    for higher_priority_key, higher_priority_value in higher_priority_consumptions.items():
                                        self.log("Checking higher priority consumption '%s' for priority %d, real usage: %.2f" % (higher_priority_key, higher_priority, higher_priority_value.real_usage))
                                        if higher_priority_value.real_usage > 50:
                                            self.log("Higher priority consumption '%s' is active, skipping" % higher_priority_key)
                                            raise Exception("Higher priority consumption found")

                        if key in self._consumptions[priority]:
                            c = self._consumptions[priority][key]
                            self.log("Checking level-up for '%s': current stage=%d, usage=%.2f" % (key, c.stage, c.usage))

                            # Find the lowest usage which is still above the current usage
                            lowest_usage = float(99999999)
                            lowest_stage = 0
                            for ik, iv in enumerate(value["stages"]):
                                if iv["usage"] > c.usage and iv["usage"] < lowest_usage:
                                    lowest_usage = iv["usage"]
                                    lowest_stage = ik

                            if lowest_usage > 0:
                                self.log("Found potential level-up for '%s': stage=%d, usage=%.2f" % (key, lowest_stage, lowest_usage))
                                # Do we have enough capacity?
                                diff = lowest_usage - c.usage
                                if exported_watt > diff:
                                    self.log("Leveling up consumption: %s, %d, %d" % (key, lowest_stage, lowest_usage))

                                    # Old stage
                                    stage = value["stages"][c.stage]

                                    # New stage
                                    new_stage = value["stages"][lowest_stage]

                                    # To we need to switch?
                                    if stage["switch"] != new_stage["switch"]:
                                        self.log("Switching from '%s' to '%s'" % (stage["switch"], new_stage["switch"]))
                                        self._turn_off(stage["switch"])
                                        self._turn_on(new_stage["switch"])
                                    else:
                                        self.log("No switch change needed, same switch: '%s'" % stage["switch"])

                                    self.call_virtual_entity(key, "usage_change", new_stage["usage"])

                                    c.stage = lowest_stage
                                    c.usage = new_stage["usage"]
                                    self.log("Updated consumption '%s': stage=%d, usage=%.2f" % (key, c.stage, c.usage))

                                    return
                                else:
                                    self.log("Insufficient exported power for level-up: need %.2f, have %.2f" % (diff, exported_watt))
                            else:
                                self.log("No level-up opportunity found for '%s'" % key)
                    except Exception as e:
                        self.log("Exception: %s" % e)
                        continue
                
                self.log("Calling consume_more for all known consumers")
                for ec in self._known:
                    self.log("Calling consume_more for consumer: %s" % ec.name)
                    ec.consume_more() 
            else:
                self.log("Exported power (%.2f w) <= 300w threshold, checking for consumption reduction")

                for key, value in consumptions.items():
                    priority = value["priority"]
                    if priority not in self._consumptions:
                        continue

                    if key in self._consumptions[priority]:
                        c = self._consumptions[priority][key]
                        self.log("Checking reduction for '%s': current stage=%d, usage=%.2f" % (key, c.stage, c.usage))
                            
                        if c.usage > panel_to_house_w:
                            self.log("Condition met: current usage (%.2f) > panel_to_house_w (%.2f)" % (c.usage, panel_to_house_w))
                            # Check if we can level down
                            # Find the heighest usage which is below the current usage
                            highest_usage = float(0)
                            highest_stage = 0
                            for ik, iv in enumerate(value["stages"]):
                                if iv["usage"] < c.usage and iv["usage"] > highest_usage:
                                    highest_usage = iv["usage"]
                                    highest_stage = ik
                            
                            self.log("Highest usage below current: stage=%d, usage=%.2f" % (highest_stage, highest_usage))
                            
                            if highest_usage > 0 and highest_usage < panel_to_house_w:
                                self.log("Leveling down consumption: %s to stage %d (%.2f w)" % (key, highest_stage, highest_usage))
                                # Old stage
                                stage = value["stages"][c.stage]

                                # New stage
                                new_stage = value["stages"][highest_stage]

                                # To we need to switch?
                                if stage["switch"] != new_stage["switch"]:
                                    self.log("Switching from '%s' to '%s'" % (stage["switch"], new_stage["switch"]))
                                    self._turn_off(stage["switch"])
                                    self._turn_on(new_stage["switch"])
                                else:
                                    self.log("No switch change needed for level-down")

                                self.call_virtual_entity(key, "usage_change", new_stage["usage"])

                                c.stage = highest_stage
                                c.usage = new_stage["usage"]
                                self.log("Updated consumption '%s': stage=%d, usage=%.2f" % (key, c.stage, c.usage))
                            else:
                                self.log("No suitable level-down found or insufficient power, turning off consumption: %s" % key)
                                # We need to turn off
                                stage = value["stages"][c.stage]
                                self._turn_off(stage["switch"])
                                del self._consumptions[key]
                                
                                self.call_virtual_entity(key, "usage_change", 0)
                                self.log("Removing consumption: %s" % key)
                                
                                return
                        else:
                            self.log("Condition not met: current usage (%.2f) <= panel_to_house_w (%.2f), keeping current state" % (c.usage, panel_to_house_w))
        else:
            self.log("No consumption configuration found in args")

        self.log("=== Energy Manager Update Method Completed ===")


                    
