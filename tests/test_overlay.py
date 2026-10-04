"""Render the overlay's state effects: recording neon, transcribing shimmer, complete bloom.

Run with the installed Scribe interpreter:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_overlay
"""

import unittest

from PyQt6.QtWidgets import QApplication

from linux.overlay import ANIM_TICK_MS, BLOOM_DEPTH, BLOOM_KEYS, RIPPLE_GLOW, RIPPLE_MAX_DEPTH, SHIMMER_PERIOD, SHIMMER_SPEED, BorderOverlay


class BloomTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.overlay = BorderOverlay({})
        self.overlay.set_state("complete")
        self.overlay.resize(400, 300)

    def tearDown(self):
        self.overlay._clear()
        self.overlay.close()

    def advance_to(self, seconds):
        while self.overlay._tick * ANIM_TICK_MS / 1000 < seconds and self.overlay._anim_timer.isActive():
            self.overlay._anim_tick()

    def alpha(self, x, y):
        return self.overlay.grab().toImage().pixelColor(x, y).alpha()

    def test_glows_on_every_edge_and_leaves_centre_clear(self):
        self.advance_to(0.1)
        for x, y in ((200, 1), (200, 298), (1, 150), (398, 150)):
            self.assertGreater(self.alpha(x, y), 200, (x, y))
        self.assertGreater(self.alpha(200, BLOOM_DEPTH // 2), 0)
        self.assertEqual(self.alpha(200, 150), 0)

    def test_pulses_twice_then_hides(self):
        peaks = []
        for t in (0.1, 0.3, 0.45):
            self.advance_to(t)
            peaks.append(self.alpha(200, 1))
        first, trough, second = peaks
        self.assertLess(trough, first // 2)
        self.assertGreater(second, first * 0.8)
        self.advance_to(BLOOM_KEYS[-1][0] + 0.1)
        self.assertFalse(self.overlay.isVisible())
        self.assertFalse(self.overlay._anim_timer.isActive())


class RippleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.level = 0.0
        self.overlay = BorderOverlay({}, lambda: self.level)
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
            self.overlay._anim_tick()
        depths = {self.depth_at(x) for x in range(40, 360, 7)}
        self.assertEqual(len(depths), 1)
        self.assertLessEqual(depths.pop(), RIPPLE_GLOW[0][0] // 2 + 1)

    def test_sound_swings_a_standing_wave_on_each_edge(self):
        self.level = 1.0
        frames = []
        for _ in range(12):
            self.overlay._anim_tick()
            frames.append([self.depth_at(x) for x in range(110, 290, 10)])
        deepest = max(max(f) for f in frames)
        self.assertGreater(deepest, RIPPLE_MAX_DEPTH // 2)
        self.assertLessEqual(deepest, RIPPLE_MAX_DEPTH + RIPPLE_GLOW[0][0] // 2 + 1)
        self.assertGreater(len({tuple(f) for f in frames}), 6)  # it moves while you talk

    def test_leaving_recording_clears_ripples(self):
        self.level = 1.0
        self.overlay._anim_tick()
        self.overlay.set_state("idle")
        self.assertFalse(self.overlay.isVisible())
        self.assertFalse(self.overlay._anim_timer.isActive())


class ShimmerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.overlay = BorderOverlay({})
        self.overlay.set_state("transcribing")
        self.overlay.resize(2000, 300)

    def tearDown(self):
        self.overlay._clear()
        self.overlay.close()

    def brightest_x(self, y):
        image = self.overlay.grab().toImage()
        return max(range(0, 2000, 5), key=lambda x: image.pixelColor(x, y).alpha())

    def test_lights_top_and_bottom_only(self):
        image = self.overlay.grab().toImage()
        self.assertGreater(max(image.pixelColor(x, 1).alpha() for x in range(0, 2000, 5)), 200)
        self.assertGreater(max(image.pixelColor(x, 298).alpha() for x in range(0, 2000, 5)), 200)
        self.assertEqual(image.pixelColor(1, 150).alpha(), 0)
        self.assertEqual(image.pixelColor(1998, 150).alpha(), 0)

    def test_light_moves_left_to_right_and_persists(self):
        before = self.brightest_x(1)
        for _ in range(5):
            self.overlay._anim_tick()
        after = self.brightest_x(1)
        self.assertEqual((after - before) % SHIMMER_PERIOD, 5 * SHIMMER_SPEED % SHIMMER_PERIOD)
        self.assertTrue(self.overlay._anim_timer.isActive())


if __name__ == "__main__":
    unittest.main()
