import asyncio
import io
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from fastapi import HTTPException
from PIL import Image, ImageDraw
from pydantic import ValidationError

from app.config import settings
from app.db import schema as t
from app.db.connection import engine
from app.matching.config import MatchingConfig
from app.matching.photos import Fingerprint, PHashProvider, photo_signal
from app.matching.scoring import score_pair
from app.matching.synthetic import image_bytes, prepare_images
from app.services.ingestion import ingest
from app.services.matching import candidate_rows, match_listing, merge, reject, split
from app.sources.mock.adapter import MockSourceAdapter
from app.worker.runner import run_once

C = MatchingConfig()


def photos(group="panamera", count=5):
    return prepare_images([f"mock://{group}/{i}" for i in range(count)])


def car(**extra):
    row = MockSourceAdapter().rows()[0] | extra
    row["price"] = Decimal(str(row["price"])) if row.get("price") is not None else None
    return row


@pytest.mark.parametrize("variant", ["resize", "jpeg", "watermark", "crop"])
def test_real_image_transformations(variant):
    provider = PHashProvider()
    distances = []
    for i in range(5):
        source = image_bytes(f"mock://panamera/{i}")
        image = Image.open(io.BytesIO(source))
        if variant == "resize":
            image = image.resize((240, 160))
        if variant == "watermark":
            ImageDraw.Draw(image).text((340, 296), "DEMO", fill="white")
        if variant == "crop":
            image = image.crop((5, 4, 475, 316))
        output = io.BytesIO()
        image.save(output, "JPEG", quality=45 if variant == "jpeg" else 90)
        a, b = provider.fingerprint("a", source), provider.fingerprint("b", output.getvalue())
        distances.append(provider.distance(a, b))
    assert all(d is not None and d <= 8 for d in distances), (variant, distances)


def test_photo_uniqueness_quality_and_versions():
    one = photos(count=1)[0]
    repeated = [Fingerprint(str(i), one.value, "ok") for i in range(6)]
    assert photo_signal(repeated, repeated, C)["matching_count"] == 1
    assert not photo_signal(repeated, repeated, C)["strong"]
    provider = PHashProvider()
    output = io.BytesIO()
    Image.new("RGB", (128, 128), "white").save(output, "PNG")
    assert provider.fingerprint("flat", output.getvalue()).quality == "flat"
    assert provider.fingerprint("bad", b"invalid").quality == "invalid"
    assert provider.fingerprint("big", b"x" * (8 * 1024 * 1024 + 1)).quality == "too_large"
    assert provider.fingerprint("stock", image_bytes("stock"), stock=True).quality == "stock"
    assert photo_signal([one], [Fingerprint("other", one.value, "ok", "embedding-v2")], C)["score"] is None
    signal = photo_signal(photos(), photos("bmw"), C)
    assert signal["matching_count"] == 0 and signal["score"] == 0


def test_cases_1_2_4_and_explanations():
    a, b = car(), car(mileage_km=144000)
    result = score_pair(a, b, photos(), photos())
    assert result["decision"] == "auto_merge" and result["photos"]["matching_count"] == 5
    assert result["mileage_difference"] == 1000 and result["config_version"] == C.version
    assert result["total_score"] == score_pair(b, a, photos(), photos())["total_score"]
    assert score_pair(a, b, photos(), photos("bmw"))["decision"] == "different"
    missing = score_pair(a, b, [], [])
    assert missing["decision"] == "possible_duplicate" and missing["reason"] == "insufficient_photos"
    assert missing["total_score"] < 0.4
    assert score_pair(a, b | {"model": "911"}, photos(), photos())["decision"] != "auto_merge"
    assert "model" in score_pair(a, b | {"model": "911"}, photos(), photos())["conflicts"]
    empty = score_pair({}, {}, [], [])
    assert empty["signals"]["description"] is None and empty["total_score"] == 0
    zero = score_pair(a | {"mileage_km": 0, "price": 0}, b | {"mileage_km": 0}, [], [])
    assert zero["signals"]["mileage"] == 1 and zero["signals"]["price"] is None


@pytest.mark.parametrize(
    "bad",
    [
        {"weights": {"photos": 1}},
        {"minimum_matching_photos": 2},
        {"maximum_processed_photos": 3, "minimum_matching_photos": 4},
        {"auto_merge_threshold": 0.5},
        {"candidate_limit": 0},
    ],
)
def test_config_validation(bad):
    with pytest.raises(ValidationError):
        MatchingConfig(**bad)


def seed(user, rows, *, with_photos=True, observed=None):
    with engine().begin() as conn:
        search_id = conn.execute(
            t.searches.insert()
            .values(user_id=user, name="Dedup test", filters={}, enabled_sources=["mock"])
            .returning(t.searches.c.id)
        ).scalar_one()
        search = dict(
            conn.execute(sa.select(t.searches).where(t.searches.c.id == search_id)).mappings().one()
        )
        for row in sorted(rows, key=lambda r: r["source_listing_id"]):
            listing = MockSourceAdapter().normalize(row, observed or t.now())
            ingest(conn, search, listing, photos() if with_photos else [])
        ids = list(
            conn.execute(
                sa.select(t.listings.c.id)
                .where(t.listings.c.source_listing_id.in_([r["source_listing_id"] for r in rows]))
                .order_by(t.listings.c.source_listing_id)
            ).scalars()
        )
    return search_id, ids


def versions(conn, user, ids=None):
    query = sa.select(t.clusters.c.id, t.clusters.c.version).where(
        t.clusters.c.user_id == user, t.clusters.c.archived_at.is_(None)
    )
    if ids:
        query = query.join(t.memberships, t.memberships.c.cluster_id == t.clusters.c.id).where(
            t.memberships.c.listing_id.in_(ids)
        )
    return {r.id: r.version for r in conn.execute(query)}


def test_auto_merge_idempotency_projection_and_case3(client, db):
    old_time = t.now() - timedelta(days=30)
    search_id, old = seed(db["alice"], [car(source_listing_id="a-old", status="REMOVED")], observed=old_time)
    _, new = seed(db["alice"], [car(source_listing_id="b-new", mileage_km=146000, price="1690000")])
    with engine().begin() as conn:
        match_listing(conn, db["alice"], new[0], C)
        assert len(versions(conn, db["alice"])) == 1
        match_listing(conn, db["alice"], new[0], C)
        assert conn.execute(sa.select(sa.func.count()).select_from(t.relist_links)).scalar_one() == 1
        assert (
            conn.execute(
                sa.select(sa.func.count()).select_from(t.events).where(t.events.c.type == "PROBABLE_RELIST")
            ).scalar_one()
            == 1
        )
        assert (
            conn.execute(sa.select(t.listings.c.status).where(t.listings.c.id == old[0])).scalar_one()
            == "REMOVED"
        )
        cluster_id = next(iter(versions(conn, db["alice"])))
    detail = client.get(f"/api/vehicles/{cluster_id}").json()
    assert len(detail["listings"]) == 2 and any(r["display_status"] == "RELISTED" for r in detail["listings"])
    assert client.get("/api/vehicles", params={"search_id": str(search_id)}).json()["total"] == 1


def test_case5_transitive_rejection_and_split_durability(db):
    user = db["alice"]
    _, ids = seed(user, [car(source_listing_id=i) for i in ("a", "b", "c")])
    with engine().begin() as conn:
        reject(conn, user, versions(conn, user, [ids[0], ids[2]]))
        match_listing(conn, user, ids[1], C)
        assert len(versions(conn, user)) == 2
        clustered = conn.execute(
            sa.select(t.memberships.c.cluster_id).where(
                t.memberships.c.user_id == user, t.memberships.c.listing_id == ids[1]
            )
        ).scalar_one()
        group = list(
            conn.execute(
                sa.select(t.memberships.c.listing_id).where(t.memberships.c.cluster_id == clustered)
            ).scalars()
        )
        assert len(group) == 2
        split(conn, user, clustered, versions(conn, user)[clustered], [[i] for i in group])
    settings.cache_clear()
    with engine().begin() as conn:
        for listing_id in ids:
            match_listing(conn, user, listing_id, MatchingConfig(auto_merge_threshold=0.7))
        membership = dict(
            conn.execute(
                sa.select(t.memberships.c.listing_id, t.memberships.c.cluster_id).where(
                    t.memberships.c.user_id == user
                )
            )
            .tuples()
            .all()
        )
        assert membership[group[0]] != membership[group[1]]
        assert membership[ids[0]] != membership[ids[2]]


def test_manual_api_isolation_versions_and_override(client, db):
    search_id, ids = seed(db["alice"], [car(source_listing_id=i) for i in ("a", "b")], with_photos=False)
    seed(db["bob"], [car(source_listing_id=i) for i in ("a", "b")], with_photos=False)
    with engine().begin() as conn:
        match_listing(conn, db["alice"], ids[0], C)
        av, bv = versions(conn, db["alice"]), versions(conn, db["bob"])
    payload = {"versions": {str(k): v for k, v in av.items()}}
    review = client.get("/api/duplicates", params={"search_id": str(search_id)})
    assert len(review.json()["items"]) == 1, review.text
    assert (
        client.post("/api/vehicles/merge", json={"versions": {str(k): v for k, v in bv.items()}}).status_code
        == 404
    )
    assert client.post("/api/duplicates/reject", json=payload).status_code == 200
    assert client.get("/api/duplicates", params={"search_id": str(search_id)}).json()["items"] == []
    assert client.post("/api/vehicles/merge", json=payload).status_code == 409
    with engine().connect() as conn:
        payload["versions"] = {str(k): v for k, v in versions(conn, db["alice"]).items()}
    assert client.post("/api/vehicles/merge", json=payload).status_code == 409
    response = client.post("/api/vehicles/merge", json=payload | {"supersede_rejections": True})
    assert response.status_code == 200, response.text
    cluster = response.json()["cluster_id"]
    detail = client.get(f"/api/vehicles/{cluster}").json()
    result = client.get("/api/vehicles", params={"search_id": str(search_id), "limit": 1}).json()
    assert result["total"] == 1 and result["items"][0]["listing_count"] == 2 and result["next_cursor"] is None
    assert (
        client.post(
            f"/api/vehicles/{cluster}/split",
            json={"version": detail["version"], "groups": [[str(ids[0])], [str(ids[0])]]},
        ).status_code
        == 422
    )
    response = client.post(
        f"/api/vehicles/{cluster}/split",
        json={"version": detail["version"], "groups": [[str(i)] for i in ids]},
    )
    assert response.status_code == 200, response.text
    with engine().connect() as conn:
        assert len(versions(conn, db["bob"])) == 2
        assert (
            conn.execute(
                sa.select(sa.func.count())
                .select_from(t.decisions)
                .where(t.decisions.c.superseded_at.is_not(None))
            ).scalar_one()
            == 2
        )


def test_concurrent_manual_merge_only_one_wins(db):
    user = db["alice"]
    seed(user, [car(source_listing_id=i) for i in ("a", "b")])
    with engine().connect() as conn:
        expected = versions(conn, user)

    def attempt():
        try:
            with engine().begin() as conn:
                merge(conn, user, expected, manual=True, config=C)
            return 200
        except HTTPException as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: attempt(), range(2)))
    assert sorted(outcomes) == [200, 409]
    with engine().connect() as conn:
        assert len(versions(conn, user)) == 1
        assert conn.execute(sa.select(sa.func.count()).select_from(t.operations)).scalar_one() == 1


def test_candidates_bounded_user_scoped_and_missing_make(db):
    user = db["alice"]
    _, ids = seed(user, [car(source_listing_id=f"car-{i}") for i in range(5)], with_photos=False)
    with engine().connect() as conn:
        row = dict(conn.execute(sa.select(t.listings).where(t.listings.c.id == ids[0])).mappings().one())
        found, limited = candidate_rows(conn, user, row, MatchingConfig(candidate_limit=2))
        assert len(found) == 2 and limited
        assert candidate_rows(conn, db["bob"], row, C) == ([], False)
        assert candidate_rows(conn, user, row | {"make": None}, C) == ([], False)


def test_manual_state_preserved_and_no_relist_without_removal(db):
    user = db["alice"]
    _, ids = seed(user, [car(source_listing_id="a"), car(source_listing_id="b")])
    with engine().begin() as conn:
        expected = versions(conn, user)
        first, second = list(expected)
        conn.execute(
            t.clusters.update()
            .where(t.clusters.c.id == first)
            .values(favourite=True, notes=[{"text": "first", "origin": str(first)}])
        )
        conn.execute(
            t.clusters.update()
            .where(t.clusters.c.id == second)
            .values(hidden=True, notes=[{"text": "second", "origin": str(second)}])
        )
        merged = merge(conn, user, expected, manual=True, config=C)
        row = conn.execute(sa.select(t.clusters).where(t.clusters.c.id == merged)).mappings().one()
        assert row["favourite"] and row["hidden"] and len(row["notes"]) == 2
        assert conn.execute(sa.select(sa.func.count()).select_from(t.relist_links)).scalar_one() == 0
        split_ids = split(conn, user, merged, row["version"], [[i] for i in ids])
        for row in conn.execute(sa.select(t.clusters).where(t.clusters.c.id.in_(split_ids))).mappings():
            assert row["favourite"] and row["hidden"] and row["notes"][0]["copied_from"] == str(merged)


def test_worker_dedup_scenario(client, db, monkeypatch):
    monkeypatch.setenv("MOCK_SCENARIO", "dedup")
    settings.cache_clear()
    response = client.post("/api/searches", json={"name": "Dedup demo", "filters": {}})
    assert response.status_code == 201
    assert asyncio.run(run_once())
    search_id = response.json()["search_id"]
    result = client.get("/api/vehicles", params={"search_id": search_id}).json()
    assert result["total"] == 5 and sum(r["listing_count"] for r in result["items"]) == 6
    assert client.get("/api/duplicates", params={"search_id": search_id}).json()["items"]
    with engine().connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(t.fingerprints)).scalar_one() == 2
    assert client.post(f"/api/searches/{search_id}/refresh").status_code == 202
    assert asyncio.run(run_once())
    assert client.get("/api/vehicles", params={"search_id": search_id}).json()["total"] == 5


def test_parallel_listing_update_blocks_merge_then_revalidates(db):
    user = db["alice"]
    _, ids = seed(user, [car(source_listing_id=i) for i in ("a", "b")])
    with engine().connect() as conn:
        expected = versions(conn, user)
    with engine().begin() as updater:
        updater.execute(t.listings.update().where(t.listings.c.id == ids[0]).values(body_type="coupe"))
        with engine().begin() as matcher:
            with pytest.raises(HTTPException) as exc:
                merge(matcher, user, expected, manual=True, config=C)
            assert exc.value.status_code == 409
    with engine().begin() as conn:
        with pytest.raises(HTTPException):
            merge(
                conn, user, expected, manual=False, config=C, evidence={"listing_ids": [str(i) for i in ids]}
            )
        assert len(versions(conn, user)) == 2


def test_score_threshold_boundary():
    a, b = car(), car(mileage_km=144000)
    initial = score_pair(a, b, photos(), photos())
    exact = sum(C.weights[k] * (v or 0) for k, v in initial["signals"].items())
    assert (
        score_pair(a, b, photos(), photos(), MatchingConfig(auto_merge_threshold=exact))["decision"]
        == "auto_merge"
    )
    assert (
        score_pair(a, b, photos(), photos(), MatchingConfig(auto_merge_threshold=exact + 0.000001))[
            "decision"
        ]
        == "possible_duplicate"
    )
