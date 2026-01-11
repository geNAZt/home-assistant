# set_state.py
# Ermöglicht das Setzen von Zuständen und Attributen jeder Entität

entity_id = data.get("entity_id")
if not entity_id:
    logger.error("Keine entity_id angegeben")
else:
    # Aktuellen Zustand abrufen
    state = hass.states.get(entity_id)
    
    # Neuen Zustand aus den Daten holen oder alten behalten
    new_state = data.get("state")
    # Neue Attribute holen oder alte behalten
    new_attributes = state.attributes.copy()
    if "attributes" in data:
        new_attributes.update(data.get("attributes"))
    
    # Zustand im System setzen
    hass.states.set(entity_id, new_state, new_attributes)
    logger.info(f"Zustand von {entity_id} auf {new_state} gesetzt.")