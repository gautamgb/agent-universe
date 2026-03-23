import json

class ConfigurationService:
    def __init__(self, config_path):
        self.config_path = config_path

    def load_config(self):
        with open(self.config_path, 'r') as file:
            return json.load(file)

    def save_config(self, config):
        with open(self.config_path, 'w') as file:
            json.dump(config, file)
