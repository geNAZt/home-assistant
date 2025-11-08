"""Main Energy Manager class for Home Assistant AppDaemon."""

import appdaemon.plugins.hass.hassapi as hass
import adbase as ad
import threading
import time

from .models import EnergyConsumer
from .virtual_entities import VirtualEntityManager
from .phase_control import PhaseControl
from .battery import BatteryManager
from .consumption import ConsumptionManager
from .ac_charging import ACChargingManager
from .state_manager import StateManager
from .power_monitoring import PowerMonitoring


class EnergyManager(hass.Hass):
    """Main energy manager that orchestrates all energy management components."""

    def initialize(self):
        """Initialize the energy manager and all its components."""
        self.log("=== Energy Manager Initialize Started (Thread: %s) ===" % threading.current_thread().name)
        start_time = time.time()
        
        # Initialize state manager
        self.state_manager = StateManager(self)
        
        # Initialize virtual entity manager
        virtual_entities_config = self.args.get("virtuals", {})
        self.virtual_entity_manager = VirtualEntityManager(self, virtual_entities_config)
        
        # Initialize power monitoring
        self.power_monitoring = PowerMonitoring(self, self.virtual_entity_manager)
        
        # Initialize phase control
        self.phase_control = PhaseControl(self)
        
        # Initialize battery manager
        self.battery_manager = BatteryManager(self)
        
        # Initialize AC charging manager
        self.ac_charging_manager = ACChargingManager(
            self, 
            self.battery_manager, 
            self.state_manager
        )
        
        # Initialize consumption manager
        self.consumption_manager = ConsumptionManager(
            self, 
            self.battery_manager, 
            self.phase_control, 
            self.virtual_entity_manager,
            self.ac_charging_manager
        )

        # Ensure that solaredge is configured correctly
        self.state_manager.ensure_state("select.pv_storage_ac_charge_policy", "Always Allowed")
        self.state_manager.ensure_state("select.pv_storage_control_mode", "Remote Control")
        self.state_manager.ensure_state("select.pv_storage_remote_command_mode", "Maximize self consumption")

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
        return self.consumption_manager.register_consumer(group, name, phase, current, turn_on, turn_off, can_be_delayed, consume_more)

    @ad.global_lock
    def em_turn_on(self, ec: EnergyConsumer):
        """Turn on an energy consumer (thread-safe).
        
        Args:
            ec: The energy consumer to turn on
        """
        self.consumption_manager.turn_on_consumer(ec)

    @ad.global_lock
    def em_turn_off(self, ec: EnergyConsumer):
        """Turn off an energy consumer (thread-safe).
        
        Args:
            ec: The energy consumer to turn off
        """
        self.consumption_manager.turn_off_consumer(ec)

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

        # Manage AC charging
        self.ac_charging_manager.manage_ac_charging()

        # Manage additional consumption
        self.consumption_manager.manage_additional_consumption(
            exported_watt, 
            panel_to_house_w, 
            self.args
        )

        update_duration = time.time() - update_start_time
        self.log("=== Energy Manager Update Method Completed (Thread: %s, Duration: %.3fs) ===" % 
                (threading.current_thread().name, update_duration))

