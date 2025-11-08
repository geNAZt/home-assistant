"""Phase control management for electrical phase balancing."""

from .models import EnergyConsumer


class PhaseControl:
    """Manages electrical phase control to prevent breaker trips."""

    def __init__(self, hass_instance):
        """Initialize phase control.
        
        Args:
            hass_instance: The AppDaemon Hass instance
        """
        self.hass = hass_instance
        self._phase_control = {}

    def check_phase(self, ec: EnergyConsumer):
        """Check if a consumer can be added to a phase without tripping breakers.
        
        Args:
            ec: The energy consumer to check
            
        Returns:
            bool: True if the phase can accommodate the consumer, False otherwise
        """
        try:
            if ec.group in self._phase_control:
                self.hass.log("Group %s found in phase control" % ec.group)
                phases = self._phase_control[ec.group]
                if ec.phase in phases:
                    self.hass.log("Phase %s found for group %s" % (ec.phase, ec.group))
                    entities = phases[ec.phase]
                    v = float(0)
                    for skey, value in entities.items(): 
                        if skey != ec.name:
                            v += value
                            self.hass.log("Added %s (%.2f) to phase usage, total so far: %.2f" % (skey, value, v))

                    total_usage = v + ec.current
                    self.hass.log("Total phase usage would be: %.2f (existing: %.2f + new: %.2f)" % 
                                 (total_usage, v, ec.current))
                    
                    if total_usage > 15500:
                        self.hass.log("    > %s wanted to use phase %s in group %s but not enough capacity (%.2f > 15500)" % 
                                     (ec.name, ec.phase, ec.group, total_usage))
                        return False
                    else:
                        self.hass.log("Phase check passed for %s/%s (%.2f <= 15500)" % (ec.name, ec.group, total_usage))
                else:
                    self.hass.log("Phase %s not found for group %s, allowing" % (ec.phase, ec.group))
            else:
                self.hass.log("Group %s not found in phase control, allowing" % ec.group)

            return True
        except Exception as ex:
            self.hass.log("ERROR in _check_phase for %s/%s: %s" % (ec.name, ec.group, str(ex)))
            return False

    def add_phase(self, ec: EnergyConsumer):
        """Add a consumer to phase control.
        
        Args:
            ec: The energy consumer to add
            
        Returns:
            bool: True if successful, False otherwise
        """
        try:
            if ec.group in self._phase_control:
                self.hass.log("Group %s already exists in phase control" % ec.group)
                phases = self._phase_control[ec.group]
                if ec.phase in phases:
                    self.hass.log("Phase %s already exists for group %s" % (ec.phase, ec.group))
                    entities = phases[ec.phase]
                    entities[ec.name] = ec.current
                    self.hass.log("Added %s to existing phase %s with current %.2f" % (ec.name, ec.phase, ec.current))
                else:
                    phases[ec.phase] = {ec.name: ec.current}
                    self.hass.log("Created new phase %s for group %s with %s (%.2f)" % 
                                 (ec.phase, ec.group, ec.name, ec.current))
            else:
                self._phase_control[ec.group] = {ec.phase: {ec.name: ec.current}}
                self.hass.log("Created new group %s with phase %s and %s (%.2f)" % 
                             (ec.group, ec.phase, ec.name, ec.current))

            self.hass.log("Phase control state: %s" % self._phase_control)
            return True
        except Exception as ex:
            self.hass.log("ERROR in _add_phase for %s/%s: %s" % (ec.name, ec.group, str(ex)))
            return False

    def remove_phase(self, ec: EnergyConsumer):
        """Remove a consumer from phase control.
        
        Args:
            ec: The energy consumer to remove
        """
        try:
            if ec.group not in self._phase_control:
                self.hass.log("WARNING: Group %s not found in phase control" % ec.group)
                return
                
            phases = self._phase_control[ec.group]
            if ec.phase not in phases:
                self.hass.log("WARNING: Phase %s not found for group %s" % (ec.phase, ec.group))
                return
                
            entities = phases[ec.phase]
            if ec.name not in entities:
                self.hass.log("WARNING: Entity %s not found in phase %s for group %s" % 
                             (ec.name, ec.phase, ec.group))
                return
                
            del entities[ec.name]
            self.hass.log("Removed %s from phase %s in group %s" % (ec.name, ec.phase, ec.group))
            
            # Clean up empty phases and groups
            if not entities:
                del phases[ec.phase]
                self.hass.log("Removed empty phase %s from group %s" % (ec.phase, ec.group))
                if not phases:
                    del self._phase_control[ec.group]
                    self.hass.log("Removed empty group %s from phase control" % ec.group)
                    
        except Exception as ex:
            self.hass.log("ERROR in _remove_phase for %s/%s: %s" % (ec.name, ec.group, str(ex)))

