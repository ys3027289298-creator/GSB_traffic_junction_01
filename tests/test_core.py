"""Regression tests for the river lock game core.

Covers: chamber occupancy, door/water-level boundary, pump energy,
fleet length limit, fault-paused clock, cancel releasing queue numbers,
event replay determinism, and ship order after JSON load. Plus illegal
commands, empty data, capacity boundary, and duplicate input.
"""

import json
import unittest

from river.core import CommandError, FaultError, LockGame


class LockGameTestCase(unittest.TestCase):
    def make_game(self, **kwargs):
        return LockGame(**kwargs)

    def fill_to_upstream(self, game):
        while game.chamber_level < game.upstream_level - 1e-9:
            game.pump("up")


class ChamberOccupancyTest(LockGameTestCase):
    def test_fleet_cannot_occupy_chamber_twice(self):
        game = self.make_game()
        game.enqueue("Alpha", 40)
        game.enqueue("Beta", 30)
        game.open_door("downstream")
        game.enter([1, 2])
        with self.assertRaises(CommandError):
            game.enter([1, 2])
        self.assertEqual([s.name for s in game.chamber], ["Alpha", "Beta"])

    def test_single_ship_cannot_enter_twice(self):
        game = self.make_game()
        game.enqueue("Alpha", 40)
        game.open_door("downstream")
        game.enter([1])
        with self.assertRaises(CommandError):
            game.enter(1)
        self.assertEqual(len(game.chamber), 1)


class DoorWaterLevelTest(LockGameTestCase):
    def test_reverse_gradient_door_opening_rejected(self):
        game = self.make_game()
        # Chamber slightly *below* downstream: reverse gradient.
        game.chamber_level = game.downstream_level - 1e-6
        with self.assertRaises(CommandError):
            game.open_door("downstream")
        self.assertFalse(game.doors["downstream"])

    def test_level_difference_boundary(self):
        game = self.make_game()
        # Exactly equal levels: door opens.
        game.chamber_level = game.downstream_level
        game.open_door("downstream")
        self.assertTrue(game.doors["downstream"])
        # Within tolerance: door opens.
        game2 = self.make_game()
        game2.chamber_level = game2.downstream_level + 1e-12
        game2.open_door("downstream")
        self.assertTrue(game2.doors["downstream"])
        # Beyond tolerance in either direction: rejected.
        game3 = self.make_game()
        game3.chamber_level = game3.downstream_level + 0.5
        with self.assertRaises(CommandError):
            game3.open_door("downstream")
        with self.assertRaises(CommandError):
            game3.open_door("upstream")


class PumpEnergyTest(LockGameTestCase):
    def test_zero_energy_pump_rejected(self):
        game = self.make_game(pump_energy=0)
        with self.assertRaises(CommandError):
            game.pump("up")
        self.assertEqual(game.chamber_level, game.downstream_level)

    def test_energy_consumed_per_pump(self):
        game = self.make_game(pump_energy=2)
        game.pump("up")
        game.pump("up")
        self.assertEqual(game.pump_energy, 0)
        with self.assertRaises(CommandError):
            game.pump("up")

    def test_pump_clamps_at_boundaries(self):
        game = self.make_game(pump_energy=100, pump_step=3.0)
        self.fill_to_upstream(game)
        self.assertEqual(game.chamber_level, game.upstream_level)
        with self.assertRaises(CommandError):
            game.pump("up")


class FleetLengthTest(LockGameTestCase):
    def test_overlength_fleet_rejected(self):
        game = self.make_game(chamber_length=100)
        game.enqueue("Long1", 60)
        game.enqueue("Long2", 50)
        game.open_door("downstream")
        with self.assertRaises(CommandError):
            game.enter([1, 2])
        self.assertEqual(len(game.chamber), 0)
        self.assertEqual(len(game.queue), 2)

    def test_exact_length_fleet_allowed(self):
        game = self.make_game(chamber_length=100)
        game.enqueue("Fit1", 60)
        game.enqueue("Fit2", 40)
        game.open_door("downstream")
        game.enter([1, 2])
        self.assertEqual(len(game.chamber), 2)


class FaultPauseTest(LockGameTestCase):
    def test_fault_pauses_freeze_clock(self):
        game = self.make_game()
        game.tick(5)
        game.trigger_fault("gate jam")
        frozen = game.time
        game.tick(3)
        game.tick(10)
        self.assertEqual(game.time, frozen)
        game.resume()
        game.tick(2)
        self.assertEqual(game.time, frozen + 2)

    def test_manual_pause_freezes_clock(self):
        game = self.make_game()
        game.pause()
        game.tick(4)
        self.assertEqual(game.time, 0)

    def test_operations_blocked_during_fault(self):
        game = self.make_game()
        game.trigger_fault("pump failure")
        with self.assertRaises(FaultError):
            game.enqueue("Alpha", 10)
        with self.assertRaises(FaultError):
            game.pump("up")


class CancelQueueTest(LockGameTestCase):
    def test_cancel_releases_queue_number(self):
        game = self.make_game()
        t1 = game.enqueue("Alpha", 10)
        t2 = game.enqueue("Beta", 20)
        game.cancel(t1)
        self.assertEqual(len(game.queue), 1)
        t3 = game.enqueue("Gamma", 30)
        self.assertEqual(t3, t1)  # released number is reused
        self.assertEqual([e["ticket"] for e in game.queue], [t2, t1])

    def test_cancel_unknown_ticket_rejected(self):
        game = self.make_game()
        with self.assertRaises(CommandError):
            game.cancel(99)

    def test_double_cancel_rejected(self):
        game = self.make_game()
        t1 = game.enqueue("Alpha", 10)
        game.cancel(t1)
        with self.assertRaises(CommandError):
            game.cancel(t1)


class EventReplayTest(LockGameTestCase):
    def build_scripted_game(self):
        game = self.make_game()
        game.enqueue("Alpha", 40)
        game.enqueue("Beta", 30)
        game.open_door("downstream")
        game.enter([1, 2])
        game.close_door("downstream")
        game.pump("up")
        game.pump("up")
        game.tick(3)
        return game

    def test_replay_reproduces_water_level(self):
        original = self.build_scripted_game()
        clone = LockGame.replay(original.events)
        self.assertEqual(clone.chamber_level, original.chamber_level)
        self.assertEqual(clone.time, original.time)
        self.assertEqual(clone.status()["chamber"], original.status()["chamber"])

    def test_reapplying_event_does_not_change_water_level(self):
        game = self.make_game()
        game.pump("up")
        level = game.chamber_level
        pump_event = game.events[-1]
        game.apply_event(pump_event)  # duplicate replay must be a no-op
        self.assertEqual(game.chamber_level, level)
        self.assertEqual(game.pump_energy, 5)

    def test_replay_twice_is_idempotent(self):
        original = self.build_scripted_game()
        clone = LockGame.replay(original.events)
        for event in original.events:
            clone.apply_event(event)
        self.assertEqual(clone.chamber_level, original.chamber_level)
        self.assertEqual(clone.time, original.time)


class SaveLoadTest(LockGameTestCase):
    def test_load_preserves_ship_order(self):
        game = self.make_game()
        for name in ("Alpha", "Beta", "Gamma"):
            game.enqueue(name, 10)
        game.open_door("downstream")
        game.enter([1, 2])
        restored = LockGame.load(game.save())
        self.assertEqual([s.name for s in restored.chamber], ["Alpha", "Beta"])
        self.assertEqual([e["ship"].name for e in restored.queue], ["Gamma"])
        self.assertEqual([e["ticket"] for e in restored.queue], [3])

    def test_load_preserves_clock_and_levels(self):
        game = self.make_game()
        game.pump("up")
        game.tick(7)
        restored = LockGame.load(game.save())
        self.assertEqual(restored.time, 7)
        self.assertEqual(restored.chamber_level, game.chamber_level)
        self.assertEqual(restored.pump_energy, game.pump_energy)

    def test_save_is_valid_json(self):
        game = self.make_game()
        game.enqueue("Alpha", 10)
        data = json.loads(game.save())
        self.assertEqual(data["version"], 1)

    def test_load_empty_data_rejected(self):
        for bad in ("", "   ", None, {}, [], 0):
            with self.assertRaises(CommandError, msg=repr(bad)):
                LockGame.load(bad)

    def test_load_malformed_data_rejected(self):
        with self.assertRaises(CommandError):
            LockGame.load("{not json")
        with self.assertRaises(CommandError):
            LockGame.load('{"foo": 1}')


class CommandInterfaceTest(LockGameTestCase):
    def test_illegal_commands_rejected(self):
        game = self.make_game()
        for bad in ("", "   ", "fly to the moon", "open sideways",
                    "pump", "pump sideways", "enqueue Solo",
                    "enqueue Bad notanumber", "cancel", "enter",
                    "tick 1 2", "close nowhere", "status extra"):
            with self.assertRaises(CommandError, msg=repr(bad)):
                game.execute(bad)

    def test_cmd_round_trip(self):
        game = self.make_game()
        ticket = game.execute("enqueue Alpha 40")
        self.assertEqual(ticket, 1)
        game.execute("open downstream")
        game.execute("enter 1")
        self.assertEqual(game.status()["chamber"], ["Alpha"])
        game.execute("close downstream")
        game.execute("pump up")
        self.assertGreater(game.chamber_level, 0)
        game.execute("tick 5")
        self.assertEqual(game.time, 5)
        blob = game.execute("save")
        self.assertEqual(LockGame.load(blob).time, 5)

    def test_duplicate_input_rejected(self):
        game = self.make_game()
        game.enqueue("Alpha", 10)
        with self.assertRaises(CommandError):
            game.enqueue("Alpha", 20)  # duplicate ship name
        game.enqueue("Beta", 20)
        game.open_door("downstream")
        with self.assertRaises(CommandError):
            game.enter([1, 1])  # duplicate tickets in one fleet
        with self.assertRaises(CommandError):
            game.execute("enter 2 2")


class CapacityBoundaryTest(LockGameTestCase):
    def test_capacity_boundary(self):
        game = self.make_game(chamber_capacity=2, chamber_length=100)
        game.enqueue("S1", 10)
        game.enqueue("S2", 10)
        game.enqueue("S3", 10)
        game.open_door("downstream")
        game.enter([1, 2])  # exactly at capacity: allowed
        self.assertEqual(len(game.chamber), 2)
        with self.assertRaises(CommandError):
            game.enter([3])  # one beyond capacity: rejected
        self.assertEqual(len(game.chamber), 2)
        self.assertEqual(len(game.queue), 1)

    def test_chamber_full_and_empty_edges(self):
        game = self.make_game(chamber_capacity=1)
        with self.assertRaises(CommandError):
            game.exit_chamber()  # empty chamber
        game.enqueue("Solo", 10)
        with self.assertRaises(CommandError):
            game.enter([1])  # no door open
        game.open_door("downstream")
        game.enter([1])
        game.close_door("downstream")
        with self.assertRaises(CommandError):
            game.exit_chamber()  # no door open


if __name__ == "__main__":
    unittest.main()
