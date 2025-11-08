# App which predicts the energy usage and tries to give recommendations about charging the battery or not

import appdaemon.plugins.hass.hassapi as hass
import threading
import json

from datetime import datetime, timedelta 

class EnergyPredict(hass.Hass):
    def initialize(self):
        # Setup timer
        self.run_every(self.run_every_c, "now", 30)

    def get_response_schema(self) -> dict:
        """
        Returns the JSON schema that the AI should use for its response.
        """
        return {
            "name": "battery_grid_heater_schedule",
            "schema": {
                "type": "object",
                "properties": {
                    "grid_charge_times": {
                        "type": "array",
                        "description": "Time periods when the battery should be charged from the grid.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "start_time": {
                                    "type": "string",
                                    "description": "Start of grid charging period in ISO 8601 time format.",
                                    "pattern": "^([01]\\d|2[0-3]):[0-5]\\d$"
                                },
                                "end_time": {
                                    "type": "string",
                                    "description": "End of grid charging period in ISO 8601 time format.",
                                    "pattern": "^([01]\\d|2[0-3]):[0-5]\\d$"
                                }
                            },
                            "required": [
                                "start_time",
                                "end_time"
                            ],
                            "additionalProperties": False
                        }
                    },
                    "heater_schedules": {
                        "type": "array",
                        "description": "Schedules for each heater including when and for how long they should be on.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "heater_id": {
                                    "type": "string",
                                    "description": "Unique identifier or name for the heater."
                                },
                                "start_time": {
                                    "type": "string",
                                    "description": "Time when this heater should be turned on (ISO 8601 time).",
                                    "pattern": "^([01]\\d|2[0-3]):[0-5]\\d$"
                                },
                                "duration_minutes": {
                                    "type": "integer",
                                    "description": "Duration in minutes for which the heater should be on.",
                                    "minimum": 1
                                }
                            },
                            "required": [
                                "heater_id",
                                "start_time",
                                "duration_minutes"
                            ],
                            "additionalProperties": False
                        }
                    },
                    "total_grid_cost": {
                        "type": "number",
                        "description": "Total cost of purchasing energy from the grid for the schedule.",
                        "minimum": 0
                    }
                },
                "required": [
                    "grid_charge_times",
                    "heater_schedules",
                    "total_grid_cost"
                ],
                "additionalProperties": False
            },
            "strict": True
        }

    def compress_history(self, history: list[dict], entity_id: str) -> tuple[str, str]:
        """
        Compress history data into 15-minute buckets for AI model consumption.
        Returns: (history_string, unit_of_measurement)
        """
        if not history or len(history) == 0:
            return "", ""
        
        first = history[0]
        new_history = [first]

        # Determine unit of measurement
        unit_of_measurement = ""
        if "attributes" in first and "unit_of_measurement" in first["attributes"]:
            unit_of_measurement = first["attributes"]["unit_of_measurement"]
        elif entity_id.startswith("climate."):
            unit_of_measurement = "C"

        # Compress to 15-minute buckets
        for entry in history[1:]:  # Skip first entry as it's already added
            if isinstance(entry["last_changed"], str):
                # Convert string to datetime if needed
                entry_time = datetime.fromisoformat(entry["last_changed"].replace('Z', '+00:00'))
                first_time = first["last_changed"] if isinstance(first["last_changed"], datetime) else datetime.fromisoformat(first["last_changed"].replace('Z', '+00:00'))
            else:
                entry_time = entry["last_changed"]
                first_time = first["last_changed"]
            
            if (entry_time - first_time).total_seconds() > 900:  # 15 minutes in seconds
                first = entry
                new_history.append(first)
        
        # Convert history to string format
        history_parts = []
        for entry in new_history:
            timestamp = entry["last_changed"]
            if isinstance(timestamp, datetime):
                timestamp = timestamp.isoformat()
            
            if unit_of_measurement and unit_of_measurement != "C":
                # Regular sensor with state
                value = entry.get("state", "")
                history_parts.append(f"{timestamp}:{value}")
            elif entity_id.startswith("climate.") and "attributes" in entry:
                # Climate entity with temperature
                if "current_temperature" in entry["attributes"]:
                    value = entry["attributes"]["current_temperature"]
                    history_parts.append(f"{timestamp}:{value}")
        
        return ",".join(history_parts), unit_of_measurement 

    def run_every_c(self, kwargs):
        self.log("=== Energy Predict Run Every C Started (Thread: %s) ===" % threading.current_thread().name)

        try:
            # Get the current time
            now = self.get_now()

            # Build prompt sections
            prompt_sections = []
            
            # Header
            prompt_sections.append("Data Format: ISO timestamp:value pairs separated by commas\n")

            # Entity data
            entities = [
                # Energy sensors
                "sensor.solar_house_consumption_daily",
                "sensor.pv_battery1_state_of_charge",
                "sensor.water_heating_pump_usage",
                "sensor.heating_usage_bad",
                "sensor.heating_usage_buero_fabian",
                "sensor.heating_usage_buero_merja",
                "sensor.heating_usage_flur",
                "sensor.heating_usage_kueche",
                "sensor.heating_usage_schlafzimmer",
                "sensor.heating_usage_speisekammer",
                "sensor.heating_usage_wohnzimmer",
                # Climate entities
                "climate.room_bad",
                "climate.room_buero_fabian",
                "climate.room_buero_merja",
                "climate.room_flur",
                "climate.room_kueche",
                "climate.room_schlafzimmer",
                "climate.room_speisekammer",
                "climate.room_wohnzimmer"
            ]

            prompt_sections.append("=== Historical Data (Last 24 Hours) ===\n")
            for entity in entities:
                try:
                    history_data = self.get_history(
                        entity_id=entity, 
                        start_time=now - timedelta(days=1), 
                        end_time=now
                    )
                    
                    if history_data and len(history_data) > 0 and len(history_data[0]) > 0:
                        daily_history, unit = self.compress_history(history_data[0], entity)
                        if daily_history:
                            prompt_sections.append(f"{entity} ({unit}): {daily_history}\n")
                        else:
                            self.log(f"Warning: No compressed history for {entity}")
                    else:
                        self.log(f"Warning: No history data for {entity}")
                except Exception as ex:
                    self.log(f"Error processing entity {entity}: {ex}")

            # Tibber price forecast
            prompt_sections.append("\n=== Energy Price Forecast (Tibber) ===\n")
            try:
                tibber_ret = self.call_service(
                    "tibber/get_prices", 
                    service_data={
                        "start": now, 
                        "end": now + timedelta(days=1)
                    }
                )
                
                if tibber_ret and "result" in tibber_ret and "response" in tibber_ret["result"]:
                    tibber_resp = tibber_ret["result"]["response"].get("prices", {}).get("Suthfeld ", [])
                    if tibber_resp:
                        price_parts = [f"{item.get('start_time', '')}:{item.get('price', '')}" for item in tibber_resp]
                        prompt_sections.append(f"Price per kWh (EUR): {','.join(price_parts)}\n")
                    else:
                        self.log("Warning: No Tibber price data received")
                else:
                    self.log("Warning: Invalid Tibber response structure")
            except Exception as ex:
                self.log(f"Error getting Tibber prices: {ex}")

            # PV generation forecast
            prompt_sections.append("\n=== PV Generation Forecast (Solcast) ===\n")
            try:
                solcast_ret = self.call_service(
                    "solcast_solar/query_forecast_data", 
                    service_data={
                        "start_date_time": now, 
                        "end_date_time": now + timedelta(days=1)
                    }
                )
                
                if solcast_ret and "result" in solcast_ret and "response" in solcast_ret["result"]:
                    pv_resp = solcast_ret["result"]["response"].get("data", [])
                    if pv_resp:
                        pv_parts = [f"{item.get('period_start', '')}:{item.get('pv_estimate', '')}" for item in pv_resp]
                        prompt_sections.append(f"PV Generation (kWh): {','.join(pv_parts)}\n")
                    else:
                        self.log("Warning: No Solcast PV data received")
                else:
                    self.log("Warning: Invalid Solcast response structure")
            except Exception as ex:
                self.log(f"Error getting Solcast forecast: {ex}")

            # Additional context
            prompt_sections.append("\n=== Additional Context ===\n")
            prompt_sections.append("Heat loss: 1.01 kW/h/k for the whole house\n")
            prompt_sections.append("Usage patterns: We take a bath in the evening, heat pump runs during the night\n")
            
            # Request with JSON Schema
            prompt_sections.append("\n=== Request ===\n")
            prompt_sections.append("Based on the historical data and forecasts above, please provide recommendations:\n")
            prompt_sections.append("1. When should we buy power from the grid?\n")
            prompt_sections.append("2. When should we heat the house?\n")
            prompt_sections.append("\n=== Response Format (JSON Schema) ===\n")
            prompt_sections.append("You MUST respond with a JSON object that strictly follows this schema:\n\n")
            prompt_sections.append(json.dumps(self.get_response_schema(), indent=2))
            prompt_sections.append("\n\nImportant notes:\n")
            prompt_sections.append("- Time formats must be in HH:MM format (24-hour, e.g., '14:30')\n")
            prompt_sections.append("- heater_id should match the room names (e.g., 'bad', 'buero_fabian', 'kueche', etc.)\n")
            prompt_sections.append("- All required fields must be present\n")
            prompt_sections.append("- Return ONLY valid JSON, no additional text or markdown formatting\n")

            # Combine all sections
            prompt = "".join(prompt_sections)
            
            self.log("Prompt generated successfully")
            self.log(f"Prompt length: {len(prompt)} characters")
            self.log(f"\n{'='*80}\n{prompt}\n{'='*80}")

        except Exception as ex:
            self.log(f"Error in run_every_c: {ex}", level="ERROR")
        