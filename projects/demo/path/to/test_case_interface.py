from path.to.iteration_counter import IterationCounter
from path.to.timeout_mechanism import TimeoutMechanism

class TestCaseInterface:
    def __init__(self, max_iterations, timeout_duration):
        self.iteration_counter = IterationCounter(max_iterations)
        self.timeout_mechanism = TimeoutMechanism(timeout_duration)

    def run_test(self, test_function):
        try:
            self.timeout_mechanism.set_timeout()
            while True:
                self.iteration_counter.increment()
                test_function()
        except Exception as e:
            print(f"Test terminated: {e}")
