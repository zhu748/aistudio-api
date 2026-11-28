"""The dashboard detail read snapshots the selected live request, not its peers."""

from contextlib import closing

import httpx

from aistudio_api.api import routes_operations
from aistudio_api.api.app import app
from aistudio_api.config import settings
from aistudio_api.infrastructure import request_context as registry
from aistudio_api.infrastructure.request_context import RequestContext
from aistudio_api.infrastructure.request_history import RequestHistory


async def test_detail_snapshots_only_the_selected_request(tmp_path, monkeypatch):
    key = "req_shared"
    url = f"/api/requests/{key}"
    live = RequestContext(key, model="replacement", events=[{"password": "fixture"}])
    snapshot = registry.request_snapshot
    stored = snapshot(RequestContext(key, model="stored", result="success"))
    seen = []

    def observe(context, *, running=False):
        assert not registry._active_lock.locked()
        seen.append(context.request_id)
        if context is live:
            # Removal after lookup does not invalidate the selected reference.
            registry.unregister_request(key)
        return snapshot(context, running=running)

    monkeypatch.setattr(registry, "_active", {})
    monkeypatch.setattr(registry, "request_snapshot", observe)
    registry.register_request(RequestContext("req_peer"))
    registry.register_request(RequestContext(key, model="superseded"))
    registry.register_request(live)

    monkeypatch.setattr(settings, "web_password", "fixture-only")
    with closing(RequestHistory(tmp_path / "history.sqlite3")) as history:
        history.insert(stored)
        monkeypatch.setattr(routes_operations, "request_history", lambda: history)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://fixture.invalid"
        ) as client:
            denied = await client.get(url)
            assert denied.status_code == 401 and not seen
            client.headers["Authorization"] = "Bearer fixture-only"

            active = await client.get(url)
            assert active.status_code == 200 and seen == [key]
            record = active.json()
            assert (record["model"], record["result"], record["events"]) == (
                "replacement",
                "running",
                [{"password": "***"}],
            )

            seen.clear()
            persisted = await client.get(url)
            assert persisted.status_code == 200 and persisted.json() == stored
            missing = await client.get("/api/requests/req_missing")
            assert missing.status_code == 404
            assert missing.json() == {
                "error": {
                    "code": 404,
                    "message": "请求记录不存在或已过保留期",
                    "status": "NOT_FOUND",
                }
            }
            assert not seen
