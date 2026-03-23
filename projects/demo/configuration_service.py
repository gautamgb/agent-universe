class ConfigurationService:
    def __init__(self):
        self.config = {
            "test_case_1": {"max_iterations": 100, "max_execution_time": 5},
            "test_case_2": {"max_iterations": 200, "max_execution_time": 10}
        }

    def get_max_iterations(self, test_name):
        return self.config.get(test_name, {}).get("max_iterations", 100)

    def get_max_execution_time(self, test_name):
        return self.config.get(test_name, {}).get("max_execution_time", 5)
