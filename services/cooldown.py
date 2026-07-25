"""
CooldownMechanism — estado de cooldown por deployment_id.

Reemplaza a services/key_rotator.py del v4 (que estaba atado a instance_id
y solo distinguía 429/404 hardcoded).

Diferencias con el v4:
  - Una sola entidad: deployment_id (no instance + model separados).
  - Falls por tipo de error (RATE_LIMIT, MODEL_NOT_FOUND, etc.).
  - Threshold ventanal por minuto (bucketed), no acumulado absoluto (LiteLLM-style).
  - Cooldown time por ErrorType, no único global (LiteLLM-style).
  - Persistencia JSON con atomic rename (survive restarts).
  - Reads NO adquieren lock — leen snapshot. Solo writes con asyncio.Lock.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import tempfile
import time
from pathlib import Path

from services.provider_handler import ErrorType

COOLDOWNS_PATH = Path.home() / ".infinity-provisioner-cooldowns.json"

# ── ventanas de 60 segundos por bucket ────────────────────────────────────────
_MINUTE = 60


def _bucket(t: float | None = None) -> int:
    """floor monotonic a la ventana de 1 minuto para bucketing."""
    return int((t if t is not None else time.monotonic()) // _MINUTE)


# ── Defaults ───────────────────────────────────────────────────────────────────

DEFAULT_ALLOWED_FAILS: dict[ErrorType, int] = {
    ErrorType.RATE_LIMIT:                 1,
    ErrorType.MODEL_NOT_FOUND:            2,
    ErrorType.SERVER_ERROR:               3,
    ErrorType.AUTH_ERROR:                 1,
    ErrorType.TIMEOUT:                    3,
    ErrorType.UNKNOWN:                    2,
}

# Cooldown por tipo de error (segundos). 0 = no cooldown (el deployment se
# reintenta de inmediato en otra iteración, pero se marca para evitar loop).
DEFAULT_COOLDOWN_PER_TYPE: dict[ErrorType, float] = {
    ErrorType.RATE_LIMIT:                 60.0,
    ErrorType.MODEL_NOT_FOUND:            300.0,
    ErrorType.SERVER_ERROR:               60.0,
    ErrorType.AUTH_ERROR:                 300.0,
    ErrorType.TIMEOUT:                    30.0,
    ErrorType.CONTEXT_WINDOW_EXCEEDED:     0.0,
    ErrorType.CONTENT_POLICY_VIOLATION:    0.0,
    ErrorType.UNKNOWN:                    60.0,
}

DEFAULT_COOLDOWN_TIME = 60.0  # fallback si no hay mapeo por tipo

# Errores transitorios: reintentar el mismo deployment más tarde puede tener
# éxito (rate limit se libera, servidor se recupera, timeout puntual). En
# model_names con un único deployment habilitado, cooldownear por estos tipos
# deja el tier entero sin healthy por un solo fallo — se exceptúan.
_TRANSIENT_ERROR_TYPES: frozenset[ErrorType] = frozenset({
    ErrorType.RATE_LIMIT,
    ErrorType.SERVER_ERROR,
    ErrorType.TIMEOUT,
    ErrorType.UNKNOWN,
})

# Jitter multiplicativo sobre cooldown_time (±15%) para evitar que varios
# deployments cooldowneados por el mismo evento reabran todos a la vez.
_JITTER_RATIO = 0.15

# Rango aceptado para un header Retry-After del upstream (segundos).
_RETRY_AFTER_MAX = 60.0


class CooldownMechanism:
    """Estado por deployment con bucketing por minuto.

    - _fail_counts[dep_id][error_type] = {bucket: count, …}
    - _cooldown_ends[dep_id] = (monotonic_end_seconds, error_type_que_disparó)
    """

    def __init__(
        self,
        allowed_fails: dict[ErrorType, int] | None = None,
        cooldown_time: float | dict[ErrorType, float] | None = None,
        persist_path: Path | str | None = None,
    ):
        self._allowed_fails = dict(allowed_fails) if allowed_fails else dict(DEFAULT_ALLOWED_FAILS)
        self._cooldown_times = (
            self._normalise_cooldown_times(cooldown_time) if cooldown_time
            else dict(DEFAULT_COOLDOWN_PER_TYPE)
        )
        self._path = Path(persist_path) if persist_path is not None else COOLDOWNS_PATH

        self._lock = asyncio.Lock()
        self._fail_counts: dict[int, dict[ErrorType, dict[int, int]]] = {}
        self._cooldown_ends: dict[int, tuple[float, ErrorType]] = {}

        self._restore()

    # ── Config ──────────────────────────────────────────────────────────────────

    def _normalise_cooldown_times(self, value: float | dict[ErrorType, float]) -> dict[ErrorType, float]:
        if isinstance(value, (int, float)):
            return {et: float(value) for et in DEFAULT_COOLDOWN_PER_TYPE}
        return dict(value)

    def update_config(
        self,
        allowed_fails: dict[ErrorType, int] | None = None,
        cooldown_time: float | dict[ErrorType, float] | None = None,
    ) -> None:
        if allowed_fails is not None:
            self._allowed_fails = dict(allowed_fails)
        if cooldown_time is not None:
            self._cooldown_times = self._normalise_cooldown_times(cooldown_time)

    # ── Reads ───────────────────────────────────────────────────────────────────

    def is_cooling_down(self, deployment_id: int) -> bool:
        end_info = self._cooldown_ends.get(deployment_id)
        if end_info is None:
            return False
        end, _ = end_info
        return time.monotonic() < end

    def cooldown_info(self, deployment_id: int) -> tuple[float, ErrorType] | None:
        """Devuelve (end_mono, error_type) si está en cooldown; None si no."""
        info = self._cooldown_ends.get(deployment_id)
        if info is None:
            return None
        end, err = info
        if time.monotonic() < end:
            return (end, err)
        return None

    def get_status(self) -> dict:
        now = time.monotonic()
        active: dict[int, dict] = {}
        for dep_id, (end, err) in self._cooldown_ends.items():
            remaining = end - now
            if remaining > 0:
                active[dep_id] = {"remaining_seconds": round(remaining), "error_type": err.value}

        cooldown_times_display = {}
        for et, seconds in self._cooldown_times.items():
            cooldown_times_display[et.value] = seconds

        af_display = {et.value: v for et, v in self._allowed_fails.items()}
        return {
            "cooldown_times": cooldown_times_display,
            "allowed_fails": af_display,
            "active_cooldowns": active,
        }

    # ── Writes ──────────────────────────────────────────────────────────────────

    async def mark_failure(
        self,
        deployment_id: int,
        error_type: ErrorType,
        is_single_deployment: bool = False,
        retry_after: float | None = None,
    ) -> bool:
        """Registra fallo con bucketing por minuto. Cooldown si supera threshold en el bucket actual.

        `is_single_deployment`: si el model_name de este deployment no tiene otros
        deployments habilitados, los errores transitorios (RATE_LIMIT, SERVER_ERROR,
        TIMEOUT, UNKNOWN) NO disparan cooldown (se registra el fallo igualmente,
        pero no se fija `_cooldown_ends`). Errores no transitorios (AUTH_ERROR,
        MODEL_NOT_FOUND) cooldownean igual, tenga o no hermanos.

        `retry_after`: si viene informado (ya validado por el caller en [0, 60]s),
        se usa como duración exacta de cooldown, sin jitter, en vez de
        `cooldown_time` configurado.
        """
        async with self._lock:
            bucket = _bucket(time.monotonic())
            self._prune_stale_buckets(deployment_id, bucket)

            counts = self._fail_counts.setdefault(deployment_id, {})
            err_buckets = counts.setdefault(error_type, {})
            err_buckets[bucket] = err_buckets.get(bucket, 0) + 1

            # Sumamos el total en todos los buckets vigentes para este tipo (0-3 buckets)
            total = sum(err_buckets.values())
            threshold = self._allowed_fails.get(error_type, 2)
            triggered = total >= threshold

            if triggered:
                exempt = is_single_deployment and error_type in _TRANSIENT_ERROR_TYPES
                if not exempt:
                    if retry_after is not None:
                        cd_time = retry_after
                    else:
                        base = self._cooldown_times.get(error_type, DEFAULT_COOLDOWN_TIME)
                        cd_time = base * (1 + random.uniform(-_JITTER_RATIO, _JITTER_RATIO))
                    if cd_time > 0:
                        self._cooldown_ends[deployment_id] = (time.monotonic() + cd_time, error_type)
                counts.pop(error_type, None)
                # Si no quedan tipos → quitar el deployment del dict
                if not counts:
                    self._fail_counts.pop(deployment_id, None)
                await asyncio.to_thread(self._persist)
            return triggered

    async def mark_success(self, deployment_id: int) -> None:
        """Resetea contadores de cooldown para el deployment."""
        async with self._lock:
            self._fail_counts.pop(deployment_id, None)
            self._cooldown_ends.pop(deployment_id, None)
            await asyncio.to_thread(self._persist)

    # ── Internas ────────────────────────────────────────────────────────────────

    def _prune_stale_buckets(self, deployment_id: int, current_bucket: int):
        """Borra buckets > 1 ventana antiguos (conservamos el anterior y el actual)."""
        thresholds = self._fail_counts.get(deployment_id)
        if not thresholds:
            return
        for error_type, buckets in list(thresholds.items()):
            stale = [b for b in buckets if b < current_bucket - 1]
            for b in stale:
                del buckets[b]
            if not buckets:
                del thresholds[error_type]
        if not thresholds:
            self._fail_counts.pop(deployment_id, None)

    # ── Persistencia ───────────────────────────────────────────────────────────

    def _persist(self) -> None:
        try:
            now_mono = time.monotonic()
            now_wall = time.time()
            data = {
                "cooldown_times": {et.value: s for et, s in self._cooldown_times.items()},
                "active_cooldowns": {},
                "fail_counts": {
                    str(dep_id): {et.value: {str(b): c for b, c in counts.items()}
                                  for et, counts in types.items()}
                    for dep_id, types in self._fail_counts.items()
                },
            }
            for dep_id, (end, err) in self._cooldown_ends.items():
                remaining = end - now_mono
                if remaining > 0:
                    data["active_cooldowns"][str(dep_id)] = {
                        "end_wall": now_wall + remaining,
                        "error_type": err.value,
                    }
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(
                prefix=".cooldowns.", suffix=".tmp", dir=str(self._path.parent)
            )
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(data, f)
                os.replace(tmp_path, self._path)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        except Exception:
            pass

    def _restore(self) -> None:
        try:
            if not self._path.exists():
                return
            data = json.loads(self._path.read_text())

            # Restaurar cooldown_times
            if "cooldown_times" in data:
                self._cooldown_times = {}
                for k, v in data["cooldown_times"].items():
                    try:
                        self._cooldown_times[ErrorType(k)] = float(v)
                    except (ValueError, TypeError):
                        pass

            now_mono = time.monotonic()
            now_wall = time.time()
            for dep_id_str, info in data.get("active_cooldowns", {}).items():
                if isinstance(info, dict):
                    remaining = float(info["end_wall"]) - now_wall
                    err = ErrorType(info.get("error_type", "UNKNOWN"))
                else:
                    remaining = float(info) - now_wall
                    err = ErrorType.UNKNOWN
                if remaining > 0:
                    self._cooldown_ends[int(dep_id_str)] = (now_mono + remaining, err)

            # Restaurar fail_counts con sus buckets
            for dep_id_str, types in data.get("fail_counts", {}).items():
                dep_id = int(dep_id_str)
                current_bucket = _bucket(now_mono)
                self._fail_counts[dep_id] = {}
                for et_str, buckets in types.items():
                    try:
                        error_type = ErrorType(et_str)
                    except ValueError:
                        continue
                    restored = {}
                    for bucket_str, count in buckets.items():
                        try:
                            b = int(bucket_str)
                            if b >= current_bucket - 1:
                                restored[b] = count
                        except (ValueError, TypeError):
                            pass
                    if restored:
                        self._fail_counts[dep_id][error_type] = restored
        except Exception:
            pass


# Instancia global
cooldown = CooldownMechanism()
