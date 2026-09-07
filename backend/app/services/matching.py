import itertools
import uuid
from datetime import timedelta
from typing import Any

import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError

from app.db import schema as t
from app.matching.config import MatchingConfig
from app.matching.photos import Fingerprint
from app.matching.scoring import conflicts, score_pair
from app.services.ingestion import event

MAX_CLUSTER_SIZE = 100


def lock_user(conn: Connection, user_id: uuid.UUID) -> None:
    conn.execute(sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": str(user_id)})


def pair(a: uuid.UUID, b: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    return (a, b) if a < b else (b, a)


def members(conn: Connection, user_id: uuid.UUID, cluster_ids: list[uuid.UUID]) -> list[dict[str, Any]]:
    try:
        with conn.begin_nested():
            return [
                dict(r)
                for r in conn.execute(
                    sa.select(t.listings, t.memberships.c.cluster_id, t.memberships.c.attached_at)
                    .join(t.memberships)
                    .where(t.memberships.c.user_id == user_id, t.memberships.c.cluster_id.in_(cluster_ids))
                    .order_by(t.listings.c.id)
                    .limit(MAX_CLUSTER_SIZE + 1)
                    .with_for_update(of=t.listings, nowait=True)
                ).mappings()
            ]
    except OperationalError as exc:
        if getattr(exc.orig, "sqlstate", None) != "55P03":
            raise
        raise HTTPException(409, "Объявления сейчас обновляются. Повторите действие.") from exc


def active_decisions(
    conn: Connection, user_id: uuid.UUID, ids: list[uuid.UUID]
) -> dict[tuple[uuid.UUID, uuid.UUID], str]:
    return {
        (r.left_id, r.right_id): r.decision
        for r in conn.execute(
            sa.select(t.decisions).where(
                t.decisions.c.user_id == user_id,
                t.decisions.c.superseded_at.is_(None),
                t.decisions.c.left_id.in_(ids),
                t.decisions.c.right_id.in_(ids),
            )
        )
    }


def write_decision(conn: Connection, user_id: uuid.UUID, a: uuid.UUID, b: uuid.UUID, value: str) -> None:
    left, right = pair(a, b)
    active = sa.and_(
        t.decisions.c.user_id == user_id,
        t.decisions.c.left_id == left,
        t.decisions.c.right_id == right,
        t.decisions.c.superseded_at.is_(None),
    )
    previous = conn.execute(sa.select(t.decisions.c.decision).where(active)).scalar_one_or_none()
    if previous == value:
        return
    conn.execute(t.decisions.update().where(active).values(superseded_at=t.now()))
    conn.execute(t.decisions.insert().values(user_id=user_id, left_id=left, right_id=right, decision=value))


def audit(conn: Connection, user_id: uuid.UUID, kind: str, payload: dict[str, Any]) -> None:
    conn.execute(t.operations.insert().values(user_id=user_id, kind=kind, payload=payload))


def owned_clusters(
    conn: Connection, user_id: uuid.UUID, versions: dict[uuid.UUID, int]
) -> list[dict[str, Any]]:
    lock_user(conn, user_id)
    rows = [
        dict(r)
        for r in conn.execute(
            sa.select(t.clusters)
            .where(
                t.clusters.c.user_id == user_id,
                t.clusters.c.id.in_(versions),
            )
            .order_by(t.clusters.c.id)
            .with_for_update()
        ).mappings()
    ]
    if len(rows) != len(versions):
        raise HTTPException(404, "Автомобиль не найден")
    if any(r["version"] != versions[r["id"]] or r["archived_at"] is not None for r in rows):
        raise HTTPException(409, "Состав автомобиля изменился. Обновите страницу.")
    return rows


def link_relists(
    conn: Connection,
    user_id: uuid.UUID,
    rows: list[dict[str, Any]],
    evidence: dict[str, Any],
    config: MatchingConfig,
) -> None:
    for old, new in itertools.permutations(rows, 2):
        if old["status"] != "REMOVED" or new["status"] != "ACTIVE":
            continue
        removed = conn.execute(
            sa.select(t.snapshots.c.observed_at)
            .where(t.snapshots.c.listing_id == old["id"], t.snapshots.c.status == "REMOVED")
            .order_by(t.snapshots.c.observed_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if removed is None or not timedelta(0) < new["first_seen_at"] - removed <= timedelta(
            days=config.relist_window_days
        ):
            continue
        created = conn.execute(
            insert(t.relist_links)
            .values(user_id=user_id, old_id=old["id"], new_id=new["id"], evidence=evidence)
            .on_conflict_do_nothing(index_elements=["user_id", "old_id", "new_id"])
            .returning(t.relist_links.c.id)
        ).scalar_one_or_none()
        if created:
            event(
                conn,
                user_id,
                new["id"],
                "PROBABLE_RELIST",
                f"relist:{old['id']}:{new['id']}",
                {
                    "old_id": str(old["id"]),
                    "new_id": str(new["id"]),
                    "old_price": str(old["price"]),
                    "new_price": str(new["price"]),
                    "evidence": evidence,
                },
            )


def merge(
    conn: Connection,
    user_id: uuid.UUID,
    versions: dict[uuid.UUID, int],
    *,
    manual: bool,
    config: MatchingConfig,
    supersede_rejections: bool = False,
    evidence: dict[str, Any] | None = None,
) -> uuid.UUID:
    clusters = owned_clusters(conn, user_id, versions)
    if len(clusters) < 2:
        raise HTTPException(422, "Выберите минимум два автомобиля")
    rows = members(conn, user_id, list(versions))
    if len(rows) > MAX_CLUSTER_SIZE:
        raise HTTPException(409, "Слишком большой кластер для безопасного объединения")
    decisions = active_decisions(conn, user_id, [r["id"] for r in rows])
    cross = [(a, b) for a, b in itertools.combinations(rows, 2) if a["cluster_id"] != b["cluster_id"]]
    rejected = any(decisions.get(pair(a["id"], b["id"])) == "different" for a, b in cross)
    if rejected and not (manual and supersede_rejections):
        raise HTTPException(
            409, "Есть ручное решение «разные автомобили». Подтвердите его замену перед объединением."
        )
    if not manual and any(conflicts(a, b, config) for a, b in cross):
        raise HTTPException(409, "Противоречие между участниками кластеров")
    if not manual:
        edge = [r for r in rows if str(r["id"]) in (evidence or {}).get("listing_ids", [])]
        if len(edge) != 2 or edge[0]["cluster_id"] == edge[1]["cluster_id"]:
            raise HTTPException(409, "Не хватает подтверждений связи между группами")
        saved = {
            r.listing_id: [Fingerprint(**p) for p in r.photos]
            for r in conn.execute(
                sa.select(t.fingerprints).where(t.fingerprints.c.listing_id.in_([r["id"] for r in edge]))
            )
        }
        fresh = score_pair(
            edge[0], edge[1], saved.get(edge[0]["id"], []), saved.get(edge[1]["id"], []), config
        )
        if fresh["decision"] != "auto_merge":
            raise HTTPException(409, "Данные изменились: совпадение больше не подтверждается")
        if evidence is None:
            evidence = {}
        evidence.update(fresh, listing_versions=[r["version"] for r in edge])
    if manual:
        for a, b in itertools.combinations(rows, 2):
            write_decision(conn, user_id, a["id"], b["id"], "same")
    target = clusters[0]["id"]
    # User observation dates and state survive regrouping. Listing history stays global and immutable.
    conn.execute(
        t.clusters.update()
        .where(t.clusters.c.id == target)
        .values(
            version=t.clusters.c.version + 1,
            first_seen_at=min(c["first_seen_at"] for c in clusters),
            last_seen_at=max(c["last_seen_at"] for c in clusters),
            favourite=any(c["favourite"] for c in clusters),
            hidden=any(c["hidden"] for c in clusters),
            notes=[note for c in clusters for note in c["notes"]],
        )
    )
    conn.execute(
        t.memberships.update()
        .where(t.memberships.c.user_id == user_id, t.memberships.c.cluster_id.in_(versions))
        .values(cluster_id=target)
    )
    conn.execute(
        t.clusters.update()
        .where(t.clusters.c.id.in_([c["id"] for c in clusters[1:]]))
        .values(archived_at=t.now(), version=t.clusters.c.version + 1)
    )
    proof = evidence or {"reason": "manual_same", "config_version": config.version}
    audit(
        conn,
        user_id,
        "manual_merge" if manual else "auto_merge",
        {
            "before": {
                str(c["id"]): [str(r["id"]) for r in rows if r["cluster_id"] == c["id"]] for c in clusters
            },
            "after": str(target),
            "evidence": proof,
        },
    )
    if manual:
        link_relists(conn, user_id, rows, proof, config)
    else:
        # Only the strong compared edge establishes a relist, not every transitive member.
        strong_ids = set(proof.get("listing_ids", []))
        link_relists(conn, user_id, [r for r in rows if str(r["id"]) in strong_ids], proof, config)
    return target


def reject(conn: Connection, user_id: uuid.UUID, versions: dict[uuid.UUID, int]) -> None:
    if len(versions) != 2:
        raise HTTPException(422, "Выберите два автомобиля")
    owned_clusters(conn, user_id, versions)
    rows = members(conn, user_id, list(versions))
    if len(rows) > MAX_CLUSTER_SIZE:
        raise HTTPException(409, "Слишком много объявлений")
    for a, b in itertools.combinations(rows, 2):
        if a["cluster_id"] != b["cluster_id"]:
            write_decision(conn, user_id, a["id"], b["id"], "different")
    conn.execute(
        t.clusters.update().where(t.clusters.c.id.in_(versions)).values(version=t.clusters.c.version + 1)
    )
    audit(
        conn,
        user_id,
        "reject",
        {"clusters": [str(c) for c in versions], "listings": [str(r["id"]) for r in rows]},
    )


def split(
    conn: Connection, user_id: uuid.UUID, cluster_id: uuid.UUID, version: int, groups: list[list[uuid.UUID]]
) -> list[uuid.UUID]:
    original = owned_clusters(conn, user_id, {cluster_id: version})[0]
    rows = members(conn, user_id, [cluster_id])
    flattened = [i for group in groups for i in group]
    if (
        len(rows) > MAX_CLUSTER_SIZE
        or len(groups) < 2
        or any(not g for g in groups)
        or len(flattened) != len(set(flattened))
        or set(flattened) != {r["id"] for r in rows}
    ):
        raise HTTPException(422, "Разбиение должно содержать каждое объявление ровно один раз")
    group_of = {i: n for n, group in enumerate(groups) for i in group}
    for a, b in itertools.combinations(flattened, 2):
        if group_of[a] != group_of[b]:
            write_decision(conn, user_id, a, b, "different")
    result = []
    for group in groups:
        subset = [r for r in rows if r["id"] in group]
        new_id = conn.execute(
            t.clusters.insert()
            .values(
                user_id=user_id,
                representative_listing_id=group[0],
                first_seen_at=min(r["attached_at"] for r in subset),
                last_seen_at=original["last_seen_at"],
                favourite=original["favourite"],
                hidden=original["hidden"],
                notes=[{"copied_from": str(cluster_id), "note": n} for n in original["notes"]],
            )
            .returning(t.clusters.c.id)
        ).scalar_one()
        conn.execute(
            t.memberships.update()
            .where(t.memberships.c.user_id == user_id, t.memberships.c.listing_id.in_(group))
            .values(cluster_id=new_id)
        )
        result.append(new_id)
    removed_links = []
    for link in conn.execute(
        sa.select(t.relist_links).where(
            t.relist_links.c.user_id == user_id,
            t.relist_links.c.old_id.in_(flattened),
            t.relist_links.c.new_id.in_(flattened),
        )
    ).mappings():
        if group_of[link["old_id"]] != group_of[link["new_id"]]:
            removed_links.append(str(link["id"]))
            conn.execute(t.relist_links.delete().where(t.relist_links.c.id == link["id"]))
    conn.execute(
        t.clusters.update()
        .where(t.clusters.c.id == cluster_id)
        .values(archived_at=t.now(), version=t.clusters.c.version + 1)
    )
    audit(
        conn,
        user_id,
        "split",
        {
            "before": str(cluster_id),
            "after": {str(c): [str(i) for i in g] for c, g in zip(result, groups, strict=True)},
            "invalidated_relist_links": removed_links,
        },
    )
    return result


def candidate_rows(
    conn: Connection, user_id: uuid.UUID, listing: dict[str, Any], config: MatchingConfig
) -> tuple[list[dict[str, Any]], bool]:
    if not listing.get("make") or not listing.get("model"):
        return [], False
    conditions = [
        t.memberships.c.user_id == user_id,
        t.listings.c.id != listing["id"],
        t.listings.c.make == listing["make"],
        t.listings.c.model == listing["model"],
        sa.or_(
            t.listings.c.status != "REMOVED",
            t.listings.c.last_seen_at >= t.now() - timedelta(days=config.relist_window_days),
        ),
    ]
    if listing["year"] is not None:
        conditions.append(
            sa.or_(
                t.listings.c.year.is_(None),
                t.listings.c.year.between(
                    listing["year"] - config.year_tolerance, listing["year"] + config.year_tolerance
                ),
            )
        )
    if listing["mileage_km"] is not None:
        conditions.append(
            sa.or_(
                t.listings.c.mileage_km.is_(None),
                sa.func.abs(t.listings.c.mileage_km - listing["mileage_km"])
                <= sa.func.greatest(
                    config.mileage_absolute_tolerance,
                    config.mileage_relative_tolerance
                    * sa.func.greatest(t.listings.c.mileage_km, listing["mileage_km"]),
                ),
            )
        )
    rank = sum(
        sa.case((t.listings.c[f] == listing[f], 1), else_=0)
        for f in ("city", "region", "generation", "color", "engine_volume", "power_hp")
        if listing.get(f) is not None
    )
    rows = [
        dict(r)
        for r in conn.execute(
            sa.select(t.listings)
            .join(t.memberships)
            .where(*conditions)
            .order_by(
                sa.literal(0).desc() if isinstance(rank, int) else rank.desc(),
                sa.func.abs(t.listings.c.mileage_km - (listing["mileage_km"] or 0)).asc().nulls_last(),
                t.listings.c.id,
            )
            .limit(config.candidate_limit + 1)
        ).mappings()
    ]
    return rows[: config.candidate_limit], len(rows) > config.candidate_limit


def match_listing(
    conn: Connection, user_id: uuid.UUID, listing_id: uuid.UUID, config: MatchingConfig
) -> None:
    lock_user(conn, user_id)
    listing = (
        conn.execute(
            sa.select(t.listings)
            .join(t.memberships)
            .where(t.memberships.c.user_id == user_id, t.listings.c.id == listing_id)
        )
        .mappings()
        .first()
    )
    if not listing:
        return
    candidates, limited = candidate_rows(conn, user_id, dict(listing), config)
    ids = [listing_id] + [r["id"] for r in candidates]
    fingerprints = {
        r.listing_id: [Fingerprint(**p) for p in r.photos]
        for r in conn.execute(sa.select(t.fingerprints).where(t.fingerprints.c.listing_id.in_(ids)))
    }
    decisions = active_decisions(conn, user_id, ids)
    for other in candidates:
        left, right = pair(listing_id, other["id"])
        a, b = (dict(listing), other) if left == listing_id else (other, dict(listing))
        evidence = score_pair(a, b, fingerprints.get(left, []), fingerprints.get(right, []), config)
        evidence.update(
            candidate_limit_hit=limited,
            listing_ids=[str(left), str(right)],
            listing_versions=[a["version"], b["version"]],
        )
        manual = decisions.get((left, right))
        if manual:
            evidence.update(decision="manual_" + manual, reason="manual_" + manual)
        group_rows = list(
            conn.execute(
                sa.select(t.clusters.c.id, t.clusters.c.version)
                .join(
                    t.memberships,
                    sa.and_(
                        t.memberships.c.cluster_id == t.clusters.c.id,
                        t.memberships.c.user_id == t.clusters.c.user_id,
                    ),
                )
                .where(t.memberships.c.user_id == user_id, t.memberships.c.listing_id.in_([left, right]))
            )
        )
        versions = {r.id: r.version for r in group_rows}
        if len(versions) == 2 and evidence["decision"] == "auto_merge":
            try:
                with conn.begin_nested():
                    merge(conn, user_id, versions, manual=False, config=config, evidence=evidence)
            except HTTPException as exc:
                evidence.update(
                    decision="possible_duplicate", reason="cluster_conflict", cluster_conflict=exc.detail
                )
        if len(versions) == 2 and evidence["decision"] == "possible_duplicate" and not manual:
            event(
                conn,
                user_id,
                right,
                "POSSIBLE_DUPLICATE",
                f"possible:{left}:{right}",
                {
                    "listing_ids": [str(left), str(right)],
                    "cluster_ids_at_event": [str(c) for c in versions],
                    "evidence": evidence,
                },
            )
        conn.execute(
            insert(t.candidates)
            .values(
                user_id=user_id,
                left_id=left,
                right_id=right,
                decision=evidence["decision"],
                evidence=evidence,
            )
            .on_conflict_do_update(
                index_elements=["user_id", "left_id", "right_id"],
                set_={"decision": evidence["decision"], "evidence": evidence, "updated_at": t.now()},
            )
        )
