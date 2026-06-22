import unittest

from app.calculator import add


class CalculatorTests(unittest.TestCase):
    def test_adds_positive_numbers(self) -> None:
        self.assertEqual(add(2, 3), 5)

    def test_adds_negative_and_positive_numbers(self) -> None:
        self.assertEqual(add(-2, 5), 3)


if __name__ == "__main__":
    unittest.main()
