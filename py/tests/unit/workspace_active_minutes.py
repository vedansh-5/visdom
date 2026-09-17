#!/usr/bin/env python3

# Copyright 2017-present, The Visdom Authors
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

"""Counting the minutes a workspace actually had work in them.

Time connected is not time worked. The unit is a minute with at least one write
in it, held as a bitmask so instances serving the same workspace can be
combined without counting a minute more than once.
"""

import tempfile
import unittest

import pytest

from visdom.server.app import Application

pytestmark = pytest.mark.unit

HOUR = 3600


def bits(mask):
    return bin(mask).count("1")


class TestActiveMinutes(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.app = Application(port=8099, env_path=self._tmp.name)

    def space(self, workspace_id="ws-a"):
        return self.app.workspace_env_manager.space(workspace_id, slug=workspace_id)

    def test_a_fresh_workspace_has_no_active_minutes(self):
        space = self.space()
        self.assertIsNone(space.active_hour)
        self.assertEqual(space.active_minutes_mask, 0)

    def test_one_write_marks_one_minute(self):
        space = self.space()
        space.mark_active_minute(HOUR * 100 + 61)
        self.assertEqual(bits(space.active_minutes_mask), 1)

    def test_two_writes_in_the_same_minute_are_one_minute(self):
        space = self.space()
        space.mark_active_minute(HOUR * 100 + 61)
        space.mark_active_minute(HOUR * 100 + 66)
        self.assertEqual(bits(space.active_minutes_mask), 1)

    def test_writes_a_minute_apart_are_separate_minutes(self):
        space = self.space()
        for offset in (0, 60, 120):
            space.mark_active_minute(HOUR * 100 + offset)
        self.assertEqual(bits(space.active_minutes_mask), 3)

    def test_an_hour_of_work_is_sixty_minutes_and_no_more(self):
        space = self.space()
        for minute in range(60):
            space.mark_active_minute(HOUR * 100 + minute * 60)
        self.assertEqual(bits(space.active_minutes_mask), 60)
        self.assertLess(space.active_minutes_mask, 1 << 60)

    def test_a_new_hour_starts_a_new_mask(self):
        space = self.space()
        space.mark_active_minute(HOUR * 100)
        first = space.active_hour
        space.mark_active_minute(HOUR * 101)
        self.assertNotEqual(space.active_hour, first)
        self.assertEqual(bits(space.active_minutes_mask), 1)

    def test_the_same_minute_on_two_instances_is_one_minute(self):
        """A union rather than a sum: three instances serving one workspace in
        one minute did one minute of work between them."""
        one, two = self.space("ws-a"), self.space("ws-b")
        one.mark_active_minute(HOUR * 100)
        two.mark_active_minute(HOUR * 100)
        two.mark_active_minute(HOUR * 100 + 60)

        union = one.active_minutes_mask | two.active_minutes_mask
        self.assertEqual(bits(union), 2)

    def test_a_write_records_its_minute(self):
        space = self.space()
        space.mark_dirty("main")
        self.assertIsNotNone(space.active_hour)
        self.assertEqual(bits(space.active_minutes_mask), 1)

    def test_the_reported_payload_carries_the_hour_and_the_mask(self):
        space = self.space()
        space.mark_dirty("main")
        payload = space.activity()
        self.assertEqual(payload["active_hour"], space.active_hour)
        self.assertEqual(payload["active_minutes_mask"], space.active_minutes_mask)


if __name__ == "__main__":
    unittest.main()
