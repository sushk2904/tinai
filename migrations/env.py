"""
migrations/env.py — Alembic migration environment (Phase 1.3)

Two critical configurations applied here that Alembic's default template
does NOT include:

1.  URL override from ALEMBIC_DATABASE_URL env var (never read from alembic.ini)
    — alembic.ini is committed to git; credentials must never live there.

2.  target_metadata wired to api.models.Base.metadata
    — without this, `alembic revision --autogenerate` generates empty files
    and never sees your ORM models.
"""

import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

# ---------------------------------------------------------------------------
# Alembic config object — gives access to values in alembic.ini
# ---------------------------------------------------------------------------
config = context.config

# Wire up Python logging from alembic.ini [loggers] section
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ---------------------------------------------------------------------------
# Override sqlalchemy.url from environment (TODO §1.3: never hardcode creds)
# ALEMBIC_DATABASE_URL uses the synchronous postgresql:// driver because
# Alembic's migration runner is synchronous (not asyncpg-compatible).
# ---------------------------------------------------------------------------
db_url = os.environ.get("ALEMBIC_DATABASE_URL")
if not db_url:
    raise RuntimeError(
        "ALEMBIC_DATABASE_URL is not set. "
        "Ensure your .env file has a valid synchronous postgresql:// URL."
    )
config.set_main_option("sqlalchemy.url", db_url)

# ---------------------------------------------------------------------------
# Import ORM metadata so --autogenerate can diff the DB against our models
# ---------------------------------------------------------------------------
from api.models import Base  # noqa: E402

target_metadata = Base.metadata


# ---------------------------------------------------------------------------
# Offline migrations (generates SQL without a live DB connection)
# ---------------------------------------------------------------------------
def run_migrations_offline() -> None:
    """
    Run migrations in 'offline' mode — outputs raw SQL instead of executing.
    Useful for reviewing what would be applied before touching the DB.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # Render TIMESTAMPTZ correctly for Postgres dialect
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


# ---------------------------------------------------------------------------
# Online migrations (runs against a live Postgres connection)
# ---------------------------------------------------------------------------
def run_migrations_online() -> None:
    """
    Run migrations in 'online' mode — connects to the DB and applies changes.
    This is the path taken by `alembic upgrade head`.
    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        # NullPool: Alembic creates one connection per migration run and
        # closes it immediately. A connection pool is wasteful here.
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # compare_type=True: detects column type changes (e.g., Integer → BigInteger)
            # in addition to structural changes. Essential for catching regressions.
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
