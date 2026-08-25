import asyncio

from backend import main


def test_lifespan_can_skip_k8s_workers_for_local_admin(monkeypatch):
    calls = []
    monkeypatch.setenv("CARHER_ADMIN_SKIP_K8S", "1")
    monkeypatch.setattr(main.k8s_ops, "init_k8s", lambda: calls.append("k8s"))
    monkeypatch.setattr(main.db, "init_db", lambda: calls.append("db"))
    monkeypatch.setattr(
        main, "start_budget_fallback_worker", lambda: calls.append("budget")
    )
    monkeypatch.setattr(
        main.sync_worker, "start_workers", lambda: calls.append("sync")
    )
    monkeypatch.setattr(
        main.metrics_mod, "start_sampler", lambda db: calls.append("metrics")
    )

    async def run():
        async with main.lifespan(main.app):
            pass

    asyncio.run(run())

    assert calls == ["db", "budget"]
