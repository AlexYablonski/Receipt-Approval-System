"""Alembic environment: tells Alembic which database to connect to and which models to compare against."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine

from app.config import settings
from app.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# What "alembic revision --autogenerate" compares the real database against.
target_metadata = Base.metadata

# Tests set sqlalchemy.url on the Alembic config to point at a temporary database.
# Normally it is unset and the URL comes from DATABASE_URL via app.config.
url = config.get_main_option("sqlalchemy.url") or settings.database_url

# render_as_batch: SQLite can't ALTER most things in place, so Alembic rebuilds the table
#   (create new, copy rows, drop old, rename) whenever a migration changes an existing table.
# compare_type: autogenerate also notices column type changes, not only added or removed columns.
migration_options = {"target_metadata": target_metadata, "render_as_batch": True, "compare_type": True}


def run_migrations_offline() -> None:
    """Write the SQL to the console instead of running it (alembic upgrade head --sql)."""
    context.configure(url=url, literal_binds=True, **migration_options)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # A plain engine on purpose: it does not switch SQLite foreign keys on (see app/db.py),
    # which keeps table rebuilds in batch mode from tripping over them.
    engine = create_engine(url)
    with engine.connect() as connection:
        context.configure(connection=connection, **migration_options)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
