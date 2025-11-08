"""Power monitoring for solar panel production and exported power."""


class PowerMonitoring:
    """Monitors solar panel production and exported/imported power."""

    def __init__(self, hass_instance, virtual_entity_manager):
        """Initialize power monitoring.
        
        Args:
            hass_instance: The AppDaemon Hass instance
            virtual_entity_manager: VirtualEntityManager instance
        """
        self.hass = hass_instance
        self.virtual_entity_manager = virtual_entity_manager
        
        self._solar_panel_production = 0
        self._solar_panel_amount = 0
        self._exported_power = 0
        self._exported_power_amount = 0

    def on_solar_panel_production(self, entity, attribute, old, new, cb_args):
        """Callback for solar panel production updates.
        
        Args:
            entity: The entity ID
            attribute: The attribute that changed
            old: Old value
            new: New value
            cb_args: Callback arguments
        """
        # Check if new is a number and update to 0 if not
        try:
            v = float(new)
        except ValueError:
            v = 0
            self.hass.log("WARNING: onSolarPanelProduction received non-numeric value: %s, using 0" % new)
            
        self._solar_panel_production += v
        self._solar_panel_amount += 1

    def on_exported_power(self, entity, attribute, old, new, cb_args):
        """Callback for exported power updates.
        
        Args:
            entity: The entity ID
            attribute: The attribute that changed
            old: Old value
            new: New value
            cb_args: Callback arguments
        """
        # Check if new is a number and update to 0 if not
        try:
            v = float(new)
        except ValueError:
            v = 0
            self.hass.log("WARNING: onExportedPower received non-numeric value: %s, using 0" % new)

        self._exported_power += v
        self._exported_power_amount += 1

        self.virtual_entity_manager.call_all_active_virtual_entities("exported_power_update", v)

    def on_imported_power(self, entity, attribute, old, new, cb_args):
        """Callback for imported power updates.
        
        Args:
            entity: The entity ID
            attribute: The attribute that changed
            old: Old value
            new: New value
            cb_args: Callback arguments
        """
        # Check if new is a number and update to 0 if not
        try:
            v = float(new)
        except ValueError:
            v = 0
            self.hass.log("WARNING: onImportedPower received non-numeric value: %s, using 0" % new)

        self.virtual_entity_manager.call_all_active_virtual_entities("imported_power_update", v)

    def get_average_solar_production(self):
        """Get the average solar panel production and reset counters.
        
        Returns:
            float: Average solar panel production in watts
        """
        panel_to_house_w = self._solar_panel_production / self._solar_panel_amount if self._solar_panel_amount > 0 else 0
        self.hass.log("Solar panel production calculation: production=%.2f, amount=%.2f, result=%.2f w" % 
                     (self._solar_panel_production, self._solar_panel_amount, panel_to_house_w))

        self._solar_panel_production = 0
        self._solar_panel_amount = 0
        
        return panel_to_house_w

    def get_average_exported_power(self):
        """Get the average exported power and reset counters.
        
        Returns:
            float: Average exported power in watts
        """
        exported_watt = self._exported_power / self._exported_power_amount if self._exported_power_amount > 0 else 0
        self.hass.log("Exported power calculation: exported=%.2f, amount=%.2f, result=%.2f w" % 
                     (self._exported_power, self._exported_power_amount, exported_watt))
        
        self._exported_power = 0
        self._exported_power_amount = 0
        
        return exported_watt

