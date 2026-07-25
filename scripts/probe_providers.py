"""
Sonda directa de cada deployment configurado en infinity-provisioner.

Bypassa el Router (sin cooldowns, sin fallback): dispara una request mínima
directamente al upstream de cada deployment vía DataDrivenHandler.pre_call,
mide latencia y clasifica el resultado. Produce un reporte markdown al final.

Uso (desde la raíz del repo): .venv/bin/python3 scripts/probe_providers.py
"""

import asyncio
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db
from services.provider_handler import get_handler, set_runtime_providers

PROBE_BODY = {
    "messages": [{"role": "user", "content": "Reply with exactly one word: ok"}],
    "max_tokens": 5,
    "stream": False,
}

TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)


async def probe_one(client: httpx.AsyncClient, dep: dict) -> dict:
    instance = await db.get_instance(dep["api_instance_id"])
    result = {
        "deployment_id": dep["id"],
        "model_name": dep["model_name"],
        "provider": dep["provider"],
        "model_id": dep["model_id"],
        "enabled": bool(dep.get("enabled", 1)),
        "instance": dep["api_instance_id"],
    }

    if not instance:
        result.update(status="ERROR", detail="api_instance no encontrada", latency_ms=None)
        return result

    handler = get_handler(instance["provider"])
    prepared = await handler.pre_call(
        {"model_id": dep["model_id"], "id": dep["id"]}, instance, dict(PROBE_BODY)
    )

    start = time.perf_counter()
    try:
        resp = await client.post(prepared.url, json=prepared.body, headers=prepared.headers)
        latency_ms = (time.perf_counter() - start) * 1000
        if resp.status_code == 200:
            data = resp.json()
            content = (
                (data.get("choices", [{}])[0].get("message", {}).get("content") or "")
                if data.get("choices") else ""
            )
            result.update(status="OK", latency_ms=round(latency_ms), detail=content.strip()[:60])
        else:
            classification = handler.parse_error(resp.status_code, resp.text)
            result.update(
                status="FAIL",
                latency_ms=round(latency_ms),
                detail=f"HTTP {resp.status_code} {classification.error_type.value}: {resp.text[:120]}",
            )
    except httpx.TimeoutException:
        latency_ms = (time.perf_counter() - start) * 1000
        result.update(status="TIMEOUT", latency_ms=round(latency_ms), detail="timeout")
    except Exception as exc:
        latency_ms = (time.perf_counter() - start) * 1000
        result.update(status="DOWN", latency_ms=round(latency_ms), detail=f"{type(exc).__name__}: {exc}")

    return result


async def main():
    await db.init_db()
    set_runtime_providers(await db.get_providers())
    deployments = await db.get_deployments()

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        results = await asyncio.gather(*(probe_one(client, d) for d in deployments))

    results.sort(key=lambda r: (r["model_name"], r["provider"]))

    print("\n| model_name | provider | model_id | enabled | status | latency_ms | detail |")
    print("|---|---|---|---|---|---|---|")
    for r in results:
        print(
            f"| {r['model_name']} | {r['provider']} | {r['model_id']} | "
            f"{'yes' if r['enabled'] else 'no'} | {r['status']} | "
            f"{r['latency_ms'] if r['latency_ms'] is not None else '-'} | "
            f"{(r['detail'] or '').replace(chr(10), ' ')} |"
        )

    ok = sum(1 for r in results if r["status"] == "OK")
    print(f"\nTotal: {len(results)} · OK: {ok} · Fallando: {len(results) - ok}")


if __name__ == "__main__":
    asyncio.run(main())
