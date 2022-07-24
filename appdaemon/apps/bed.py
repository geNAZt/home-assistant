import appdaemon.plugins.hass.hassapi as hass
import datetime

#
# Bed control
#
# This class should control lights for a two sided bed. Following logic should 
# present:
#
# If right side is in bed (binary sensor is occupied) and its later than 11pm
# the light should be dimmed to 10% (if the light was on at this state).
# If left side goes to bed the light should go off after 5 seconds and 
# not turn on again until its not occupied again. 
# If left side stands up and right side is occupied light should go to 10%
# brightness regardless of the time. 
# If right side stands up while the left side is occupied lights should stay off
# If both sides are not occupied the light should be controlled by the luminance sensor
#
# Args:
#

RIGHT_SIDE_FSR = "binary_sensor.bed_right_occupancy"
LEFT_SIDE_FSR = "binary_sensor.bed_left_occupancy"
BED_LIGHT = "light.schlafzimmer"

class Bed(hass.Hass):
    def initialize(self):
        self.log("Bed control loaded...")

        self.not_before = datetime.time(23, 00, 0)
        self.not_after = datetime.time(11, 00, 0)

        self.adapi = self.get_ad_api()

        self.right_side_occupied = self.adapi.get_entity(RIGHT_SIDE_FSR).get_state()
        self.left_side_occupied = self.adapi.get_entity(LEFT_SIDE_FSR).get_state()

        #self.listen_state(self.update_from_event, RIGHT_SIDE_FSR, duration = 5, new = "on")
        #self.listen_state(self.update_from_event, LEFT_SIDE_FSR, duration = 30, new = "on")
        #self.listen_state(self.update_from_event, RIGHT_SIDE_FSR, new = "off")
        #self.listen_state(self.update_from_event, LEFT_SIDE_FSR, duration = 10, new = "off")

        #self.run_every(self.update, "now", 60)

    def update_from_event(self, entity, attribute, old, new, kwargs):
        if entity == RIGHT_SIDE_FSR:
            self.right_side_occupied = new
        if entity == LEFT_SIDE_FSR:
            self.left_side_occupied = new

        self.update([])

    def update(self, kwargs):
        time_now = datetime.datetime.now().time()
        is_sleep_time = True if time_now > self.not_before or time_now < self.not_after else False

        self.log("Bed state, right: %s, left: %s, time to sleep: %s", self.right_side_occupied, self.left_side_occupied, is_sleep_time, level = "INFO")

        if self.left_side_occupied == "on":
            self.log("Merjs is in bed", level = "INFO")
            self.turn_off(BED_LIGHT)
            return

        if self.right_side_occupied == "on" and is_sleep_time == True:
            self.log("Fabian is in bed, Merja isn't", level = "INFO")
            #self.turn_off(BED_LIGHT)
            self.turn_on(BED_LIGHT, brightness = 26)
            return

        self.log("Full brightness", level = "INFO")
        self.turn_on(BED_LIGHT, brightness = 204)

