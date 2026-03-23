class IterationCounter:
    def __init__(self, max_iterations):
        self.max_iterations = max_iterations
        self.current_iteration = 0

    def increment(self):
        self.current_iteration += 1
        if self.current_iteration >= self.max_iterations:
            print(f"Test terminated due to reaching max iterations: {self.max_iterations}")
            return True
        return False

    def get_count(self):
        return self.current_iteration
