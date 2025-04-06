import appdaemon.plugins.hass.hassapi as hass
import requests
import math

from datetime import datetime
from typing import Optional, Dict, Any, Union, List
from dataclasses import dataclass

@dataclass
class Register:
    id: str

@dataclass
class Meter:
    id: str
    registers: List[Register]

@dataclass
class MeterReadingResponse:
    success: bool
    message: Optional[str] = None

@dataclass
class ApiToken:
    token: str
    refresh_token: Optional[str] = None

class TibberInternalAPIError(Exception):
    """Custom exception for Tibber API errors"""
    pass

class TibberInternalAPI:
    """Client for interacting with Tibber's internal API"""
    
    def __init__(self, token: Optional[str] = None):
        """Initialize the client with an optional access token.
        
        Args:
            token: Optional Tibber API access token
        """
        self.token = token
        self.base_url = "https://app.tibber.com/v4"
        self.headers = {
            "User-Agent": "Tibber/25.12.0",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Accept-Language": "en-US",
            "X-Client-Version": "25.12.0",
            "X-Platform": "android"
        }
        if token:
            self.headers["Authorization"] = f"Bearer {token}"

    def login_with_credentials(self, email: str, password: str) -> ApiToken:
        """Login with email and password credentials."""
        url = f"{self.base_url}/login.credentials"
        data = {
            "email": email,
            "password": password
        }
        
        response = requests.post(
            url,
            data=data,
            headers={
                "User-Agent": "Tibber/25.12.0",
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded"
            }
        )
        
        if response.status_code != 200:
            raise TibberInternalAPIError(f"Login failed: {response.text}")
            
        response_data = response.json()
        return ApiToken(
            token=response_data["token"],
            refresh_token=response_data.get("refreshToken")
        )

    def _execute_query(self, query: str, variables: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Execute a GraphQL query against the Tibber API.
        
        Args:
            query: The GraphQL query string
            variables: Optional variables for the query
            
        Returns:
            The JSON response from the API
            
        Raises:
            TibberInternalAPIError: If the API request fails
        """
        if not self.token:
            raise TibberInternalAPIError("No authentication token available")
            
        response = requests.post(
            f"{self.base_url}/gql",
            headers=self.headers,
            json={
                "query": query,
                "variables": variables or {}
            }
        )
        
        if not response.ok:
            raise TibberInternalAPIError(f"API request failed: {response.status_code} - {response.text}")
            
        data = response.json()
        if "errors" in data:
            raise TibberInternalAPIError(f"GraphQL errors: {data['errors']}")
            
        return data

    def get_meters(self) -> List[Meter]:
        """Get all meters associated with the account.
        
        Returns:
            A list of Meter objects
            
        Raises:
            TibberInternalAPIError: If the API request fails
        """
        query = """
        query GetMeters {
          me {
            meters {
              items {
                meter {
                  id                 
                  registers {
                    id
                  }
                }
              }
            }
          }
        }
        """

        result = self._execute_query(query)
        print(f"Debug - Result: {result}")
        meters_data = result.get("data", {}).get("me", {}).get("meters", {}).get("items", [])
        
        meters = []
        for meter in meters_data:
            meter = meter.get("meter")
            print(f"Debug - Meter: {meter}")
            if meter is not None:
                print(f"Debug - Meter: {meter['id']}")
            
                meters.append(
                    Meter(
                        id=meter["id"],
                        registers=meter["registers"]
                    )
                )
        
        return meters

    def submit_meter_reading(self, meter_id, meter_register_id, reading):
        """Submit a meter reading for a specific meter"""
        mutation = """
        mutation AddMeterReading($meterId: String!, $date: String!, $readings: [AddMeterReading!]!) {
            me {
                addMeterReadings(meterId: $meterId, readingDate: $date, readings: $readings) {
                    success {
                        inputTitle
                        inputValue
                        title
                        descriptionHtml
                        doneButtonText
                    }
                    error {
                        message
                    }
                }
            }
        }
        """
        
        # Format the date as YYYY-MM-DDTHH:mm:ssZ
        date = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        
        # Ensure reading is a float/double
        reading_value = float(reading)
        
        variables = {
            "meterId": meter_id,
            "date": date,
            "readings": [
                {
                    "id": meter_register_id,
                    "value": reading_value  # Ensure value is a float/double
                }
            ]
        }
        
        print(f"Debug - Submitting reading:")
        print(f"  Meter ID: {meter_id}")
        print(f"  Date: {date}")
        print(f"  Reading ID: {meter_register_id}")
        print(f"  Reading Value: {reading_value}")
        
        response = requests.post(
            f"{self.base_url}/gql",
            headers=self.headers,
            json={
                "query": mutation,
                "variables": variables
            }
        )
        
        if response.status_code != 200:
            print(f"Error submitting meter reading: {response.text}")
            return False
            
        data = response.json()
        if "errors" in data:
            print(f"GraphQL errors: {data['errors']}")
            return False
            
        result = data.get("data", {}).get("me", {}).get("addMeterReadings", {})
        if result.get("error"):
            print(f"API error: {result['error']}")
            return False
            
        success = result.get("success")
        if not success:
            print("No success response received")
            return False
            
        print(f"Success: {success}")
        return True

class TibberMeter(hass.Hass):
    def initialize(self):
        runtime = datetime.time(23, 55, 0)
        self.run_daily(self.run_every_c, runtime)

    def run_every_c(self, c):
        # First login with credentials
        client = TibberInternalAPI()
        try:
            # Replace with your email and password
            token = client.login_with_credentials(
                email=self.args["email"],
                password=self.args["password"]
            )

            self.log(f"Successfully logged in. Token: {token.token[:10]}...")
            
            # Update the client with the new token
            client.token = token.token
            client.headers["Authorization"] = f"Bearer {token.token}"
            
            # Get homes
            meters = client.get_meters()

            # Submit meter reading for first home
            if meters:
                meter = meters[0]
                response = client.submit_meter_reading(
                    meter.id,
                    meter.registers[0].get("id"),
                    math.ceil(float(self.get_state("sensor.pv_m1_imported_kwh")))
                )

                self.log(f"\nMeter reading submission: {'Success' if response else 'Failed'}")
                    
        except Exception as e:
            self.log(f"Error: {e}")