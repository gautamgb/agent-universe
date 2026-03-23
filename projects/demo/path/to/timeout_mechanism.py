import signal

class TimeoutMechanism:
    def __init__(self, timeout_duration):
        self.timeout_duration = timeout_duration

    def set_timeout(self):
        signal.signal(signal.SIGALRM, self.handle_timeout)
        signal.alarm(self.timeout_duration)

    def handle_timeout(self, signum, frame):
        raise Exception("Test timed out")
