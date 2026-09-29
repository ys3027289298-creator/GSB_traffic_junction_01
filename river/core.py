"""River lock game core logic.

Fixed logic clock, JSON save/load, string CMD operations, stdlib only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

LEVEL_TOLERANCE = 1e-9

SIDES = ("upstream", "downstream")
PUMP_DIRECTIONS = ("up", "down")


class CommandError(Exception):
    """Raised for illegal commands or invalid input."""


class FaultError(Exception):
    """Raised when operating while the lock is in a fault state."""


@dataclass
class Ship:
    name: str
    length: int

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name.strip():
            raise CommandError("ship name must not be empty")
        if not isinstance(self.length, int) or isinstance(self.length, bool):
            raise CommandError("ship length must be an integer")
        if self.length <= 0:
            raise CommandError("ship length must be positive")


class LockGame:
    """A single-chamber river lock with a fixed logic clock."""

    def __init__(self, chamber_length=120, chamber_capacity=3, pump_energy=6,
                 upstream_level=10.0, downstream_level=0.0, pump_step=2.0):
        if chamber_length <= 0:
            raise CommandError("chamber_length must be positive")
        if chamber_capacity <= 0:
            raise CommandError("chamber_capacity must be positive")
        if pump_energy < 0:
            raise CommandError("pump_energy must not be negative")
        if upstream_level <= downstream_level:
            raise CommandError("upstream_level must exceed downstream_level")
        if pump_step <= 0:
            raise CommandError("pump_step must be positive")
        self.chamber_length = chamber_length
        self.chamber_capacity = chamber_capacity
        self.pump_energy = pump_energy
        self.upstream_level = float(upstream_level)
        self.downstream_level = float(downstream_level)
        self.pump_step = float(pump_step)

        self.chamber_level = self.downstream_level
        self.time = 0
        self.paused = False
        self.fault = None
        self.doors = {"upstream": False, "downstream": False}
        self.chamber = []               # ships in the chamber, order preserved
        self.queue = []                 # [{"ticket": int, "ship": Ship}], order preserved
        self._tickets_in_chamber = set()
        self._next_ticket = 1
        self._released_tickets = []     # cancelled tickets, reusable, sorted
        self.events = []                # deterministic event log

    # ------------------------------------------------------------------ clock

    def tick(self, steps=1):
        steps = self._to_int(steps, "steps")
        if steps < 0:
            raise CommandError("steps must not be negative")
        if self.paused:
            # Fault/manual pause freezes the logic clock.
            return self.time
        self.time += steps
        self._record("tick", steps=steps)
        return self.time

    def pause(self):
        self.paused = True
        self._record("pause")

    def trigger_fault(self, reason="fault"):
        self.paused = True
        self.fault = reason
        self._record("fault", reason=reason)

    def resume(self):
        self.paused = False
        self.fault = None
        self._record("resume")

    # ------------------------------------------------------------------ queue

    def enqueue(self, name, length):
        self._check_running()
        ship = Ship(name, self._to_int(length, "length"))
        if any(entry["ship"].name == ship.name for entry in self.queue):
            raise CommandError(f"duplicate ship name in queue: {ship.name}")
        if any(s.name == ship.name for s in self.chamber):
            raise CommandError(f"duplicate ship name in chamber: {ship.name}")
        ticket = self._alloc_ticket()
        self.queue.append({"ticket": ticket, "ship": ship})
        self._record("enqueue", ticket=ticket, name=ship.name, length=ship.length)
        return ticket

    def cancel(self, ticket):
        self._check_running()
        ticket = self._to_int(ticket, "ticket")
        for index, entry in enumerate(self.queue):
            if entry["ticket"] == ticket:
                del self.queue[index]
                self._release_ticket(ticket)
                self._record("cancel", ticket=ticket)
                return ticket
        raise CommandError(f"ticket not in queue: {ticket}")

    def _alloc_ticket(self):
        if self._released_tickets:
            return self._released_tickets.pop(0)
        ticket = self._next_ticket
        self._next_ticket += 1
        return ticket

    def _release_ticket(self, ticket):
        if ticket not in self._released_tickets:
            self._released_tickets.append(ticket)
            self._released_tickets.sort()

    # ---------------------------------------------------------------- chamber

    def enter(self, tickets):
        """Move a fleet (one or more queued ships) into the chamber."""
        self._check_running()
        if isinstance(tickets, int):
            tickets = [tickets]
        tickets = [self._to_int(t, "ticket") for t in tickets]
        if not tickets:
            raise CommandError("fleet must not be empty")
        if len(set(tickets)) != len(tickets):
            raise CommandError("duplicate tickets in fleet")
        self._entry_side()  # raises if no door is open
        entries = []
        for ticket in tickets:
            if ticket in self._tickets_in_chamber:
                raise CommandError(f"ticket already in chamber: {ticket}")
            entry = self._find_queued(ticket)
            if entry is None:
                raise CommandError(f"ticket not in queue: {ticket}")
            entries.append(entry)
        total_length = sum(e["ship"].length for e in entries)
        if total_length > self.chamber_length:
            raise CommandError(
                f"fleet length {total_length} exceeds chamber {self.chamber_length}")
        if len(self.chamber) + len(entries) > self.chamber_capacity:
            raise CommandError("chamber capacity exceeded")
        for entry in entries:
            self.queue.remove(entry)
            self.chamber.append(entry["ship"])
            self._tickets_in_chamber.add(entry["ticket"])
        self._record("enter", tickets=list(tickets))
        return [e["ship"].name for e in entries]

    def exit_chamber(self):
        self._check_running()
        if not self.chamber:
            raise CommandError("chamber is empty")
        self._exit_side()  # raises if no door is open
        exited = [ship.name for ship in self.chamber]
        self.chamber = []
        self._tickets_in_chamber = set()
        self._record("exit", ships=exited)
        return exited

    def _entry_side(self):
        for side in SIDES:
            if self.doors[side]:
                return side
        raise CommandError("no door open: ships cannot enter")

    def _exit_side(self):
        for side in SIDES:
            if self.doors[side]:
                return side
        raise CommandError("no door open: ships cannot exit")

    def _find_queued(self, ticket):
        for entry in self.queue:
            if entry["ticket"] == ticket:
                return entry
        return None

    # ------------------------------------------------------------------ doors

    def open_door(self, side):
        self._check_running()
        self._validate_side(side)
        if side == "upstream":
            diff = self.upstream_level - self.chamber_level
        else:
            diff = self.downstream_level - self.chamber_level
        # A door may only open when the levels on both sides match; a
        # negative difference (reverse gradient) is rejected just like a
        # positive one.
        if abs(diff) > LEVEL_TOLERANCE:
            raise CommandError(
                f"water level difference {diff} blocks the {side} door")
        self.doors[side] = True
        self._record("open", side=side)

    def close_door(self, side):
        self._check_running()
        self._validate_side(side)
        self.doors[side] = False
        self._record("close", side=side)

    # ------------------------------------------------------------------- pump

    def pump(self, direction):
        self._check_running()
        if direction not in PUMP_DIRECTIONS:
            raise CommandError(f"unknown pump direction: {direction}")
        if any(self.doors.values()):
            raise CommandError("all doors must be closed to pump")
        if self.pump_energy <= 0:
            raise CommandError("pump energy exhausted")
        if direction == "up":
            if self.chamber_level >= self.upstream_level - LEVEL_TOLERANCE:
                raise CommandError("chamber already at upstream level")
            self.chamber_level = min(self.chamber_level + self.pump_step,
                                     self.upstream_level)
        else:
            if self.chamber_level <= self.downstream_level + LEVEL_TOLERANCE:
                raise CommandError("chamber already at downstream level")
            self.chamber_level = max(self.chamber_level - self.pump_step,
                                     self.downstream_level)
        self.pump_energy -= 1
        self._record("pump", direction=direction)
        return self.chamber_level

    # ----------------------------------------------------------------- events

    def _record(self, action, **params):
        self.events.append({"seq": len(self.events), "action": action,
                            "params": params})

    def apply_event(self, event):
        """Apply one logged event. Re-applying an event is a no-op."""
        seq = event.get("seq")
        if not isinstance(seq, int):
            raise CommandError("event missing integer seq")
        if seq < len(self.events):
            return  # already applied: replay must not change state twice
        if seq > len(self.events):
            raise CommandError(f"event gap: expected seq {len(self.events)}")
        action = event.get("action")
        params = event.get("params") or {}
        handlers = {
            "tick": lambda: self.tick(params["steps"]),
            "pause": self.pause,
            "fault": lambda: self.trigger_fault(params["reason"]),
            "resume": self.resume,
            "enqueue": lambda: self.enqueue(params["name"], params["length"]),
            "cancel": lambda: self.cancel(params["ticket"]),
            "enter": lambda: self.enter(list(params["tickets"])),
            "exit": self.exit_chamber,
            "open": lambda: self.open_door(params["side"]),
            "close": lambda: self.close_door(params["side"]),
            "pump": lambda: self.pump(params["direction"]),
        }
        handler = handlers.get(action)
        if handler is None:
            raise CommandError(f"unknown event action: {action}")
        handler()

    @classmethod
    def replay(cls, events, **config):
        """Rebuild a game deterministically from an event log."""
        game = cls(**config)
        for event in events:
            game.apply_event(event)
        return game

    # -------------------------------------------------------------- save/load

    def save(self):
        data = {
            "version": 1,
            "config": {
                "chamber_length": self.chamber_length,
                "chamber_capacity": self.chamber_capacity,
                "pump_energy": self.pump_energy,
                "upstream_level": self.upstream_level,
                "downstream_level": self.downstream_level,
                "pump_step": self.pump_step,
            },
            "state": {
                "time": self.time,
                "chamber_level": self.chamber_level,
                "paused": self.paused,
                "fault": self.fault,
                "doors": dict(self.doors),
                "chamber": [{"name": s.name, "length": s.length}
                            for s in self.chamber],
                "queue": [{"ticket": e["ticket"],
                           "ship": {"name": e["ship"].name,
                                    "length": e["ship"].length}}
                          for e in self.queue],
                "tickets_in_chamber": sorted(self._tickets_in_chamber),
                "next_ticket": self._next_ticket,
                "released_tickets": list(self._released_tickets),
                "events": json.loads(json.dumps(self.events)),
            },
        }
        return json.dumps(data, sort_keys=True)

    @classmethod
    def load(cls, data):
        if isinstance(data, (str, bytes)):
            if not data or not data.strip():
                raise CommandError("empty save data")
            try:
                data = json.loads(data)
            except json.JSONDecodeError as exc:
                raise CommandError(f"invalid save data: {exc}") from exc
        if not data:
            raise CommandError("empty save data")
        if not isinstance(data, dict) or "config" not in data or "state" not in data:
            raise CommandError("malformed save data")
        config = dict(data["config"])
        state = data["state"]
        saved_pump_energy = config.pop("pump_energy")
        game = cls(**config)
        game.pump_energy = saved_pump_energy
        game.time = state["time"]
        game.chamber_level = state["chamber_level"]
        game.paused = state["paused"]
        game.fault = state["fault"]
        game.doors = {side: bool(state["doors"].get(side, False))
                      for side in SIDES}
        # Restore in stored order; never reverse or reorder ships.
        game.chamber = [Ship(s["name"], s["length"]) for s in state["chamber"]]
        game.queue = [{"ticket": e["ticket"],
                       "ship": Ship(e["ship"]["name"], e["ship"]["length"])}
                      for e in state["queue"]]
        game._tickets_in_chamber = set(state["tickets_in_chamber"])
        game._next_ticket = state["next_ticket"]
        game._released_tickets = sorted(state["released_tickets"])
        game.events = list(state["events"])
        return game

    # ------------------------------------------------------------------ status

    def status(self):
        return {
            "time": self.time,
            "chamber_level": self.chamber_level,
            "upstream_level": self.upstream_level,
            "downstream_level": self.downstream_level,
            "pump_energy": self.pump_energy,
            "doors": dict(self.doors),
            "paused": self.paused,
            "fault": self.fault,
            "chamber": [s.name for s in self.chamber],
            "queue": [[e["ticket"], e["ship"].name] for e in self.queue],
        }

    # --------------------------------------------------------------------- cmd

    def execute(self, line):
        """Run one text command. Raises CommandError on illegal input."""
        if not isinstance(line, str) or not line.strip():
            raise CommandError("empty command")
        parts = line.split()
        cmd, args = parts[0].lower(), parts[1:]

        def need(count):
            if len(args) != count:
                raise CommandError(
                    f"{cmd} expects {count} argument(s), got {len(args)}")

        if cmd == "enqueue":
            need(2)
            return self.enqueue(args[0], self._to_int(args[1], "length"))
        if cmd == "cancel":
            need(1)
            return self.cancel(self._to_int(args[0], "ticket"))
        if cmd == "enter":
            if not args:
                raise CommandError("enter expects at least one ticket")
            return self.enter([self._to_int(a, "ticket") for a in args])
        if cmd == "exit":
            need(0)
            return self.exit_chamber()
        if cmd == "open":
            need(1)
            return self.open_door(args[0])
        if cmd == "close":
            need(1)
            return self.close_door(args[0])
        if cmd == "pump":
            need(1)
            return self.pump(args[0])
        if cmd == "tick":
            if len(args) > 1:
                raise CommandError("tick expects at most one argument")
            return self.tick(self._to_int(args[0], "steps") if args else 1)
        if cmd == "pause":
            need(0)
            return self.pause()
        if cmd == "resume":
            need(0)
            return self.resume()
        if cmd == "status":
            need(0)
            return self.status()
        if cmd == "save":
            need(0)
            return self.save()
        raise CommandError(f"unknown command: {cmd}")

    # ----------------------------------------------------------------- helpers

    def _check_running(self):
        if self.fault is not None:
            raise FaultError(f"lock halted by fault: {self.fault}")

    @staticmethod
    def _validate_side(side):
        if side not in SIDES:
            raise CommandError(f"unknown door side: {side}")

    @staticmethod
    def _to_int(value, field):
        if isinstance(value, bool):
            raise CommandError(f"{field} must be an integer")
        try:
            return int(value)
        except (TypeError, ValueError):
            raise CommandError(f"{field} must be an integer: {value!r}") from None
