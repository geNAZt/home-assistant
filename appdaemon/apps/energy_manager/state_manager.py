"""State management for ensuring entities maintain desired states."""


class StateManager:
    """Manages entity state enforcement."""

    def __init__(self, hass_instance):
        """Initialize state manager.
        
        Args:
            hass_instance: The AppDaemon Hass instance
        """
        self.hass = hass_instance
        self._state_callbacks = {}
        self._state_values = {}

    def ensure_state(self, entity_id, state):
        """Ensure an entity maintains a specific state.
        
        Args:
            entity_id: The entity ID to manage
            state: The desired state value
        """
        try:
            self._state_values[entity_id] = state            
            self._set_state(entity_id, state)
            self.hass.log("Applied state %s to entity %s" % (state, entity_id))
            
            if entity_id not in self._state_callbacks:
                self.hass.log("Setting up state callback for %s" % entity_id)
                self._state_callbacks[entity_id] = self.hass.listen_state(self._ensure_state_callback, entity_id)
                self.hass.log("State callback registered for %s" % entity_id)
        except Exception as ex:
            self.hass.log("ERROR in ensure_state for %s: %s" % (entity_id, str(ex)))

    def _ensure_state_callback(self, entity, attribute, old, new, cb_args):
        """Callback to enforce state when it changes.
        
        Args:
            entity: The entity ID
            attribute: The attribute that changed
            old: Old value
            new: New value
            cb_args: Callback arguments
        """
        try:
            value = self._state_values[entity]
            if new != value:
                self.hass.log("State mismatch detected, correcting %s from %s to %s" % (entity, new, value))
                self._set_state(entity, value)
        except Exception as ex:
            self.hass.log("ERROR in _ensure_state_callback for %s: %s" % (entity, str(ex)))

    def _set_state(self, entity, value):
        """Set the state of an entity.
        
        Args:
            entity: The entity ID
            value: The state value to set
        """
        try:
            if entity.startswith("select."):
                self.hass.log("Setting select option for %s to %s" % (entity, value))
                self.hass.call_service("select/select_option", entity_id=entity, option=value)
                self.hass.log("Select option set successfully")
            else:
                self.hass.log("Setting state for %s to %s" % (entity, value))
                self.hass.set_state(entity, state=value)
                self.hass.log("State set successfully")
        except Exception as ex:
            self.hass.log("ERROR in _set_state for %s: %s" % (entity, str(ex)))

