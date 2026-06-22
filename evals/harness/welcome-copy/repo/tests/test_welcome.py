import unittest

from notifications.formatters import welcome_subject
from notifications.messages import welcome_message


class WelcomeCopyTests(unittest.TestCase):
    def test_subject_normalizes_display_name(self) -> None:
        self.assertEqual(welcome_subject("  ada lovelace "), "Welcome, Ada Lovelace")

    def test_message_normalizes_display_name(self) -> None:
        self.assertEqual(
            welcome_message("  ada lovelace "),
            "Hello Ada Lovelace, thanks for joining.",
        )


if __name__ == "__main__":
    unittest.main()
