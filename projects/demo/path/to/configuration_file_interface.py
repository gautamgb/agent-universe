from path.to.configuration_service import ConfigurationService

class ConfigurationFileInterface:
    def __init__(self, config_path):
        self.configuration_service = ConfigurationService(config_path)

    def get_max_iterations(self):
        config = self.configuration_service.load_config()
        return config.get('max_iterations', 100)  # Default to 100 if not specified

    def set_max_iterations(self, max_iterations):
        config = self.configuration_service.load_config()
        config['max_iterations'] = max_iterations
        self.configuration_service.save_config(config)
