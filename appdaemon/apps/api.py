import hassapi as hass

class API(hass.Hass):

    def initialize(self):
        self.register_endpoint(self.my_callback, "api")

    async def my_callback(self, request, kwargs):
        data = await request.json()
        self.log(data)
        response = {"message": "Hello World"}
        return response, 200
