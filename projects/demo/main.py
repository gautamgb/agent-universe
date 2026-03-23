from test_case import TestCase

if __name__ == "__main__":
    test_cases = [
        TestCase("test_case_1"),
        TestCase("test_case_2")
    ]

    for test in test_cases:
        test.run()
