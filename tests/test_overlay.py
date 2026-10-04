"""Render the status border without relying on window-system masking.

Run with the installed Scribe interpreter:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_overlay
"""

import unittest

from PyQt6.QtWidgets import QApplication

from linux.overlay import BORDER_THICKNESS, BorderOverlay


class UnmaskedOverlay(BorderOverlay):
    """Model a compositor that does not visually clip a window's mask."""

    def setMask(self, region):
        pass


class BorderRenderingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.overlay = UnmaskedOverlay({})

    def tearDown(self):
        self.overlay._clear()
        self.overlay.close()

    def assert_border(self, color, alpha):
        image = self.overlay.grab().toImage()
        t = BORDER_THICKNESS
        for y in range(image.height()):
            for x in range(image.width()):
                pixel = image.pixelColor(x, y)
                on_border = (x < t or y < t or
                             x >= image.width() - t or y >= image.height() - t)
                self.assertEqual(pixel.alpha(), alpha if on_border else 0, (x, y))
                if on_border:
                    for actual, expected in zip(pixel.getRgb()[:3], color.getRgb()[:3]):
                        self.assertLessEqual(abs(actual - expected), 1)

    def test_each_status_paints_only_six_pixel_border(self):
        for state in ("recording", "transcribing", "complete"):
            with self.subTest(state=state):
                self.overlay.set_state(state)
                self.overlay.resize(96, 72)
                self.assert_border(self.overlay._colors[state], 230)

    def test_repeated_fades_leave_center_transparent_and_reduce_alpha(self):
        self.overlay.set_state("transcribing")
        self.overlay.resize(96, 72)
        self.assert_border(self.overlay._colors["transcribing"], 230)
        for step in range(1, 4):
            self.overlay._fade_tick()
            self.assert_border(self.overlay._colors["transcribing"], int(230 * (1 - step / 11)))


if __name__ == "__main__":
    unittest.main()
