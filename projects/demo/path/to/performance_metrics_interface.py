from path.to.dynamic_adjustment_service import DynamicAdjustmentService

class PerformanceMetricsInterface:
    def __init__(self):
        self.dynamic_adjustment_service = DynamicAdjustmentService()

    def collect_metric(self, metric):
        self.dynamic_adjustment_service.collect_metrics(metric)

    def process_metrics(self):
        self.dynamic_adjustment_service.process_metrics()

    def adjust_limits(self):
        self.dynamic_adjustment_service.adjust_limits()
