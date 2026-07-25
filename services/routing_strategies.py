"""
Routing Strategies — estrategias pluggables de selección de deployment.
Cada estrategia implementa el Protocol RoutingStrategy.select().
"""

import random
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class RoutingContext:
    model_name: str
    original_model: str
    body: dict
    stream: bool = False
    args: dict = field(default_factory=dict)


class RoutingStrategy(Protocol):
    def select(self, deployments: list[dict], ctx: RoutingContext) -> dict:
        """Selecciona un deployment de la lista. Retorna el dict de deployment."""
        ...


class PositionalStrategy:
    """Ordena por 'order' y retorna el primero. Default."""

    def select(self, deployments: list[dict], ctx: RoutingContext) -> dict:
        return sorted(deployments, key=lambda d: d.get("order", 0))[0]


class SimpleShuffleStrategy:
    """Selección ponderada por 'weight'. O(n)."""

    def select(self, deployments: list[dict], ctx: RoutingContext) -> dict:
        weights = [max(0.0, d.get("weight", 1.0)) for d in deployments]
        total = sum(weights)
        if total <= 0:
            return deployments[0]
        r = random.random() * total
        cumulative = 0.0
        for d, w in zip(deployments, weights):
            cumulative += w
            if r <= cumulative:
                return d
        return deployments[-1]


class LatencyBasedStrategy:
    """
    Selecciona el deployment con menor latencia promedio reciente.
    Rolling window con deque(maxlen) y TTL configurable.
    Sin datos → fallback a median deployment por order.
    """

    def __init__(self, maxlen: int = 100, ttl_seconds: float = 300.0):
        self._latencies: dict[int, deque] = {}
        self._maxlen = maxlen
        self._ttl = ttl_seconds

    def record(self, deployment_id: int, latency_ms: float):
        dq = self._latencies.setdefault(deployment_id, deque(maxlen=self._maxlen))
        dq.append((time.monotonic(), latency_ms))

    def _avg_latency(self, deployment_id: int) -> float | None:
        dq = self._latencies.get(deployment_id)
        if not dq:
            return None
        now = time.monotonic()
        valid = [(ts, ms) for ts, ms in dq if now - ts < self._ttl]
        if not valid:
            return None
        return sum(ms for _, ms in valid) / len(valid)

    def select(self, deployments: list[dict], ctx: RoutingContext) -> dict:
        best = None
        best_latency = float("inf")
        for d in deployments:
            avg = self._avg_latency(d["id"])
            if avg is not None and avg < best_latency:
                best_latency = avg
                best = d
        if best is not None:
            return best
        # Sin datos → mediana por order
        sorted_deps = sorted(deployments, key=lambda d: d.get("order", 0))
        return sorted_deps[len(sorted_deps) // 2]


class LeastBusyStrategy:
    """Selecciona el deployment con menor cantidad de in-flight requests. Tie → order."""

    def __init__(self):
        self._in_flight: dict[int, int] = {}

    def increment(self, deployment_id: int):
        self._in_flight[deployment_id] = self._in_flight.get(deployment_id, 0) + 1

    def decrement(self, deployment_id: int):
        self._in_flight[deployment_id] = max(0, self._in_flight.get(deployment_id, 0) - 1)

    def select(self, deployments: list[dict], ctx: RoutingContext) -> dict:
        def sort_key(d):
            return (self._in_flight.get(d["id"], 0), d.get("order", 0))

        return sorted(deployments, key=sort_key)[0]


class LeastRpmStrategy:
    """Selecciona el deployment con menor RPM consumido en la ventana actual (bucket
    de 1 minuto). Tie → order. Requiere que el caller pase `rpm_counts` (dict
    deployment_id → count) en `ctx.args` — el Router ya calcula ese conteo para el
    throttle RPM/TPM, así que esta estrategia lo reutiliza en vez de duplicar estado."""

    def select(self, deployments: list[dict], ctx: RoutingContext) -> dict:
        rpm_counts = ctx.args.get("rpm_counts", {})

        def sort_key(d):
            return (rpm_counts.get(d["id"], 0), d.get("order", 0))

        return sorted(deployments, key=sort_key)[0]


# Instancias compartidas
_latency_strategy = LatencyBasedStrategy()
_least_busy_strategy = LeastBusyStrategy()
_least_rpm_strategy = LeastRpmStrategy()

ROUTING_STRATEGIES: dict[str, Any] = {
    "positional": PositionalStrategy(),
    "simple-shuffle": SimpleShuffleStrategy(),
    "latency-based": _latency_strategy,
    "least-busy": _least_busy_strategy,
    "least-rpm": _least_rpm_strategy,
}

DEFAULT_STRATEGY = "positional"
