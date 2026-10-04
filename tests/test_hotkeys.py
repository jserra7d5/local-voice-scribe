"""Config combos become the portal's preferred_trigger strings (xkb key names)."""

import unittest

from linux.hotkeys import _portal_trigger


class PortalTriggerTests(unittest.TestCase):
    def test_modifiers_map_to_portal_names(self):
        self.assertEqual(_portal_trigger("super+alt+r"), "LOGO+ALT+r")
        self.assertEqual(_portal_trigger("<ctrl>+<shift>+s"), "CTRL+SHIFT+s")

    def test_function_keys_keep_xkb_case(self):
        self.assertEqual(_portal_trigger("alt+f8"), "ALT+F8")
        self.assertEqual(_portal_trigger("ctrl+space"), "CTRL+space")


if __name__ == "__main__":
    unittest.main()
