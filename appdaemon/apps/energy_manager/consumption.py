"""Consumption management for the energy manager."""

from datetime import datetime, timedelta, timezone
from .models import EnergyConsumer, AdditionalConsumer


class ConsumptionManager:
    """Manages energy consumption tracking and control."""

    def __init__(self, hass_instance, battery_manager, phase_control, virtual_entity_manager, ac_charging_manager=None):
        """Initialize consumption manager.
        
        Args:
            hass_instance: The AppDaemon Hass instance
            battery_manager: BatteryManager instance
            phase_control: PhaseControl instance
            virtual_entity_manager: VirtualEntityManager instance
            ac_charging_manager: ACChargingManager instance (optional)
        """
        self.hass = hass_instance
        self.battery_manager = battery_manager
        self.phase_control = phase_control
        self.virtual_entity_manager = virtual_entity_manager
        self.ac_charging_manager = ac_charging_manager
        
        self._turned_on = []
        self._known = []
        self._consumptions = {}

    def register_consumer(self, group, name, phase, current, turn_on, turn_off, can_be_delayed, consume_more):
        """Register a new energy consumer.
        
        Args:
            group: Consumer group name
            name: Consumer name
            phase: Electrical phase identifier
            current: Current consumption in watts
            turn_on: Function to turn on the consumer
            turn_off: Function to turn off the consumer
            can_be_delayed: Function to check if consumption can be delayed
            consume_more: Function to increase consumption
            
        Returns:
            EnergyConsumer: The created consumer object
        """
        ec = EnergyConsumer(group, name, phase, current, turn_on, turn_off, can_be_delayed, consume_more)
        self._known.append(ec)
        return ec

    def turn_on_consumer(self, ec: EnergyConsumer):
        """Turn on an energy consumer.
        
        Args:
            ec: The energy consumer to turn on
        """
        try:
            # Check if already turned on
            if ec in self._turned_on:
                self.hass.log("Consumer %s/%s already turned on, skipping" % (ec.name, ec.group))
                return

            # Check for phase control
            if len(ec.phase) > 0:
                self.hass.log("Checking phase control for %s/%s (phase: %s)" % (ec.name, ec.group, ec.phase))
                if not self.phase_control.check_phase(ec):
                    self.hass.log("Phase check failed for %s/%s" % (ec.name, ec.group))
                    return

            # Check if allowed to consume
            self.hass.log("Checking if allowed to consume for %s/%s" % (ec.name, ec.group))
            if self._allowed_to_consume(ec):
                if len(ec.phase) > 0:
                    self.hass.log("Adding phase for %s/%s" % (ec.name, ec.group))
                    self.phase_control.add_phase(ec)

                self.hass.log("  > Turning on %s/%s" % (ec.name, ec.group))
                ec.turn_on()
                self._turned_on.append(ec)
                self.hass.log("Successfully turned on %s/%s" % (ec.name, ec.group))
            else:
                self.hass.log("Not allowed to consume for %s/%s" % (ec.name, ec.group))
        except Exception as ex:
            self.hass.log("ERROR in turn_on_consumer for %s/%s: %s" % (ec.name, ec.group, str(ex)))

    def turn_off_consumer(self, ec: EnergyConsumer):
        """Turn off an energy consumer.
        
        Args:
            ec: The energy consumer to turn off
        """
        try:
            ec.turn_off()

            # Check if already turned on
            if ec not in self._turned_on:
                self.hass.log("Consumer %s/%s not in turned_on list, skipping removal" % (ec.name, ec.group))
                return
            
            if len(ec.phase) > 0:
                self.hass.log("Removing phase for %s/%s" % (ec.name, ec.group))
                self.phase_control.remove_phase(ec)

            self._turned_on.remove(ec)
            self.hass.log("Successfully turned off %s/%s" % (ec.name, ec.group))
        except Exception as ex:
            self.hass.log("ERROR in turn_off_consumer for %s/%s: %s" % (ec.name, ec.group, str(ex)))

    def _can_be_delayed(self, ec: EnergyConsumer):
        """Check if a consumer can be delayed.
        
        Args:
            ec: The energy consumer to check
            
        Returns:
            bool: True if the consumer can be delayed, False otherwise
        """
        try:
            result = ec.can_be_delayed()
            self.hass.log("Consumer %s/%s can_be_delayed: %s" % (ec.name, ec.group, result))
            return result
        except Exception as ex:
            self.hass.log("ERROR in _can_be_delayed for %s/%s: %s" % (ec.name, ec.group, str(ex)))
            return False

    def _allowed_to_consume(self, ec: EnergyConsumer):
        """Check if a consumer is allowed to consume energy.
        
        Args:
            ec: The energy consumer to check
            
        Returns:
            bool: True if allowed to consume, False otherwise
        """
        try:
            max_current = 15500 * 3
            new_current = float(0)
            self.hass.log("Initial max_current: %.2f, new_current: %.2f" % (max_current, new_current))

            # Check for battery
            battery_charge = self.battery_manager.get_battery_charge_percent()
            self.hass.log("Battery charge: %.2f%%" % battery_charge)
            if battery_charge > 15:
                new_current += 21000
                self.hass.log("Added battery current: 21000, new_current now: %.2f" % new_current)
            
            # Check for additional PV input
            pv_production = float(self.hass.get_state("sensor.solar_panel_production_w"))
            battery_charge_pv = float(self.hass.get_state("sensor.solar_panel_to_battery_w"))
            pv_over_production = pv_production - battery_charge_pv
            self.hass.log("PV production: %.2f, PV to battery: %.2f, over production: %.2f" % 
                         (pv_production, battery_charge_pv, pv_over_production))
            
            if pv_over_production > 100:
                pv_current = pv_over_production / float(230)  # Rough estimate since we don't have a voltage tracker on PV
                new_current += pv_current * 1000
                self.hass.log("Added PV current: %.2f, new_current now: %.2f" % (pv_current * 1000, new_current))

            current_used = float(0)
            for ent in self._turned_on:
                if ent.group != ec.group or ent.name != ec.name:
                    current_used += ent.current
            self.hass.log("Current used by other consumers: %.2f" % current_used)

            if new_current > 0:
                self.hass.log("new_current > 0, checking if PV is enough")
                # Check if PV is enough, if not can we delay?
                if current_used + ec.current > new_current:
                    self.hass.log("current_used + ec.current (%.2f) > new_current (%.2f)" % 
                                 (current_used + ec.current, new_current))
                    if self._can_be_delayed(ec):
                        max_current = new_current
                        self.hass.log("Consumer can be delayed, setting max_current to new_current: %.2f" % max_current)
            else:
                self.hass.log("new_current <= 0, checking if consumption can be delayed")
                # Check if this consumption can be delayed
                if self._can_be_delayed(ec):
                    tomorrow_estimate = self._estimated_production_tomorrow()
                    battery_remaining_capacity = self.battery_manager.get_remaining_battery_capacity()
                    self.hass.log("Tomorrow estimate: %.2f, battery remaining: %.2f" % 
                                 (tomorrow_estimate, battery_remaining_capacity))
                    if tomorrow_estimate >= battery_remaining_capacity:
                        max_current = 0
                        self.hass.log("Setting max_current to 0 (tomorrow estimate >= battery remaining)")
            
            total_required = current_used + ec.current
            self.hass.log("Total required: %.2f, max_current: %.2f" % (total_required, max_current))
            
            if total_required > max_current:
                self.hass.log("NOT allowed to consume (total_required > max_current)")
                return False
            else:
                self.hass.log("ALLOWED to consume (total_required <= max_current)")
                return True
                
        except Exception as ex:
            self.hass.log("ERROR in _allowed_to_consume for %s/%s: %s" % (ec.name, ec.group, str(ex)))
            return False

    def energy_consumption_rate(self, tracker):
        """Calculate the energy consumption rate for a tracker.
        
        Args:
            tracker: The entity ID of the consumption tracker
            
        Returns:
            float: Consumption rate in watts per hour
        """
        try:
            now = datetime.now()

            current_value = float(self.hass.get_state(tracker))
            self.hass.log("Current value for %s: %.2f w" % (tracker, current_value))

            # Check unit
            unit = self.hass.get_state(tracker, attribute="unit_of_measurement")
            self.hass.log("Unit of measurement for %s: %s" % (tracker, unit))
            
            if unit == "W":
                self.hass.log("Returning current value directly (W): %.2f" % current_value)
                return current_value
            elif unit == "kW":
                converted_value = current_value * 1000
                self.hass.log("Converted kW to W: %.2f -> %.2f" % (current_value, converted_value))
                return converted_value

            start_time_history = now - timedelta(minutes=10)
            self.hass.log("Getting history for %s from %s" % (tracker, start_time_history))
            
            data = self.hass.get_history(entity_id=tracker, start_time=start_time_history)
            self.hass.log("History data length: %d" % len(data))
            
            if len(data) > 0:
                if len(data[0]) > 0:
                    try:
                        state = float(data[0][0]['state'])
                        date = data[0][0]['last_changed']

                        self.hass.log("State value for %s: %.2f w (from %s)" % (tracker, state, date))

                        diffTime = now.astimezone(timezone.utc) - date
                        self.hass.log("Time difference: %.2f seconds" % diffTime.seconds)
                        
                        rate_current = ((current_value - state) / float(diffTime.seconds)) * 3600.0
                        self.hass.log("Calculated rate for %s: %.2f w/h" % (tracker, rate_current))
                        return rate_current
                    except ValueError as ve:
                        self.hass.log("ERROR: ValueError in energy_consumption_rate for %s: %s" % (tracker, str(ve)))
                    except Exception as ex:
                        self.hass.log("ERROR: Unexpected exception in energy_consumption_rate for %s: %s" % 
                                     (tracker, str(ex)))
                else:
                    self.hass.log("No history data available for %s" % tracker)
            else:
                self.hass.log("No history data returned for %s" % tracker)

            self.hass.log("Returning 0 for %s (no valid data)" % tracker)
            return float(0)
            
        except Exception as ex:
            self.hass.log("ERROR: Exception in energy_consumption_rate for %s: %s" % (tracker, str(ex)))
            return float(0)

    def update_consumption_trackers(self, consumption_config):
        """Update all consumption trackers with current rates.
        
        Args:
            consumption_config: Configuration dictionary for consumptions
        """
        for priority, consumptions in self._consumptions.items():
            for key, value in consumptions.items():                
                # Get the consumption configuration
                if key in consumption_config:
                    energy_consumption_rate = self.energy_consumption_rate(consumption_config[key]["tracker"])

                    self.hass.log("Energy consumption rate for %s: %.2f w" % (key, energy_consumption_rate))

                    if energy_consumption_rate > 0:
                        value.real_usage = energy_consumption_rate
                    else:
                        value.real_usage = 0

    def get_consumptions(self):
        """Get the consumptions dictionary."""
        return self._consumptions

    def get_turned_on(self):
        """Get the list of turned on consumers."""
        return self._turned_on

    def get_known(self):
        """Get the list of known consumers."""
        return self._known

    def manage_additional_consumption(self, exported_watt, panel_to_house_w, consumption_config):
        """Manage additional consumption based on exported power.
        
        Args:
            exported_watt: Current exported power in watts
            panel_to_house_w: Solar panel production to house in watts
            consumption_config: Configuration dictionary for consumptions
        """
        if "consumption" not in consumption_config:
            self.hass.log("No consumption configuration found in args")
            return

        self.hass.log("=== Additional Consumption Logic ===")
        consumptions = consumption_config["consumption"]
        self.hass.log("Available consumptions: %s" % list(consumptions.keys()))

        for priority, cur_consumptions in self._consumptions.items():
            self.hass.log("Current active consumptions for priority %d: %s" % (priority, list(cur_consumptions.keys())))
            for key, value in cur_consumptions.items():
                self.hass.log("Current active consumption '%s' for priority %d: %d" % (key, priority, value.real_usage))

        battery_charge_in_kwh = self.battery_manager.get_current_battery_capacity()
        
        # Check daily energy added to battery from PV vs AC charging
        # Only trigger midday heating topup if more energy came from PV than AC today
        # This prevents false triggers when battery is high from completed AC charging
        pv_to_battery_daily_kwh = 0.0
        ac_to_battery_daily_kwh = 0.0
        try:
            pv_to_battery_daily_kwh = float(self.hass.get_state("sensor.solar_panel_to_battery_daily", default=0))
            if pv_to_battery_daily_kwh < 0:
                pv_to_battery_daily_kwh = 0.0
        except Exception as ex:
            self.hass.log("Error getting PV to battery daily energy: %s" % str(ex))
        
        try:
            ac_to_battery_daily_kwh = float(self.hass.get_state("sensor.solar_grid_to_battery_daily", default=0))
            if ac_to_battery_daily_kwh < 0:
                ac_to_battery_daily_kwh = 0.0
        except Exception as ex:
            self.hass.log("Error getting AC to battery daily energy: %s" % str(ex))
        
        # Only consider battery charge as valid for consume_more if PV has contributed more energy than AC today
        # This ensures we only trigger midday topup when battery charge is primarily from PV, not AC
        # Use a threshold: PV energy must be at least 0.5 kWh more than AC energy (to account for noise)
        pv_advantage_kwh = pv_to_battery_daily_kwh - ac_to_battery_daily_kwh
        pv_advantage_threshold_kwh = 0.5  # Minimum advantage for PV over AC
        
        if pv_advantage_kwh >= pv_advantage_threshold_kwh:
            # PV has contributed more energy than AC today, so battery charge is likely from PV
            if battery_charge_in_kwh > 8.5:
                self.hass.log("PV advantage today: %.3f kWh (PV: %.3f, AC: %.3f), battery charge: %.3f kWh - calling consume_more" % 
                             (pv_advantage_kwh, pv_to_battery_daily_kwh, ac_to_battery_daily_kwh, battery_charge_in_kwh))
                for ec in self._known:
                    self.hass.log("Calling consume_more for consumer: %s" % ec.name)
                    ec.consume_more()
            else:
                self.hass.log("PV advantage today: %.3f kWh (PV: %.3f, AC: %.3f) but battery charge (%.3f kWh) not high enough for consume_more" % 
                             (pv_advantage_kwh, pv_to_battery_daily_kwh, ac_to_battery_daily_kwh, battery_charge_in_kwh))
        else:
            # AC has contributed more (or equal) energy than PV today, likely from AC charging
            # Don't trigger midday topup even if battery is high
            self.hass.log("AC advantage or insufficient PV today: PV advantage %.3f kWh (PV: %.3f, AC: %.3f) < %.3f kWh threshold, battery charge: %.3f kWh - skipping consume_more to avoid AC charging trigger" % 
                         (pv_advantage_kwh, pv_to_battery_daily_kwh, ac_to_battery_daily_kwh, pv_advantage_threshold_kwh, battery_charge_in_kwh)) 

        # If we have export power check if we can use it
        if exported_watt > 300:
            self._handle_export_power(exported_watt, consumptions)
        else:
            self._handle_low_export_power(exported_watt, panel_to_house_w, consumptions)

    def _handle_export_power(self, exported_watt, consumptions):
        """Handle high exported power by activating or leveling up consumptions."""
        self.hass.log("Exported power (%.2f w) > 300w threshold, checking for additional consumption opportunities" % exported_watt)
        
        for key, value in consumptions.items():
            try:
                self.hass.log("Checking consumption key: %s" % key)

                priority = value["priority"]
                if priority not in self._consumptions:
                    self._consumptions[priority] = {}

                # Check if higher priority consumptions are active
                if priority > 0:
                    for higher_priority, higher_priority_consumptions in self._consumptions.items():
                        if higher_priority < priority:
                            self.hass.log("Checking higher priority consumptions for priority %d" % higher_priority)
                            for higher_priority_key, higher_priority_value in higher_priority_consumptions.items():
                                self.hass.log("Checking higher priority consumption '%s' for priority %d, real usage: %.2f" % 
                                             (higher_priority_key, higher_priority, higher_priority_value.real_usage))
                                if higher_priority_value.real_usage > 50:
                                    self.hass.log("Higher priority consumption '%s' is active, skipping" % higher_priority_key)
                                    raise Exception("Higher priority consumption found")
                
                if key not in self._consumptions[priority]:
                    self._activate_consumption(key, value, exported_watt, priority)
                    return
                else:
                    self.hass.log("Consumption '%s' already active, checking for level-up" % key)
                    if self._level_up_consumption(key, value, exported_watt, priority):
                        return
            except Exception as e:
                self.hass.log("Exception: %s" % e)
                continue

    def _activate_consumption(self, key, value, exported_watt, priority):
        """Activate a new consumption if conditions are met."""
        self.hass.log("Consumption '%s' not currently active, evaluating for activation" % key)

        # Get the lowest usage stage
        lowest_usage = float(99999999)
        lowest_stage = 0
        for ik, iv in enumerate(value["stages"]): 
            if iv["usage"] < lowest_usage:
                lowest_usage = iv["usage"]
                lowest_stage = ik

        self.hass.log("Lowest usage for '%s': stage=%d, usage=%.2f" % (key, lowest_stage, lowest_usage))

        if lowest_usage < exported_watt:
            self.hass.log("Condition met: lowest_usage (%.2f) < exported_watt (%.2f)" % (lowest_usage, exported_watt))
            
            # We need to turn on
            stage = value["stages"][lowest_stage]
            self.hass.log("Activating consumption '%s' with switch '%s'" % (key, stage["switch"]))
            self._turn_on_switch(stage["switch"])

            self.hass.log("Adding consumption: %s, %d, %d" % (key, lowest_stage, lowest_usage))
            self._consumptions[priority][key] = AdditionalConsumer(lowest_stage, lowest_usage, lowest_usage)
            
            self.virtual_entity_manager.call_virtual_entity(key, "usage_change", lowest_usage)

    def _level_up_consumption(self, key, value, exported_watt, priority):
        """Level up an existing consumption if possible.
        
        Returns:
            bool: True if level-up occurred, False otherwise
        """
        c = self._consumptions[priority][key]
        self.hass.log("Checking level-up for '%s': current stage=%d, usage=%.2f" % (key, c.stage, c.usage))

        # Find the lowest usage which is still above the current usage
        lowest_usage = float(99999999)
        lowest_stage = 0
        for ik, iv in enumerate(value["stages"]):
            if iv["usage"] > c.usage and iv["usage"] < lowest_usage:
                lowest_usage = iv["usage"]
                lowest_stage = ik

        if lowest_usage > 0:
            self.hass.log("Found potential level-up for '%s': stage=%d, usage=%.2f" % (key, lowest_stage, lowest_usage))
            # Do we have enough capacity?
            diff = lowest_usage - c.usage
            if exported_watt > diff:
                self.hass.log("Leveling up consumption: %s, %d, %d" % (key, lowest_stage, lowest_usage))

                # Old stage
                stage = value["stages"][c.stage]

                # New stage
                new_stage = value["stages"][lowest_stage]

                # Do we need to switch?
                if stage["switch"] != new_stage["switch"]:
                    self.hass.log("Switching from '%s' to '%s'" % (stage["switch"], new_stage["switch"]))
                    self._turn_off_switch(stage["switch"])
                    self._turn_on_switch(new_stage["switch"])
                else:
                    self.hass.log("No switch change needed, same switch: '%s'" % stage["switch"])

                self.virtual_entity_manager.call_virtual_entity(key, "usage_change", new_stage["usage"])

                c.stage = lowest_stage
                c.usage = new_stage["usage"]
                self.hass.log("Updated consumption '%s': stage=%d, usage=%.2f" % (key, c.stage, c.usage))

                return True
            else:
                self.hass.log("Insufficient exported power for level-up: need %.2f, have %.2f" % (diff, exported_watt))
        else:
            self.hass.log("No level-up opportunity found for '%s'" % key)
        
        return False

    def _handle_low_export_power(self, exported_watt, panel_to_house_w, consumptions):
        """Handle low exported power by reducing or turning off consumptions."""
        self.hass.log("Exported power (%.2f w) <= 300w threshold, checking for consumption reduction" % exported_watt)

        for key, value in consumptions.items():
            priority = value["priority"]
            if priority not in self._consumptions:
                continue

            if key in self._consumptions[priority]:
                c = self._consumptions[priority][key]
                self.hass.log("Checking reduction for '%s': current stage=%d, usage=%.2f, real usage=%.2f" % 
                             (key, c.stage, c.usage, c.real_usage))
                    
                panel_to_house_w = panel_to_house_w - c.real_usage
                if panel_to_house_w < 0:
                    self._reduce_consumption(key, value, panel_to_house_w, priority)
                    return

    def _reduce_consumption(self, key, value, panel_to_house_w, priority):
        """Reduce consumption by leveling down or turning off."""
        c = self._consumptions[priority][key]
        self.hass.log("Condition met: current usage (%.2f) > panel_to_house_w (%.2f)" % (c.usage, panel_to_house_w))
        
        # Check if we can level down
        # Find the highest usage which is below the current usage
        highest_usage = float(0)
        highest_stage = 0
        for ik, iv in enumerate(value["stages"]):
            if iv["usage"] < c.usage and iv["usage"] > highest_usage:
                highest_usage = iv["usage"]
                highest_stage = ik
        
        self.hass.log("Highest usage below current: stage=%d, usage=%.2f" % (highest_stage, highest_usage))
        
        if highest_usage > 0 and highest_usage < panel_to_house_w:
            self._level_down_consumption(key, value, highest_stage, highest_usage, priority)
        else:
            self._turn_off_consumption(key, value, priority)

    def _level_down_consumption(self, key, value, highest_stage, highest_usage, priority):
        """Level down a consumption to a lower stage."""
        c = self._consumptions[priority][key]
        self.hass.log("Leveling down consumption: %s to stage %d (%.2f w)" % (key, highest_stage, highest_usage))
        
        # Old stage
        stage = value["stages"][c.stage]

        # New stage
        new_stage = value["stages"][highest_stage]

        # Do we need to switch?
        if stage["switch"] != new_stage["switch"]:
            self.hass.log("Switching from '%s' to '%s'" % (stage["switch"], new_stage["switch"]))
            self._turn_off_switch(stage["switch"])
            self._turn_on_switch(new_stage["switch"])
        else:
            self.hass.log("No switch change needed for level-down")

        self.virtual_entity_manager.call_virtual_entity(key, "usage_change", new_stage["usage"])

        c.stage = highest_stage
        c.usage = new_stage["usage"]
        self.hass.log("Updated consumption '%s': stage=%d, usage=%.2f" % (key, c.stage, c.usage))

    def _turn_off_consumption(self, key, value, priority):
        """Turn off a consumption completely."""
        self.hass.log("No suitable level-down found or insufficient power, turning off consumption: %s" % key)

        # Check if we can turn off
        can_be_turned_off = True
        if "can_be_turned_off" in value:
            can_be_turned_off = value["can_be_turned_off"]

        if not can_be_turned_off:
            self.hass.log("Consumption '%s' cannot be turned off, skipping" % key)
            return

        # We need to turn off
        c = self._consumptions[priority][key]
        stage = value["stages"][c.stage]
        self._turn_off_switch(stage["switch"])
        del self._consumptions[priority][key]

        # If there are no other consumptions with the same priority, we need to remove the priority
        if len(self._consumptions[priority]) == 0:
            del self._consumptions[priority]
        
        self.virtual_entity_manager.call_virtual_entity(key, "usage_change", 0)
        self.hass.log("Removing consumption: %s" % key)

    def _turn_on_switch(self, entity):
        """Turn on a switch entity (virtual or regular).
        
        Args:
            entity: The entity ID to turn on
        """
        try:
            if entity.startswith("virtual."):
                ent = entity.split(".")[1]
                self.hass.log("Turning on virtual entity: %s" % ent)
                self.virtual_entity_manager.turn_on_virtual(ent)
            else:
                self.hass.log("Turning on regular entity: %s" % entity)
                self.hass.turn_on(entity)
                self.hass.log("Regular entity %s turned on" % entity)
        except Exception as ex:
            self.hass.log("ERROR in _turn_on_switch for %s: %s" % (entity, str(ex)))

    def _turn_off_switch(self, entity):
        """Turn off a switch entity (virtual or regular).
        
        Args:
            entity: The entity ID to turn off
        """
        try:
            if entity.startswith("virtual."):   
                ent = entity.split(".")[1]
                self.hass.log("Turning off virtual entity: %s" % ent)
                self.virtual_entity_manager.turn_off_virtual(ent)
            else:
                self.hass.log("Turning off regular entity: %s" % entity)
                self.hass.turn_off(entity)
                self.hass.log("Regular entity %s turned off" % entity)
        except Exception as ex:
            self.hass.log("ERROR in _turn_off_switch for %s: %s" % (entity, str(ex)))

    def _estimated_production_tomorrow(self):
        """Estimate solar production for tomorrow.
        
        Returns:
            float: Estimated production in kWh
        """
        if self.ac_charging_manager:
            return self.ac_charging_manager._estimated_production_tomorrow()
        
        # Fallback if AC charging manager not available
        try:
            now = self.hass.get_now()
            if now.hour < 2:
                return float(self.hass.get_state("sensor.solcast_pv_forecast_prognose_heute"))
            else:
                return float(self.hass.get_state("sensor.solcast_pv_forecast_prognose_morgen"))
        except Exception as ex:
            self.hass.log("ERROR in _estimated_production_tomorrow: %s" % str(ex))
            return 0.0

