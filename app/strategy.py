"""Strategy registry.

No strategy is currently configured — the previous Renko / Fast Ichimoku
strategies were removed. This module is the seam where a new strategy plugs in:

  * add its id + metadata to STRATEGIES,
  * implement its decision in the engine (see TFEngine.active_strats / tick),
  * call self._enter(...) / drive exits from the tick loop.

Until then STRATEGIES is empty and the engine trades nothing.
"""
from __future__ import annotations

STRATEGIES: dict[int, dict] = {}


def name(sid: int) -> str:
    return STRATEGIES.get(sid, {}).get("name", f"S{sid}")
