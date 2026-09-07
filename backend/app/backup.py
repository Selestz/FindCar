"""Consistent PostgreSQL backup and restore verification into a new temporary database."""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import URL, Connection, make_url

from app.config import settings


def pg_environment(url: URL, database: str | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        PGHOST=url.host or "localhost",
        PGPORT=str(url.port or 5432),
        PGUSER=url.username or "postgres",
        PGPASSWORD=url.password or "",
        PGDATABASE=database or url.database or "postgres",
        PGCONNECT_TIMEOUT="10",
    )
    if "sslmode" in url.query:
        env["PGSSLMODE"] = str(url.query["sslmode"])
    return env


def utility(name: str, pg_bin: str | None) -> str:
    path = Path(pg_bin) / (name + (".exe" if os.name == "nt" else "")) if pg_bin else None
    found = str(path) if path and path.is_file() else shutil.which(name)
    if not found:
        raise ValueError(f"{name} not found; pass --pg-bin")
    return found


def table_manifest(conn: Connection) -> dict[str, Any]:
    metadata = sa.MetaData()
    metadata.reflect(conn, schema="public")
    result = {}
    for table in sorted(metadata.tables.values(), key=lambda x: x.name):
        digest = hashlib.sha256()
        count = 0
        query = sa.select(table).order_by(*table.primary_key.columns)
        if not list(table.primary_key.columns):
            raise ValueError("Backup verification requires a primary key: " + table.name)
        for row in conn.execution_options(stream_results=True).execute(query).mappings():
            encoded = json.dumps(
                dict(row), sort_keys=True, ensure_ascii=True, default=str, separators=(",", ":")
            )
            digest.update(encoded.encode() + b"\n")
            count += 1
        result[table.name] = {"rows": count, "sha256": digest.hexdigest()}
    return result


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def backup(output: Path, pg_bin: str | None = None) -> dict[str, Any]:
    output = output.resolve()
    manifest_path = output.with_suffix(output.suffix + ".json")
    if output.exists() or manifest_path.exists():
        raise ValueError("Backup destination already exists; refusing to overwrite")
    output.parent.mkdir(parents=True, exist_ok=True)
    url = make_url(settings().database_url)
    db = sa.create_engine(url)
    created = False
    try:
        with db.connect().execution_options(isolation_level="REPEATABLE READ") as conn, conn.begin():
            conn.execute(sa.text("SET TRANSACTION READ ONLY"))
            conn.execute(sa.text("SET LOCAL TIME ZONE 'UTC'"))
            snapshot = conn.execute(sa.text("SELECT pg_export_snapshot()")).scalar_one()
            tables = table_manifest(conn)
            with output.open("xb") as archive:
                created = True
                subprocess.run(
                    [
                        utility("pg_dump", pg_bin),
                        "--format=custom",
                        "--no-owner",
                        "--no-acl",
                        "--snapshot=" + snapshot,
                    ],
                    stdout=archive,
                    stderr=subprocess.PIPE,
                    env=pg_environment(url),
                    check=True,
                    timeout=300,
                )
        manifest = {"format": 1, "archive_sha256": file_hash(output), "tables": tables}
        with manifest_path.open("x", encoding="utf8") as stream:
            json.dump(manifest, stream, indent=2)
        return manifest
    except Exception:
        if created:
            output.unlink(missing_ok=True)
        raise
    finally:
        db.dispose()


def verify(archive: Path, database: str, pg_bin: str | None = None) -> dict[str, Any]:
    # Verification never restores over the live database or any existing database.
    if not re.fullmatch(r"findcar_restore_[a-z0-9_]{1,40}", database):
        raise ValueError("Use a new database named findcar_restore_<suffix>")
    url = make_url(settings().database_url)
    if database == url.database:
        raise ValueError("Cannot use the current application database")
    manifest = json.loads(archive.with_suffix(archive.suffix + ".json").read_text(encoding="utf8"))
    if manifest.get("format") != 1 or manifest["archive_sha256"] != file_hash(archive):
        raise ValueError("Backup checksum does not match")
    maintenance = sa.create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    restored = sa.create_engine(url.set(database=database))
    created = False
    try:
        with maintenance.connect() as conn:
            if conn.execute(
                sa.text("SELECT 1 FROM pg_database WHERE datname=:name"), {"name": database}
            ).first():
                raise ValueError("Verification database already exists; refusing to overwrite")
            conn.execute(sa.text(f'CREATE DATABASE "{database}" TEMPLATE template0'))
            created = True
        subprocess.run(
            [
                utility("pg_restore", pg_bin),
                "--exit-on-error",
                "--single-transaction",
                "--no-owner",
                "--no-acl",
                "--dbname=" + database,
                str(archive.resolve()),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=pg_environment(url, database),
            check=True,
            timeout=300,
        )
        with restored.connect() as conn:
            conn.execute(sa.text("SET TIME ZONE 'UTC'"))
            actual = table_manifest(conn)
        if actual != manifest["tables"]:
            raise ValueError("Restored rows differ from the consistent backup snapshot")
        return {"verified": True, "tables": len(actual), "rows": sum(t["rows"] for t in actual.values())}
    finally:
        restored.dispose()
        if created:
            with maintenance.connect() as conn:
                conn.execute(sa.text(f'DROP DATABASE "{database}"'))
        maintenance.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["create", "verify"])
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument("--pg-bin")
    parser.add_argument("--database", default="findcar_restore_verify")
    args = parser.parse_args()
    try:
        if args.action == "create":
            result = backup(args.file, args.pg_bin)
            print(json.dumps({"created": True, "tables": len(result["tables"])}))
        else:
            print(json.dumps(verify(args.file, args.database, args.pg_bin)))
    except subprocess.CalledProcessError:
        parser.exit(1, "PostgreSQL utility failed; check permissions, disk space and PostgreSQL version.\n")
    except (ValueError, OSError, sa.exc.SQLAlchemyError, subprocess.TimeoutExpired) as exc:
        # No connection strings, passwords or raw database errors in console output.
        parser.exit(
            1,
            f"Backup operation failed ({type(exc).__name__}). Check paths, permissions and database availability.\n",
        )


if __name__ == "__main__":
    main()
