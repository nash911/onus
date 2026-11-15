"""Copy every Onus table from one database to another (SQLite -> Supabase Postgres), table by table.

    poetry run python scripts/migrate_sqlite_to_postgres.py \\
        --from sqlite:////data/onus.db --to "postgresql+psycopg://USER:PASS@HOST:5432/postgres?sslmode=require"

Tables are copied in foreign-key order (papers, analyses, disputes, live_usage, live_requests) using the same
SQLAlchemy models, so UUID strings, UTC datetimes and JSON (-> JSONB) round-trip unchanged. The target schema
must already exist (apply db/migrations/001_init.sql in the Supabase SQL editor first), or pass --create.

Rows whose primary key already exists in the target are skipped, so the script can be re-run after a partial
copy. `--dry-run` only counts. Passwords are never printed.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from sqlalchemy import Engine, func, inspect, select, tuple_
from sqlalchemy.engine import make_url

from onus.db.models import Base
from onus.db.session import make_engine

BATCH = 500


def safe(url: str) -> str:
    return make_url(url).render_as_string(hide_password=True)


def copy_all(src: Engine, dst: Engine, *, dry_run: bool = False, create: bool = False,
             batch: int = BATCH, log=print) -> dict[str, dict[str, int]]:
    if create and not dry_run:
        Base.metadata.create_all(dst)
    missing = [t.name for t in Base.metadata.sorted_tables if not inspect(dst).has_table(t.name)]
    if missing and not dry_run:
        raise SystemExit(f"target is missing tables {missing}: apply db/migrations/001_init.sql or pass --create")
    report: dict[str, dict[str, int]] = {}
    with src.connect() as sc, dst.connect() as dc:
        for table in Base.metadata.sorted_tables:
            pk = list(table.primary_key.columns)
            n_src = sc.scalar(select(func.count()).select_from(table)) or 0
            copied = skipped = 0
            if not dry_run and n_src:
                result = sc.execute(select(table).order_by(*pk))
                while rows := result.fetchmany(batch):
                    dicts = [dict(r._mapping) for r in rows]
                    keys = [tuple(d[c.name] for c in pk) for d in dicts]
                    existing = set(dc.execute(select(*pk).where(tuple_(*pk).in_(keys))).all()) if keys else set()
                    new = [d for d, k in zip(dicts, keys, strict=True) if k not in existing]
                    if new:
                        dc.execute(table.insert(), new)
                    copied += len(new)
                    skipped += len(dicts) - len(new)
                dc.commit()
            report[table.name] = {"source": int(n_src), "copied": copied, "skipped": skipped}
            log(f"{table.name:14s} source={n_src:6d} copied={copied:6d} skipped={skipped:6d}")
    return report


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--from", dest="src", required=True, help="source URL, e.g. sqlite:////data/onus.db")
    ap.add_argument("--to", dest="dst", required=True, help="target URL, e.g. postgresql+psycopg://...")
    ap.add_argument("--create", action="store_true", help="create missing tables in the target")
    ap.add_argument("--dry-run", action="store_true", help="count source rows only")
    args = ap.parse_args(argv)
    print(f"from {safe(args.src)}\nto   {safe(args.dst)}")
    src, dst = make_engine(args.src), make_engine(args.dst)
    try:
        copy_all(src, dst, dry_run=args.dry_run, create=args.create)
    finally:
        src.dispose()
        dst.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(main())
