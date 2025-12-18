"""Virtual entity management for the energy manager."""

import time


class VirtualEntityManager:
    """Manages virtual entities and their event handling."""

    def __init__(self, hass_instance, virtual_entities_config):
        """Initialize the virtual entity manager.
        
        Args:
            hass_instance: The AppDaemon Hass instance
            virtual_entities_config: Configuration dictionary for virtual entities
        """
        self.hass = hass_instance
        self._virtual_entities = {}
        
        # Register virtual entities
        for key, value in virtual_entities_config.items():
            from .models import VirtualEntity
            self._virtual_entities[key] = VirtualEntity(False, value["events"])

    def call_all_active_virtual_entities(self, event, value):
        """Call an event on all active virtual entities.
        
        Args:
            event: The event name to trigger
            value: The value to pass to the event handler
        """
        for key, entity in self._virtual_entities.items():
            if entity.switched:
                self.call_virtual_entity(key, event, value)

    def call_virtual_entity(self, entity, event, value):
        """Call an event on a specific virtual entity.
        
        Args:
            entity: The entity name
            event: The event name to trigger
            value: The value to pass to the event handler
        """
        start_time = time.time()
        
        if entity not in self._virtual_entities:
            self.hass.log("Virtual entity %s not found in _virtual_entities" % entity)
            return

        e = self._virtual_entities[entity]
        if event in e.events:
            self.hass.log("Executing virtual entity event code for %s" % entity)
            try:
                called = False
                # Try to find the entity in consumptions
                consumptions = getattr(self.hass, '_consumptions', {})
                for priority, consumption_dict in consumptions.items():
                    for key, entity_value in consumption_dict.items():
                        if key == entity:
                            exec(e.events[event]["code"], {"self": self.hass, "value": value, "entity": entity_value})
                            called = True
                            break

                if not called:
                    self.hass.log("Executing virtual entity event code for %s" % entity)
                    exec(e.events[event]["code"], {"self": self.hass, "value": value})
            except Exception as ex:
                duration = time.time() - start_time
                self.hass.log("ERROR: Virtual entity %s event execution failed (Duration: %.3fs, Error: %s)" % 
                             (entity, duration, str(ex)))

    def turn_on_virtual(self, entity):
        """Turn on a virtual entity.
        
        Args:
            entity: The entity name (without 'virtual.' prefix)
        """
        if entity in self._virtual_entities:
            self.call_virtual_entity(entity, "switched", True)
            self._virtual_entities[entity].switched = True
            self.hass.log("Virtual entity %s switched to True" % entity)

    def turn_off_virtual(self, entity):
        """Turn off a virtual entity.
        
        Args:
            entity: The entity name (without 'virtual.' prefix)
        """
        if entity in self._virtual_entities:
            self.call_virtual_entity(entity, "switched", False)
            self._virtual_entities[entity].switched = False
            self.hass.log("Virtual entity %s switched to False" % entity)

