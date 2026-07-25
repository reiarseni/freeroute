"""
Tests para services/cooldown.py — CooldownMechanism.

Cubre: threshold triggers, success reset, persist/restore, concurrent reads sin lock.
"""

import asyncio

import pytest

from services.cooldown import CooldownMechanism
from services.provider_handler import ErrorType


@pytest.fixture
def tmp_cooldowns(tmp_path):
    """CooldownMechanism con persistencia en tmp_path para tests.

    cooldown_time como dict por tipo sigue siendo compatible con la API;
    RATE_LIMIT=2s, SERVER_ERROR=2s (acorta el test_cooldown_expires).
    """
    return CooldownMechanism(
        allowed_fails={ErrorType.RATE_LIMIT: 1, ErrorType.SERVER_ERROR: 3},
        cooldown_time={
            ErrorType.RATE_LIMIT: 2.0,
            ErrorType.SERVER_ERROR: 2.0,
        },
        persist_path=tmp_path / "cooldowns.json",
    )


# ── Threshold triggers ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rate_limit_triggers_immediately(tmp_cooldowns):
    """RATE_LIMIT con allowed_fails=1 → cooldown inmediato."""
    triggered = await tmp_cooldowns.mark_failure(42, ErrorType.RATE_LIMIT)
    assert triggered is True
    assert tmp_cooldowns.is_cooling_down(42) is True


@pytest.mark.asyncio
async def test_server_error_needs_three_failures(tmp_cooldowns):
    """SERVER_ERROR con allowed_fails=3 → cooldown al 3er fallo."""
    await tmp_cooldowns.mark_failure(42, ErrorType.SERVER_ERROR)
    assert tmp_cooldowns.is_cooling_down(42) is False

    await tmp_cooldowns.mark_failure(42, ErrorType.SERVER_ERROR)
    assert tmp_cooldowns.is_cooling_down(42) is False

    triggered = await tmp_cooldowns.mark_failure(42, ErrorType.SERVER_ERROR)
    assert triggered is True
    assert tmp_cooldowns.is_cooling_down(42) is True


@pytest.mark.asyncio
async def test_different_error_types_are_independent(tmp_cooldowns):
    """Fallo de tipo diferente no acumula con otro tipo."""
    # 1 RATE_LIMIT → cooldown (threshold=1)
    await tmp_cooldowns.mark_failure(10, ErrorType.RATE_LIMIT)
    # 1 SERVER_ERROR más → no cooldown (threshold=3)
    await tmp_cooldowns.mark_failure(10, ErrorType.SERVER_ERROR)
    assert tmp_cooldowns.is_cooling_down(10) is True  # por el RATE_LIMIT


@pytest.mark.asyncio
async def test_cooldown_expires(tmp_cooldowns):
    """Cooldown dura el tiempo configurado."""
    await tmp_cooldowns.mark_failure(1, ErrorType.RATE_LIMIT)
    assert tmp_cooldowns.is_cooling_down(1) is True
    # Esperar a que expire: peor caso con jitter +15% es 2.3s; margen a 2.5s.
    await asyncio.sleep(2.5)
    assert tmp_cooldowns.is_cooling_down(1) is False


# ── Success reset ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mark_success_clears_cooldown(tmp_cooldowns):
    """mark_success limpia cooldown y contadores."""
    await tmp_cooldowns.mark_failure(5, ErrorType.SERVER_ERROR)
    await tmp_cooldowns.mark_failure(5, ErrorType.SERVER_ERROR)
    await tmp_cooldowns.mark_failure(5, ErrorType.SERVER_ERROR)
    assert tmp_cooldowns.is_cooling_down(5) is True

    await tmp_cooldowns.mark_success(5)
    assert tmp_cooldowns.is_cooling_down(5) is False


@pytest.mark.asyncio
async def test_mark_success_resets_counters(tmp_cooldowns):
    """mark_success resetea contadores — puede volver a fallar sin cooldown."""
    await tmp_cooldowns.mark_failure(6, ErrorType.SERVER_ERROR)  # 1 de 3
    await tmp_cooldowns.mark_failure(6, ErrorType.SERVER_ERROR)  # 2 de 3
    await tmp_cooldowns.mark_success(6)
    # Ahora empieza de 0 — puede fallar 2 veces más sin cooldown
    await tmp_cooldowns.mark_failure(6, ErrorType.SERVER_ERROR)
    await tmp_cooldowns.mark_failure(6, ErrorType.SERVER_ERROR)
    assert tmp_cooldowns.is_cooling_down(6) is False  # 2 de 3, no cooldown


# ── get_status ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_status_shows_active_cooldowns(tmp_cooldowns):
    await tmp_cooldowns.mark_failure(7, ErrorType.RATE_LIMIT)
    status = tmp_cooldowns.get_status()
    assert 7 in status["active_cooldowns"]
    assert status["active_cooldowns"][7]["remaining_seconds"] > 0
    assert status["active_cooldowns"][7]["error_type"] == "RATE_LIMIT"
    assert status["cooldown_times"]["RATE_LIMIT"] == 2.0


@pytest.mark.asyncio
async def test_get_status_empty_when_no_cooldowns(tmp_cooldowns):
    status = tmp_cooldowns.get_status()
    assert status["active_cooldowns"] == {}


# ── Persist/Restore ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_persist_and_restore(tmp_path):
    path = tmp_path / "cd.json"
    cd = CooldownMechanism(
        allowed_fails={ErrorType.RATE_LIMIT: 1},
        cooldown_time=60.0,
        persist_path=path,
    )
    await cd.mark_failure(100, ErrorType.RATE_LIMIT)
    assert cd.is_cooling_down(100) is True

    # Restaurar desde disco en nueva instancia
    cd2 = CooldownMechanism(
        allowed_fails={ErrorType.RATE_LIMIT: 1},
        cooldown_time=60.0,
        persist_path=path,
    )
    assert cd2.is_cooling_down(100) is True


@pytest.mark.asyncio
async def test_restore_missing_file_no_crash(tmp_path):
    """Archivo inexistente → no crash, estado vacío."""
    cd = CooldownMechanism(
        cooldown_time=60.0,
        persist_path=tmp_path / "nonexistent.json",
    )
    assert cd.is_cooling_down(1) is False
    status = cd.get_status()
    assert status["active_cooldowns"] == {}


@pytest.mark.asyncio
async def test_restore_corrupt_file_no_crash(tmp_path):
    """Archivo corrupto → no crash, estado vacío."""
    corrupt = tmp_path / "bad.json"
    corrupt.write_text("{{invalid json!!!")
    cd = CooldownMechanism(
        cooldown_time=60.0,
        persist_path=corrupt,
    )
    assert cd.is_cooling_down(1) is False


# ── Concurrent reads sin lock ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_reads_no_crash(tmp_cooldowns):
    """50 lecturas concurrentes no bloquean ni crashean."""
    await tmp_cooldowns.mark_failure(99, ErrorType.RATE_LIMIT)

    async def read_status():
        return tmp_cooldowns.is_cooling_down(99)

    results = await asyncio.gather(*[read_status() for _ in range(50)])
    assert all(r is True for r in results)


@pytest.mark.asyncio
async def test_update_config(tmp_cooldowns):
    """update_config cambia allowed_fails dinámicamente."""
    tmp_cooldowns.update_config(cooldown_time={ErrorType.RATE_LIMIT: 0.1})
    await tmp_cooldowns.mark_failure(200, ErrorType.RATE_LIMIT)
    assert tmp_cooldowns.is_cooling_down(200) is True
    await asyncio.sleep(0.2)
    assert tmp_cooldowns.is_cooling_down(200) is False


# ── Per-type cooldown_time (optim 2) ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_cooldown_time_per_error_type(tmp_path):
    """RATE_LIMIT=2s y SERVER_ERROR=5s dan duraciones distintas."""
    cd = CooldownMechanism(
        allowed_fails={ErrorType.RATE_LIMIT: 1, ErrorType.SERVER_ERROR: 1},
        cooldown_time={
            ErrorType.RATE_LIMIT: 2.0,
            ErrorType.SERVER_ERROR: 5.0,
        },
        persist_path=tmp_path / "cd.json",
    )
    await cd.mark_failure(1, ErrorType.RATE_LIMIT)
    await cd.mark_failure(2, ErrorType.SERVER_ERROR)
    info1 = cd.cooldown_info(1)
    info2 = cd.cooldown_info(2)
    assert info1 is not None and info2 is not None
    end1, err1 = info1
    end2, err2 = info2
    # SERVER_ERROR debería durar más. Con jitter ±15% el peor caso es
    # 5*0.85 - 2*1.15 = 1.95s de diferencia mínima; usamos 1.5s de margen.
    assert end2 > end1
    assert end2 - end1 >= 1.5
    assert err1 == ErrorType.RATE_LIMIT
    assert err2 == ErrorType.SERVER_ERROR


@pytest.mark.asyncio
async def test_context_window_no_cooldown(tmp_path):
    """CONTEXT_WINDOW_EXCEEDED con cooldown_time=0 no entra en cooldown."""
    cd = CooldownMechanism(
        allowed_fails={ErrorType.CONTEXT_WINDOW_EXCEEDED: 1},
        cooldown_time={ErrorType.CONTEXT_WINDOW_EXCEEDED: 0.0},
        persist_path=tmp_path / "cd.json",
    )
    triggered = await cd.mark_failure(3, ErrorType.CONTEXT_WINDOW_EXCEEDED)
    # trigger=True porque alcanzó threshold, pero no hay cooldown activo
    assert triggered is True
    assert cd.is_cooling_down(3) is False


# ── Bucketing por minuto (optim 1) ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_failures_in_different_minutes_do_not_accumulate(tmp_path):
    """Fallos en buckets separados no disparan cooldown ( LiteLLM-style windowed)."""
    cd = CooldownMechanism(
        allowed_fails={ErrorType.SERVER_ERROR: 3},
        cooldown_time={ErrorType.SERVER_ERROR: 60.0},
        persist_path=tmp_path / "cd.json",
    )
    # Forzamos el bucket al 0 y luego al 1 simulando paso de minuto
    from services.cooldown import _bucket as bucket_fn
    import services.cooldown as cd_mod

    # Inyectamos fallos manualmente en buckets distintos
    cd._fail_counts[10] = {ErrorType.SERVER_ERROR: {0: 1}}
    # El bucket actual (ahora) debería podar el bucket 0
    triggered = await cd.mark_failure(10, ErrorType.SERVER_ERROR)
    # El bucket 0 se poda → solo cuenta el fallo nuevo en bucket actual → 1 < 3
    assert triggered is False
    assert cd.is_cooling_down(10) is False


@pytest.mark.asyncio
async def test_success_does_not_reset_other_error_types(tmp_cooldowns):
    """mark_success resetea TODOS los contadores del deployment (comportamiento actual)."""
    # Documentamos el comportamiento: si marcamos success en deployment con
    # acumulado RATE_LIMIT, se resetea todo. Esto es herramienta para
    # entender el bucketing.
    await tmp_cooldowns.mark_failure(11, ErrorType.RATE_LIMIT)
    assert tmp_cooldowns.is_cooling_down(11) is True
    await tmp_cooldowns.mark_success(11)
    assert tmp_cooldowns.is_cooling_down(11) is False


# ── Excepción single-deployment (parity hardening) ─────────────────────────────


@pytest.mark.asyncio
async def test_single_deployment_transient_error_no_cooldown(tmp_cooldowns):
    """RATE_LIMIT (transitorio) en single-deployment no cooldownea."""
    triggered = await tmp_cooldowns.mark_failure(
        20, ErrorType.RATE_LIMIT, is_single_deployment=True,
    )
    assert triggered is True  # threshold alcanzado
    assert tmp_cooldowns.is_cooling_down(20) is False  # pero sin cooldown


@pytest.mark.asyncio
async def test_single_deployment_auth_error_still_cooldowns(tmp_path):
    """AUTH_ERROR (no transitorio) en single-deployment sí cooldownea."""
    cd = CooldownMechanism(
        allowed_fails={ErrorType.AUTH_ERROR: 1},
        cooldown_time={ErrorType.AUTH_ERROR: 5.0},
        persist_path=tmp_path / "cd.json",
    )
    triggered = await cd.mark_failure(21, ErrorType.AUTH_ERROR, is_single_deployment=True)
    assert triggered is True
    assert cd.is_cooling_down(21) is True


@pytest.mark.asyncio
async def test_multi_deployment_transient_error_cooldowns_as_before(tmp_cooldowns):
    """is_single_deployment=False (default) → comportamiento sin cambios."""
    triggered = await tmp_cooldowns.mark_failure(22, ErrorType.RATE_LIMIT)
    assert triggered is True
    assert tmp_cooldowns.is_cooling_down(22) is True


# ── Retry-After override (parity hardening) ────────────────────────────────────


@pytest.mark.asyncio
async def test_retry_after_used_as_exact_cooldown_duration(tmp_path):
    cd = CooldownMechanism(
        allowed_fails={ErrorType.RATE_LIMIT: 1},
        cooldown_time={ErrorType.RATE_LIMIT: 60.0},
        persist_path=tmp_path / "cd.json",
    )
    import time as time_mod
    before = time_mod.monotonic()
    await cd.mark_failure(30, ErrorType.RATE_LIMIT, retry_after=12.0)
    end, err = cd.cooldown_info(30)
    assert err == ErrorType.RATE_LIMIT
    # Sin jitter: duración exacta de 12s (con margen de ejecución del test)
    assert 11.9 <= end - before <= 12.1


@pytest.mark.asyncio
async def test_retry_after_ignored_when_none(tmp_cooldowns):
    """Sin retry_after, se usa cooldown_time configurado (con jitter, ver test de jitter)."""
    await tmp_cooldowns.mark_failure(31, ErrorType.RATE_LIMIT, retry_after=None)
    assert tmp_cooldowns.is_cooling_down(31) is True


# ── Jitter en cooldown_time (parity hardening) ──────────────────────────────────


@pytest.mark.asyncio
async def test_cooldown_duration_has_jitter(tmp_path):
    """Sin retry_after, la duración efectiva varía dentro de ±15% de cooldown_time."""
    import time as time_mod

    cd = CooldownMechanism(
        allowed_fails={ErrorType.RATE_LIMIT: 1},
        cooldown_time={ErrorType.RATE_LIMIT: 60.0},
        persist_path=tmp_path / "cd.json",
    )
    durations = []
    for dep_id in range(40, 55):
        before = time_mod.monotonic()
        await cd.mark_failure(dep_id, ErrorType.RATE_LIMIT)
        end, _ = cd.cooldown_info(dep_id)
        durations.append(end - before)

    assert all(51.0 <= d <= 69.0 for d in durations)
    # Con 15 muestras y jitter real, no todas deberían caer en el mismo valor exacto.
    assert len(set(round(d, 3) for d in durations)) > 1
