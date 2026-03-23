class DynamicAdjustmentService:
    def __init__(self):
        self.metrics = {}

    def collect_metrics(self, test_name, iteration_count, elapsed_time):
        if test_name not in self.metrics:
            self.metrics[test_name] = []

        self.metrics[test_name].append((iteration_count, elapsed_time))
        self.adjust_max_iterations(test_name)

    def adjust_max_iterations(self, test_name):
        # Simple logic to adjust max iterations based on collected metrics
        if len(self.metrics[test_name]) > 5:
            avg_iterations = sum([m[0] for m in self.metrics[test_name]]) / len(self.metrics[test_name])
            avg_time = sum([m[1] for m in self.metrics[test_name]]) / len(self.metrics[test_name])

            print(f"Adjusting max iterations for {test_name}: Avg Iterations={avg_iterations}, Avg Time={avg_time}")
            # Update configuration service with new limits
