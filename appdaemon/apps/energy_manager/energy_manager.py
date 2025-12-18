"""Main Energy Manager class for Home Assistant AppDaemon."""

import appdaemon.plugins.hass.hassapi as hass
import threading
import time

from .virtual_entities import VirtualEntityManager
from .consumption import ConsumptionManager
from .power_monitoring import PowerMonitoring


class EnergyManager(hass.Hass):
    """Main energy manager that orchestrates all energy management components."""

    def initialize(self):
        """Initialize the energy manager and all its components."""
        self.log("=== Energy Manager Initialize Started (Thread: %s) ===" % threading.current_thread().name)
        start_time = time.time()
        
        # Initialize virtual entity manager
        virtual_entities_config = self.args.get("virtuals", {})
        self.virtual_entity_manager = VirtualEntityManager(self, virtual_entities_config)
        
        # Initialize power monitoring
        self.power_monitoring = PowerMonitoring(self, self.virtual_entity_manager)
        
        # Initialize consumption manager
        self.consumption_manager = ConsumptionManager(
            self, 
            self.virtual_entity_manager
        )

        # Listen for state changes
        self.listen_state(self.power_monitoring.on_solar_panel_production, "sensor.solar_panel_production_w")
        self.listen_state(self.power_monitoring.on_exported_power, "sensor.solar_exported_power_w")
        self.listen_state(self.power_monitoring.on_imported_power, "sensor.solar_imported_power_w")

        # Disable all consumptions to ensure clean state
        consumptions = self.args.get("consumption", {})
        for key, value in consumptions.items():
            stages = value["stages"]
            for stage in stages:
                self.consumption_manager._turn_off_switch(stage["switch"])

                if stage["switch"].startswith("virtual."):
                    self.virtual_entity_manager.call_virtual_entity(stage["switch"].split(".")[1], "usage_change", 0)

                self.log("Disabled consumption '%s' with switch '%s'" % (key, stage["switch"]))

        self.run_every(self.run_every_c, "now", 60)
        
        init_time = time.time() - start_time
        self.log("=== Energy Manager Initialize Completed (Thread: %s, Duration: %.3fs) ===" % 
                (threading.current_thread().name, init_time))

    def run_every_c(self, c):
        """Callback for periodic updates.
        
        Args:
            c: Callback argument
        """
        try:
            self.update()
        except Exception as ex:
            self.log("ERROR in run_every_c: %s" % str(ex))

    def update(self):
        """Main update method that runs periodically."""
        self.log("=== Energy Manager Update Method Started (Thread: %s) ===" % threading.current_thread().name)
        update_start_time = time.time()

        # Update all consumption trackers
        consumption_config = self.args.get("consumption", {})
        self.consumption_manager.update_consumption_trackers(consumption_config)

        # Get proper solar panel production
        panel_to_house_w = self.power_monitoring.get_average_solar_production()
        exported_watt = self.power_monitoring.get_average_exported_power()

        self.log("Checking for additional consumption, exported %.2f w, produced %.2f w" % (exported_watt, panel_to_house_w))

        # Manage additional consumption
        self.consumption_manager.manage_additional_consumption(
            exported_watt, 
            panel_to_house_w, 
            self.args
        )

        update_duration = time.time() - update_start_time
        self.log("=== Energy Manager Update Method Completed (Thread: %s, Duration: %.3fs) ===" % 
                (threading.current_thread().name, update_duration))

