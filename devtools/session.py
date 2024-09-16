class Session:
    def __init__(self, browser, session_id):
        if not isinstance(session_id, str):
            raise TypeError("session_id must be a string")
        # Resources
        self.browser = browser

        # State
        self.session_id = session_id
        self.message_id = 0
        self.subscriptions = {}

    def send_command(self, command, params=None):
        current_id = self.message_id
        self.message_id += 1
        json_command = {
            "id": current_id,
            "method": command,
        }

        if self.session_id:
            json_command["sessionId"] = self.session_id
        if params:
            json_command["params"] = params

        return self.browser.write_json(json_command)

    def subscribe(self, string, callback, repeating):
        if string in self.subscriptions:
            raise ValueError("You are already subscribed to this string, duplicate subscriptions are not allowed.")
        else:
            self.subscriptions[string] = (callback, repeating)

    def unsubscribe(self, string):
        if string not in self.subscriptions:
            raise ValueError("Cannot unsubscribe as string is not present in subscriptions")
        del self.subscriptions[string]
