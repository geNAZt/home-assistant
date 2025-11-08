"""AC charging management for the energy manager."""

from datetime import datetime, timedelta


class ACChargingManager:
    """Manages AC charging logic for battery pre-charging."""

    def __init__(self, hass_instance, battery_manager, state_manager):
        """Initialize AC charging manager.
        
        Args:
            hass_instance: The AppDaemon Hass instance
            battery_manager: BatteryManager instance
            state_manager: StateManager instance
        """
        self.hass = hass_instance
        self.battery_manager = battery_manager
        self.state_manager = state_manager

    def _estimated_production_tomorrow(self):
        """Estimate solar production for tomorrow.
        
        Returns:
            float: Estimated production in kWh
        """
        try:
            now = self.hass.get_now()
            self.hass.log("Current time: %s (hour: %d)" % (now, now.hour))

            if now.hour < 2:
                forecast_value = float(self.hass.get_state("sensor.solcast_pv_forecast_prognose_heute"))
                self.hass.log("Using today's forecast (hour < 2): %.2f" % forecast_value)
                return forecast_value
            else:
                forecast_value = float(self.hass.get_state("sensor.solcast_pv_forecast_prognose_morgen"))
                self.hass.log("Using tomorrow's forecast (hour >= 2): %.2f" % forecast_value)
                return forecast_value
        except Exception as ex:
            self.hass.log("ERROR in _estimated_production_tomorrow: %s" % str(ex))
            return 0.0

    def manage_ac_charging(self):
        """Manage AC charging based on time of day and battery state."""
        now = self.hass.get_now()
        self.hass.log("Current time: %s (hour: %d)" % (now, now.hour))

        battery_charge_in_kwh = self.battery_manager.get_current_battery_capacity()
        battery_remaining_capacity = self.battery_manager.get_remaining_battery_capacity()
        self.hass.log("Battery capacity: %.2f kWh" % battery_charge_in_kwh)

        # Check for manual override
        override = self.hass.get_state("input_boolean.charge_solar_battery_override")
        self.hass.log("Manual override: %s" % override)
        if override == "on":
            self.hass.log("Manual override is on, skipping AC charging logic")

        # Control AC charging
        # 
        # Concept here is that we want to skip pricy hours in the morning by precharging our battery with the kWh needed.
        # When looking into tibber pricing data the sweetspot is around 3a.m for this. We need to charge until we hit PV operation.
        # For this we need to estimate how much energy we need per hour and when sunrise is
        if now.hour < 6:
            self._handle_early_morning_charging(now, battery_charge_in_kwh, battery_remaining_capacity)
        elif override == "on":
            self._handle_override_charging(battery_charge_in_kwh, battery_remaining_capacity)
        else:
            self.hass.log("Setting PV storage mode to 'Maximize self consumption' (not early morning)")
            self.state_manager.ensure_state("select.pv_storage_remote_command_mode", "Maximize self consumption")

    def _handle_early_morning_charging(self, now, battery_charge_in_kwh, battery_remaining_capacity):
        """Handle AC charging logic for early morning hours."""
        self.hass.log("=== AC Charging Logic (Early Morning) ===")
        stop_charging = datetime(now.year, now.month, now.day, 6, 0, 0, 0, now.tzinfo)
        self.hass.log("Stop charging time: %s" % stop_charging)

        tomorrow_estimate = self._estimated_production_tomorrow()

        self.hass.log("Battery status: remaining=%.3f kWh, current=%.3f kWh, tomorrow_estimate=%.3f kWh" % 
                     (battery_remaining_capacity, battery_charge_in_kwh, tomorrow_estimate))

        time_sunrise = datetime.fromisoformat(self.hass.get_state("sensor.sun_next_rising"))

        # Use solcast to get first hour of tomorrow's production
        try:
            solcast_ret = self.hass.call_service(
                "solcast_solar/query_forecast_data", 
                service_data={
                    "start_date_time": now, 
                    "end_date_time": now + timedelta(days=1)
                }
            )
            
            if solcast_ret and "result" in solcast_ret and "response" in solcast_ret["result"]:
                pv_resp = solcast_ret["result"]["response"].get("data", [])
                if pv_resp:
                    for item in pv_resp:
                        if float(item.get('pv_estimate', 0)) > 0.5:
                            time_sunrise = datetime.fromisoformat(item.get('period_start', ''))
                            break
                else:
                    self.hass.log("Warning: No Solcast PV data received")
            else:
                self.hass.log("Warning: Invalid Solcast response structure")
        except Exception as ex:
            self.hass.log(f"Error getting Solcast forecast: {ex}")

        self.hass.log("Sunrise time: %s" % time_sunrise)
        
        time_till_sunrise = (time_sunrise - stop_charging).total_seconds()
        minutes, rest = divmod(time_till_sunrise, 60)

        self.hass.log("Time calculations: minutes=%d, total_seconds=%.2f, rest=%.2f" % (minutes, time_till_sunrise, rest))

        # We simply assume that we consume 1000 watt per hour for now until we found a way to predict this
        needed_watt_per_minute = 500 / 60
        needed_kwh = (minutes * needed_watt_per_minute) / 1000

        self.hass.log("Energy needs: watt_per_minute=%.2f, needed_kwh=%.3f" % (needed_watt_per_minute, needed_kwh))
        self.hass.log("Wanting to charge %.3f kWh, having %.3f kWh in battery" % (needed_kwh, battery_charge_in_kwh))

        if tomorrow_estimate / 2 < battery_remaining_capacity:
            self.hass.log("Condition met: tomorrow_estimate/2 (%.3f) < battery_remaining_capacity (%.3f)" % 
                         (tomorrow_estimate / 2, battery_remaining_capacity))
            if battery_charge_in_kwh < needed_kwh:
                self.hass.log("Setting PV storage mode to 'Charge from PV and AC' (need %.3f, have %.3f)" % 
                             (needed_kwh, battery_charge_in_kwh))
                self.state_manager.ensure_state("select.pv_storage_remote_command_mode", "Charge from PV and AC")
                self.hass.call_service("number/set_value", entity_id="number.pv_storage_remote_charge_limit", value=5000)
            else:
                self.hass.log("Setting PV storage mode to 'Off' (have sufficient charge)")
                self.state_manager.ensure_state("select.pv_storage_remote_command_mode", "Off")
        else:
            self.hass.log("Condition not met: tomorrow_estimate/2 (%.3f) >= battery_remaining_capacity (%.3f)" % 
                        (tomorrow_estimate / 2, battery_remaining_capacity))
            self.hass.log("Setting PV storage mode to 'Maximize self consumption'")
            self.state_manager.ensure_state("select.pv_storage_remote_command_mode", "Maximize self consumption")

    def _handle_override_charging(self, battery_charge_in_kwh, battery_remaining_capacity):
        """Handle AC charging when manual override is enabled."""
        self.hass.log("Override is on, battery_remaining_capacity (%.3f)" % battery_remaining_capacity)
        if battery_remaining_capacity > 2:
            self.hass.log("Setting PV storage mode to 'Charge from PV and AC' (need %.3f, have %.3f)" % 
                         (battery_remaining_capacity - 2, battery_charge_in_kwh))
            self.state_manager.ensure_state("select.pv_storage_remote_command_mode", "Charge from PV and AC")
            self.hass.call_service("number/set_value", entity_id="number.pv_storage_remote_charge_limit", value=5000)
        else:
            self.hass.log("Setting PV storage mode to 'Maximize self consumption' (have sufficient charge)")
            self.state_manager.ensure_state("select.pv_storage_remote_command_mode", "Maximize self consumption")

