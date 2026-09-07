import asyncio
import json
from datetime import timedelta

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from test_core import SEARCH, create

from app.auth.security import COOKIE
from app.backup import backup, verify
from app.config import settings
from app.db import schema as t
from app.db.connection import engine
from app.diagnostics import failure_context
from app.http_security import RequestBodyLimit
from app.main import app
from app.sources.images import allowed_image, sanitize


def test_session_cookie_rotation_expiry_and_logout(client):
    old = client.cookies.get(COOKIE)
    login = client.post("/api/auth/login", json={"username": "alice", "password": "test-password-alice"})
    cookie = login.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "path=/" in cookie
    assert "secure;" not in cookie  # This test uses local HTTP.
    assert old != client.cookies.get(COOKIE)
    with TestClient(app) as stale:
        stale.cookies.set(COOKIE, old)
        assert stale.get("/api/auth/me").status_code == 401
    with engine().begin() as conn:
        conn.execute(t.sessions.update().values(expires_at=t.now() - timedelta(seconds=1)))
    assert client.get("/api/auth/me").status_code == 401


def test_secure_cookie_for_https_configuration(client, monkeypatch):
    monkeypatch.setenv("COOKIE_SECURE", "true")
    settings.cache_clear()
    response = client.post("/api/auth/login", json={"username": "alice", "password": "test-password-alice"})
    assert "secure" in response.headers["set-cookie"].lower()


def test_mutation_guards_and_security_headers(client):
    for headers in ({"Origin": "https://foreign.invalid"}, {"X-CSRF-Token": ""}):
        response = client.post("/api/searches", json=SEARCH, headers=headers)
        assert response.status_code == 403
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["cache-control"] == "no-store"
        assert len(response.headers["x-request-id"]) == 32
    response = client.get("/api/auth/me")
    assert "password_hash" not in response.text and "token_digest" not in response.text


def test_api_body_limit_with_and_without_content_length(client):
    payload = json.dumps({"name": "x" * 70000, "filters": {}}).encode()
    assert client.post("/api/searches", content=payload).status_code == 413
    reached = []
    sent = []

    async def target(scope, receive, send):
        reached.append(True)

    messages = iter(
        [
            {"type": "http.request", "body": b"x" * 40000, "more_body": True},
            {"type": "http.request", "body": b"y" * 40000, "more_body": False},
        ]
    )

    async def receive():
        return next(messages)

    async def send(message):
        sent.append(message)

    asyncio.run(RequestBodyLimit(target)({"type": "http", "method": "POST", "headers": []}, receive, send))
    assert not reached and sent[0]["status"] == 413


def test_foreign_data_never_exposes_notes_images_or_events(client, db):
    from app.auth.security import digest

    ids = create(client)
    car = client.get("/api/vehicles", params={"search_id": ids["search_id"]}).json()["items"][0]
    # Revocation applies to existing sessions, including read-only image routes.
    with engine().begin() as conn:
        conn.execute(t.users.update().where(t.users.c.id == db["alice"]).values(enabled=False))
    for route in (
        "/api/vehicles/" + car["cluster_id"],
        "/api/events",
        "/api/searches",
        "/api/listings/" + car["id"] + "/image/0",
    ):
        assert client.get(route).status_code == 401
    with engine().connect() as conn:
        assert conn.execute(sa.select(t.sessions.c.token_digest)).scalar_one() == digest(
            client.cookies.get(COOKIE)
        )


def test_error_diagnostics_do_not_include_untrusted_message():
    try:
        raise ValueError("password=secret; https://source.invalid/contact; private note")
    except ValueError as exc:
        data = failure_context(exc)
    assert data["exception_type"] == "ValueError" and data["line"] > 0
    assert "secret" not in json.dumps(data) and "source.invalid" not in json.dumps(data)


def test_backup_refuses_overwrite_and_invalid_restore_target(tmp_path):
    archive = tmp_path / "backup.dump"
    archive.write_bytes(b"keep-me")
    with pytest.raises(ValueError, match="overwrite"):
        backup(archive)
    assert archive.read_bytes() == b"keep-me"
    for target in ("findcar", "postgres", 'findcar_restore_x"; DROP DATABASE findcar; --'):
        with pytest.raises(ValueError, match="new database"):
            verify(archive, target)


def test_backup_checksum_failure_precedes_database_access(tmp_path):
    archive = tmp_path / "backup.dump"
    archive.write_bytes(b"tampered")
    archive.with_suffix(".dump.json").write_text(json.dumps({"format": 1, "archive_sha256": "wrong"}))
    with pytest.raises(ValueError, match="checksum"):
        verify(archive, "findcar_restore_test")


@pytest.mark.parametrize(
    "url",
    [
        "https://s31.auto.drom.ru:bad/photo/a.jpg",
        "https://user:password@s31.auto.drom.ru/photo/a.jpg",
        "https://s31.auto.drom.ru/photo/a.jpg#https://127.0.0.1",
        "file:///etc/passwd",
        "https://avatars.avto.ru.evil.invalid/get-autoru-a/1/1200x900",
    ],
)
def test_image_addresses_reject_unsafe_variants(url):
    assert not allowed_image("drom", url) and not allowed_image("auto_ru", url)


def test_raster_decoder_rejects_html_and_oversized_content():
    from app.sources.base import SourceFailure

    for data in (b"<html><script>alert(1)</script></html>", b"x" * 2000001):
        with pytest.raises(SourceFailure):
            sanitize(data)


def test_same_vehicle_across_two_sources_is_one_cluster(db):
    from test_matching import C, photos

    from app.domain.models import NormalizedListing
    from app.services.ingestion import ingest
    from app.services.matching import match_listing
    from app.sources.mock.adapter import MockSourceAdapter

    base = MockSourceAdapter().normalize(MockSourceAdapter().rows()[0], t.now()).model_dump()
    with engine().begin() as conn:
        search = dict(
            conn.execute(
                t.searches.insert()
                .values(
                    user_id=db["alice"],
                    name="cross-source fixture",
                    filters={},
                    enabled_sources=["drom", "auto_ru"],
                )
                .returning(t.searches)
            )
            .mappings()
            .one()
        )
        for source, url in [
            ("drom", "https://auto.drom.ru/porsche/panamera/800000001.html"),
            ("auto_ru", "https://auto.ru/cars/used/sale/porsche/panamera/100000001-abcdef/"),
        ]:
            listing = NormalizedListing.model_validate(
                base | {"source": source, "source_url": url, "images": [], "main_image_url": None}
            )
            ingest(
                conn, search, listing, photos()
            )  # Explicit offline image fixture, never a live image fallback.
        for listing_id in conn.execute(sa.select(t.listings.c.id)).scalars():
            match_listing(conn, db["alice"], listing_id, C)
        groups = conn.execute(sa.select(sa.func.count(sa.distinct(t.memberships.c.cluster_id)))).scalar_one()
        assert groups == 1


def test_two_sources_preserve_results_when_third_fails(client, monkeypatch):
    from app.domain.models import NormalizedListing
    from app.sources.base import SourceFailure
    from app.sources.mock.adapter import MockSourceAdapter
    from app.worker import runner

    class Fixture(MockSourceAdapter):
        def __init__(self, source):
            super().__init__()
            self.real_source = source

        def rows(self):
            return super().rows()[:1]

        async def get_listing(self, listing_id):
            # This fixture exposes a real-source URL but resolves it entirely offline.
            return self.rows()[0]

        def normalize(self, raw, observed_at):
            base = super().normalize(raw, observed_at).model_dump()
            url = (
                "https://auto.drom.ru/porsche/panamera/800000001.html"
                if self.real_source == "drom"
                else "https://auto.ru/cars/used/sale/porsche/panamera/100000001-abcdef/"
            )
            return NormalizedListing.model_validate(
                base | {"source": self.real_source, "source_url": url, "images": [], "main_image_url": None}
            )

    def adapter(source):
        if source == "avito":
            raise SourceFailure("ACCESS_NOT_CONFIGURED")
        return Fixture(source)

    monkeypatch.setattr(runner, "adapter_for", adapter)
    ids = create(client, enabled_sources=["drom", "auto_ru", "avito"])
    run = client.get("/api/search-runs/" + ids["run_id"]).json()
    assert run["outcome"] == "partial"
    assert sum(s["state"] == "complete" for s in run["sources"]) == 2
    with engine().connect() as conn:
        assert set(conn.execute(sa.select(t.listings.c.source)).scalars()) == {"drom", "auto_ru"}
