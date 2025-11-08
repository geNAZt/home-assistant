"""Battery management for the energy manager."""

from .constants import MIN_BATTERY_CHARGE


class BatteryManager:
    """Manages battery state and capacity calculations."""

    def __init__(self, hass_instance):
        """Initialize battery manager.
        
        Args:
            hass_instance: The AppDaemon Hass instance
        """
        self.hass = hass_instance

    def get_remaining_battery_capacity(self):
        """Get the remaining battery capacity that can be used.
        
        Returns:
            float: Remaining battery capacity in kWh
        """
        try:
            battery_max_capacity = float(self.hass.get_state("sensor.pv_battery1_size_max")) / float(1000)  # Given in Wh
            self.hass.log("Battery max capacity: %.3f kWh" % battery_max_capacity)
            
            # We need to remove MIN_BATTERY_CHARGE as buffer since we can't deep discharge the battery
            battery_min_capacity = battery_max_capacity * MIN_BATTERY_CHARGE
            self.hass.log("Battery min capacity (%.1f%%): %.3f kWh" % (MIN_BATTERY_CHARGE * 100, battery_min_capacity))
            
            battery_charge = float(self.hass.get_state("sensor.pv_battery1_state_of_charge")) / float(100)  # Given in full number percent
            self.hass.log("Battery charge: %.1f%%" % (battery_charge * 100))
            
            battery_capacity_used = battery_max_capacity * battery_charge
            self.hass.log("Battery capacity used: %.3f kWh" % battery_capacity_used)
            
            remaining = (battery_max_capacity - battery_capacity_used)
            self.hass.log("Remaining battery capacity: %.3f kWh" % remaining)
            return remaining
        except Exception as ex:
            self.hass.log("ERROR in _get_remaining_battery_capacity: %s" % str(ex))
            return 0.0

    def get_current_battery_capacity(self):
        """Get the current usable battery capacity (above minimum threshold).
        
        Returns:
            float: Current battery capacity in kWh
        """
        try:
            battery_max_capacity = float(self.hass.get_state("sensor.pv_battery1_size_max")) / float(1000)  # Given in Wh
            self.hass.log("Battery max capacity: %.3f kWh" % battery_max_capacity)
            
            battery_min_capacity = battery_max_capacity * MIN_BATTERY_CHARGE
            self.hass.log("Battery min capacity (%.1f%%): %.3f kWh" % (MIN_BATTERY_CHARGE * 100, battery_min_capacity))
            
            battery_charge = float(self.hass.get_state("sensor.pv_battery1_state_of_charge")) / float(100)  # Given in full number percent
            self.hass.log("Battery charge: %.1f%%" % (battery_charge * 100))
            
            battery_capacity_used = battery_max_capacity * battery_charge
            self.hass.log("Battery capacity used: %.3f kWh" % battery_capacity_used)
            
            current = battery_capacity_used - battery_min_capacity
            self.hass.log("Current battery capacity: %.3f kWh" % current)
            return current
        except Exception as ex:
            self.hass.log("ERROR in _get_current_battery_capacity: %s" % str(ex))
            return 0.0

    def get_battery_charge_percent(self):
        """Get the current battery charge percentage.
        
        Returns:
            float: Battery charge percentage (0-100)
        """
        try:
            return float(self.hass.get_state("sensor.pv_battery1_state_of_charge"))
        except Exception as ex:
            self.hass.log("ERROR in get_battery_charge_percent: %s" % str(ex))
            return 0.0

