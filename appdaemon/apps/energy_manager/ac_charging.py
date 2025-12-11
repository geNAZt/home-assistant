"""AC charging management for the energy manager."""

from datetime import datetime, timedelta

PV_START_WATTAGE = 500  # Wattage threshold for PV start

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
        

    def _get_pv_forecast_data(self, start_time, end_time):
        """Get detailed PV forecast data from Solcast.
        
        Args:
            start_time: Start datetime for forecast
            end_time: End datetime for forecast
            
        Returns:
            list: List of forecast data points with 'period_start' and 'pv_estimate'
        """
        try:
            solcast_ret = self.hass.call_service(
                "solcast_solar/query_forecast_data", 
                service_data={
                    "start_date_time": start_time, 
                    "end_date_time": end_time
                }
            )
            
            if solcast_ret and "result" in solcast_ret and "response" in solcast_ret["result"]:
                pv_resp = solcast_ret["result"]["response"].get("data", [])
                if pv_resp:
                    return pv_resp
                else:
                    self.hass.log("Warning: No Solcast PV data received")
            else:
                self.hass.log("Warning: Invalid Solcast response structure")
        except Exception as ex:
            self.hass.log(f"Error getting Solcast forecast: {ex}")
        
        return []

    def _calculate_24h_pv_forecast(self, now):
        """Calculate total PV forecast for the next 24 hours.
        
        Args:
            now: Current datetime
            
        Returns:
            tuple: (total_pv_kwh, forecast_data_list, pv_peak_end_time)
                - total_pv_kwh: Total PV production forecast in kWh
                - forecast_data_list: List of forecast data points
                - pv_peak_end_time: Datetime when PV peak ends (when production drops significantly)
        """
        end_time = now + timedelta(hours=24)
        forecast_data = self._get_pv_forecast_data(now, end_time)
        
        total_pv_kwh = 0.0
        pv_peak_end_time = None
        max_pv_power = 0.0
        max_pv_time = None
        
        if forecast_data:
            # Calculate total PV production and find peak
            for item in forecast_data:
                try:
                    pv_estimate_kw = float(item.get('pv_estimate', 0))
                    # Solcast provides kW, convert to kWh (assuming 30-minute intervals)
                    total_pv_kwh += pv_estimate_kw * 0.5
                    
                    # Track peak production time
                    if pv_estimate_kw > max_pv_power:
                        max_pv_power = pv_estimate_kw
                        period_start = item.get('period_start', '')
                        if period_start:
                            max_pv_time = datetime.fromisoformat(period_start)
                except (ValueError, TypeError) as ex:
                    self.hass.log("Error processing forecast item: %s" % str(ex))
                    continue
            
            # Find when PV peak ends (when production drops below 20% of peak)
            peak_threshold = max_pv_power * 0.2
            if max_pv_time and max_pv_power > 0:
                for item in forecast_data:
                    try:
                        period_start = item.get('period_start', '')
                        if not period_start:
                            continue
                        item_time = datetime.fromisoformat(period_start)
                        if item_time > max_pv_time:
                            pv_estimate_kw = float(item.get('pv_estimate', 0))
                            if pv_estimate_kw < peak_threshold:
                                pv_peak_end_time = item_time
                                break
                    except (ValueError, TypeError) as ex:
                        self.hass.log("Error processing forecast item for peak end: %s" % str(ex))
                        continue
                
                # If no drop found, use end of forecast period
                if pv_peak_end_time is None and forecast_data:
                    try:
                        last_item = forecast_data[-1]
                        period_start = last_item.get('period_start', '')
                        if period_start:
                            pv_peak_end_time = datetime.fromisoformat(period_start)
                    except (ValueError, TypeError) as ex:
                        self.hass.log("Error getting last forecast time: %s" % str(ex))
        
        self.hass.log("24h PV forecast: total=%.3f kWh, peak_end=%s" % (total_pv_kwh, pv_peak_end_time))
        return total_pv_kwh, forecast_data, pv_peak_end_time

    def _estimate_24h_consumption(self):
        """Estimate energy consumption for the next 24 hours.
        
        Returns:
            float: Estimated consumption in kWh
        """
        # Try to get historical consumption data, fallback to fixed rate
        try:
            # Use average consumption rate - assume 1000W average (24 kWh per day)
            # This is a conservative estimate, can be improved with historical data
            consumption_kwh = 24.0
            self.hass.log("Estimated 24h consumption: %.3f kWh" % consumption_kwh)
            return consumption_kwh
        except Exception as ex:
            self.hass.log("Error estimating consumption, using default: %s" % str(ex))
            return 24.0

    def _find_pv_start_time(self, forecast_data, now):
        """Find when PV production reaches the start wattage threshold.
        
        Args:
            forecast_data: List of forecast data points
            now: Current datetime
            
        Returns:
            datetime: When PV reaches start wattage, or None if not found
        """
        if not forecast_data:
            return None
        
        for item in forecast_data:
            try:
                period_start = item.get('period_start', '')
                if not period_start:
                    continue
                item_time = datetime.fromisoformat(period_start)
                if item_time > now:
                    pv_estimate_kw = float(item.get('pv_estimate', 0))
                    pv_estimate_w = pv_estimate_kw * 1000
                    if pv_estimate_w >= PV_START_WATTAGE:
                        return item_time
            except (ValueError, TypeError) as ex:
                self.hass.log("Error processing forecast item for PV start: %s" % str(ex))
                continue
        
        return None

    def _calculate_energy_until_pv_start(self, pv_start_time, now):
        """Calculate energy needed until PV reaches start wattage.
        
        Args:
            pv_start_time: Datetime when PV reaches start wattage
            now: Current datetime
            
        Returns:
            float: Energy needed in kWh (assuming 500W consumption rate)
        """
        if pv_start_time is None:
            return 0.0
        
        time_until_pv = (pv_start_time - now).total_seconds()
        hours_until_pv = time_until_pv / 3600.0
        
        # Assume 500W average consumption until PV starts
        energy_needed_kwh = (PV_START_WATTAGE / 1000.0) * hours_until_pv
        
        self.hass.log("Energy needed until PV start: %.3f kWh (%.2f hours at %.0fW)" % 
                     (energy_needed_kwh, hours_until_pv, PV_START_WATTAGE))
        return energy_needed_kwh

    def manage_ac_charging(self):
        """Manage AC charging based on time of day and battery state."""
        now = self.hass.get_now()
        self.hass.log("Current time: %s (hour: %d)" % (now, now.hour))

        battery_charge_in_kwh = self.battery_manager.get_current_battery_capacity()
        battery_remaining_capacity = self.battery_manager.get_remaining_battery_capacity()
        
        # Get battery max capacity and current SoC
        try:
            battery_max_capacity = float(self.hass.get_state("sensor.pv_battery1_size_max")) / 1000.0
            battery_soc_percent = float(self.hass.get_state("sensor.pv_battery1_state_of_charge"))
        except Exception as ex:
            self.hass.log("ERROR getting battery capacity: %s" % str(ex))
            battery_max_capacity = 0.0
            battery_soc_percent = 0.0
        
        self.hass.log("Battery: current=%.2f kWh, remaining=%.2f kWh, SoC=%.1f%%, max=%.2f kWh" % 
                     (battery_charge_in_kwh, battery_remaining_capacity, battery_soc_percent, battery_max_capacity))

        # Check for manual override
        override = self.hass.get_state("input_boolean.charge_solar_battery_override")
        self.hass.log("Manual override: %s" % override)
        
        if override == "on":
            self._handle_override_charging(battery_charge_in_kwh, battery_remaining_capacity)
        elif now.hour < 6:
            self._handle_early_morning_charging(now, battery_charge_in_kwh, battery_remaining_capacity, 
                                                battery_max_capacity, battery_soc_percent)
        else:
            self.hass.log("Setting PV storage mode to 'Maximize self consumption' (not early morning)")
            self.state_manager.ensure_state("select.pv_storage_remote_command_mode", "Maximize self consumption")

    def _handle_early_morning_charging(self, now, battery_charge_in_kwh, battery_remaining_capacity, 
                                      battery_max_capacity, battery_soc_percent):
        """Handle AC charging logic for early morning hours based on PV forecast."""
        self.hass.log("=== AC Charging Logic (Early Morning) ===")
        
        # Get 24h PV forecast
        total_pv_kwh, forecast_data, pv_peak_end_time = self._calculate_24h_pv_forecast(now)
        
        # Estimate 24h consumption
        consumption_24h_kwh = self._estimate_24h_consumption()
        
        # Calculate if PV forecast is enough to cover next 24h
        pv_sufficient = total_pv_kwh >= consumption_24h_kwh
        
        self.hass.log("PV forecast analysis: pv=%.3f kWh, consumption=%.3f kWh, sufficient=%s" % 
                     (total_pv_kwh, consumption_24h_kwh, pv_sufficient))
        
        # Calculate energy needed to max out battery from current SoC
        energy_to_max_battery = battery_remaining_capacity
        
        # Check if PV forecast is enough to max out battery from current SoC
        pv_can_max_battery = total_pv_kwh >= energy_to_max_battery
        
        self.hass.log("Battery analysis: energy_to_max=%.3f kWh, pv_can_max=%s" % 
                     (energy_to_max_battery, pv_can_max_battery))
        
        if not pv_sufficient:
            # PV forecast is NOT enough to cover next 24h - charge to full capacity
            # Time charging to reach nearly 100% when PV peak ends
            self.hass.log("PV forecast insufficient - charging to full capacity")
            
            if pv_peak_end_time and pv_peak_end_time > now:
                time_until_peak_end = (pv_peak_end_time - now).total_seconds() / 3600.0
                energy_to_charge = battery_remaining_capacity
                
                # Calculate charging rate needed to reach ~75% by peak end
                target_soc = 75.0
                target_energy = battery_max_capacity * (target_soc / 100.0)
                current_energy = battery_max_capacity * (battery_soc_percent / 100.0)
                energy_needed = target_energy - current_energy
                
                if energy_needed > 0 and time_until_peak_end > 0:
                    # Calculate required charging power (assuming AC charging efficiency)
                    charging_power_w = (energy_needed / time_until_peak_end) * 1000.0
                    # Limit to reasonable AC charging power (e.g., 5000W max)
                    charging_power_w = min(charging_power_w, 5000.0)
                    
                    self.hass.log("Charging to %.1f%% by peak end: need %.3f kWh in %.2f hours (%.0fW)" % 
                                 (target_soc, energy_needed, time_until_peak_end, charging_power_w))
                    
                    if battery_soc_percent < target_soc:
                        self.state_manager.ensure_state("select.pv_storage_remote_command_mode", "Charge from PV and AC")
                        self.hass.call_service("number/set_value", entity_id="number.pv_storage_remote_charge_limit", 
                                              value=int(charging_power_w))
                    else:
                        self.hass.log("Battery already at target SoC")
                        self.state_manager.ensure_state("select.pv_storage_remote_command_mode", "Maximize self consumption")
                else:
                    # Charge at max rate if we can't calculate timing
                    self.state_manager.ensure_state("select.pv_storage_remote_command_mode", "Charge from PV and AC")
                    self.hass.call_service("number/set_value", entity_id="number.pv_storage_remote_charge_limit", value=5000)
            else:
                # Charge at max rate if we can't determine peak end
                self.state_manager.ensure_state("select.pv_storage_remote_command_mode", "Charge from PV and AC")
                self.hass.call_service("number/set_value", entity_id="number.pv_storage_remote_charge_limit", value=5000)
        
        elif pv_can_max_battery:
            # PV forecast IS enough to max out battery - only charge enough to make it till PV start
            self.hass.log("PV forecast sufficient to max battery - charging only until PV start")
            
            pv_start_time = self._find_pv_start_time(forecast_data, now)
            energy_needed_until_pv = self._calculate_energy_until_pv_start(pv_start_time, now)
            
            if energy_needed_until_pv > battery_charge_in_kwh:
                energy_to_charge = energy_needed_until_pv - battery_charge_in_kwh
                self.hass.log("Need to charge %.3f kWh to reach PV start (have %.3f, need %.3f)" % 
                             (energy_to_charge, battery_charge_in_kwh, energy_needed_until_pv))
                
                if energy_to_charge > 0.1:  # Only charge if we need at least 0.1 kWh
                    self.state_manager.ensure_state("select.pv_storage_remote_command_mode", "Charge from PV and AC")
                    self.hass.call_service("number/set_value", entity_id="number.pv_storage_remote_charge_limit", value=5000)
                else:
                    self.hass.log("Sufficient charge to reach PV start")
                    self.state_manager.ensure_state("select.pv_storage_remote_command_mode", "Maximize self consumption")
            else:
                self.hass.log("Sufficient charge to reach PV start (have %.3f, need %.3f)" % 
                             (battery_charge_in_kwh, energy_needed_until_pv))
                self.state_manager.ensure_state("select.pv_storage_remote_command_mode", "Maximize self consumption")
        
        else:
            # PV forecast is sufficient for 24h but not enough to max battery
            # In this case, don't charge (let PV handle it)
            self.hass.log("PV forecast sufficient for 24h but not enough to max battery - no AC charging")
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

