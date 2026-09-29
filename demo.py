"""Fixed deterministic demo of the river lock game.

Run: python demo.py
"""

import json

from river.core import LockGame

SCRIPT = [
    "enqueue Alpha 40",
    "enqueue Beta 30",
    "enqueue Gamma 60",
    "cancel 3",            # Gamma cancels: ticket 3 is released
    "enqueue Delta 50",    # reuses released ticket 3
    "open downstream",
    "enter 1 2",           # Alpha + Beta = 70 <= 120
    "close downstream",
    "pump up",
    "pump up",
    "pump up",
    "pump up",
    "pump up",             # chamber level 0 -> 10 (upstream)
    "pause",
    "tick 3",              # frozen while paused
    "resume",
    "tick 2",
    "open upstream",
    "exit",
    "save",
]


def run_demo(verbose=True):
    game = LockGame()
    saved = None
    for line in SCRIPT:
        result = game.execute(line)
        if line == "save":
            saved = result
        if verbose:
            status = game.status()
            print(f"$ {line}")
            print(f"  t={status['time']} level={status['chamber_level']:.1f} "
                  f"energy={status['pump_energy']} chamber={status['chamber']} "
                  f"queue={status['queue']}")
    # Verify determinism: JSON round-trip and event replay agree.
    restored = LockGame.load(saved)
    assert restored.status() == game.status(), "save/load mismatch"
    clone = LockGame.replay(game.events)
    assert clone.chamber_level == game.chamber_level, "replay mismatch"
    assert clone.time == game.time, "replay clock mismatch"
    if verbose:
        print("save/load and event replay verified")
        print(json.dumps(game.status(), sort_keys=True))
    return game


if __name__ == "__main__":
    run_demo()
