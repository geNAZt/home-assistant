# Energy Manager Module

This module has been refactored from a single large file into multiple focused modules for better maintainability and understanding.

## Module Structure

### Core Modules

- **`energy_manager.py`** - Main orchestrator class that coordinates all components
- **`models.py`** - Data classes (AdditionalConsumer, VirtualEntity, EnergyConsumer)
- **`constants.py`** - Shared constants (MIN_BATTERY_CHARGE)

### Component Modules

- **`virtual_entities.py`** - Manages virtual entities and their event handling
- **`phase_control.py`** - Handles electrical phase balancing to prevent breaker trips
- **`battery.py`** - Battery state and capacity management
- **`consumption.py`** - Energy consumption tracking and control logic
- **`ac_charging.py`** - AC charging logic for battery pre-charging
- **`state_manager.py`** - Entity state enforcement
- **`power_monitoring.py`** - Solar panel production and exported power monitoring

## Usage

The module is configured in `apps.yaml`:

```yaml
energy_manager:
  module: energy_manager.energy_manager
  class: EnergyManager
```

Other apps can access the energy manager using:

```python
energy_manager = self.get_app("energy_manager")
```

## Migration Notes

The original `energy_manager.py` file has been preserved in the parent directory for reference. Once you've verified that the refactored version works correctly, you can safely remove the old file.

All functionality has been preserved - the refactoring only reorganizes the code into logical modules without changing behavior.

