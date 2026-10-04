"""Render the status border without relying on window-system masking.

Run with the installed Scribe interpreter:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_overlay
"""

import unittest

from PyQt6.QtWidgets import QApplication

from linux.overlay import BORDER_THICKNESS, RIPPLE_GLOW, RIPPLE_MAX_DEPTH, BorderOverlay


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

    def test_flash_states_paint_only_six_pixel_border(self):
        for state in ("transcribing", "complete"):
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


class RippleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.level = 0.0
        self.overlay = UnmaskedOverlay({}, lambda: self.level)
        self.overlay.set_state("recording")
        self.overlay.resize(400, 300)

    def tearDown(self):
        self.overlay._clear()
        self.overlay.close()

    def depth_at(self, x):
        """How far paint reaches down from the top edge at column x."""
        image = self.overlay.grab().toImage()
        return max((y for y in range(RIPPLE_MAX_DEPTH + RIPPLE_GLOW[0][0] + 4)
                    if image.pixelColor(x, y).alpha() > 0), default=-1) + 1

    def alpha_at_inset(self, inset):
        return self.overlay.grab().toImage().pixelColor(200, inset).alpha()

    def test_silence_lies_flat_on_the_edge(self):
        for _ in range(20):
            self.overlay._ripple_tick()
        depths = {self.depth_at(x) for x in range(40, 360, 7)}
        self.assertEqual(len(depths), 1)
        self.assertLessEqual(depths.pop(), RIPPLE_GLOW[0][0] // 2 + 1)

    def test_sound_swings_a_standing_wave_on_each_edge(self):
        self.level = 1.0
        frames = []
        for _ in range(12):
            self.overlay._ripple_tick()
            frames.append([self.depth_at(x) for x in range(110, 290, 10)])
        deepest = max(max(f) for f in frames)
        self.assertGreater(deepest, RIPPLE_MAX_DEPTH // 2)
        self.assertLessEqual(deepest, RIPPLE_MAX_DEPTH + RIPPLE_GLOW[0][0] // 2 + 1)
        self.assertGreater(len({tuple(f) for f in frames}), 6)  # it moves while you talk

    def test_leaving_recording_clears_ripples(self):
        self.level = 1.0
        self.overlay._ripple_tick()
        self.overlay.set_state("transcribing")
        self.assertEqual(self.alpha_at_inset(BORDER_THICKNESS + 1), 0)
        self.assertFalse(self.overlay._ripple_timer.isActive())


if __name__ == "__main__":
    unittest.main()
