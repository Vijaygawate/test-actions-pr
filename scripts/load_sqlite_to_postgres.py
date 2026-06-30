"""Load the IVP SQLite seed database into a local PostgreSQL database.

The script reads the bundled SQLite file at data/ivp_local.db, transforms it
into the contract shapes used by the application, and writes those rows into a
PostgreSQL database using the schema defined in shared/data/schema.sql.

Example:
    python scripts/load_sqlite_to_postgres.py

Optional overrides:
    python scripts/load_sqlite_to_postgres.py --sqlite-db data/ivp_local.db \
        --schema shared/data/schema.sql --env-file .env --reset

Connection values can use either DB_* names used by Terraform deployments or
POSTGRES_* names commonly used in local .env files.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SQLITE_DB = REPO_ROOT / "data" / "ivp_local.db"
DEFAULT_SCHEMA_PATH = REPO_ROOT / "shared" / "data" / "schema.sql"
DEFAULT_ENV_PATH = REPO_ROOT / ".env"


def _read_env_file(env_path: str | Path | None = None) -> dict[str, str]:
    path = Path(env_path or DEFAULT_ENV_PATH)
    values: dict[str, str] = {}
    if not path.exists():
        return values

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def load_connection_config(env_path: str | Path | None = None) -> dict[str, Any]:
    env_values = _read_env_file(env_path)
    secret = _load_secret_config(env_values.get("DB_SECRET_ARN") or os.getenv("DB_SECRET_ARN", ""))
    host = (
        env_values.get("DB_HOST")
        or env_values.get("POSTGRES_HOST")
        or os.getenv("DB_HOST")
        or os.getenv("POSTGRES_HOST")
        or secret.get("host")
        or "localhost"
    )
    port = int(
        env_values.get("DB_PORT")
        or env_values.get("POSTGRES_PORT")
        or os.getenv("DB_PORT")
        or os.getenv("POSTGRES_PORT")
        or secret.get("port")
        or "5432"
    )
    dbname = (
        env_values.get("DB_NAME")
        or env_values.get("POSTGRES_DB")
        or env_values.get("POSTGRES_DATABASE")
        or os.getenv("DB_NAME")
        or os.getenv("POSTGRES_DB")
        or os.getenv("POSTGRES_DATABASE")
        or secret.get("dbname")
        or secret.get("database")
        or "postgres"
    )
    user = (
        env_values.get("DB_USER")
        or env_values.get("POSTGRES_USER")
        or os.getenv("DB_USER")
        or os.getenv("POSTGRES_USER")
        or secret.get("username")
        or "postgres"
    )
    password = (
        env_values.get("DB_PASSWORD")
        or env_values.get("POSTGRES_PASSWORD")
        or os.getenv("DB_PASSWORD")
        or os.getenv("POSTGRES_PASSWORD")
        or secret.get("password")
        or "postgres"
    )
    return {"host": host, "port": port, "dbname": dbname, "user": user, "password": password}


def _load_secret_config(secret_arn: str) -> dict[str, Any]:
    if not secret_arn:
        return {}
    try:
        import boto3
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise SystemExit("boto3 is required when DB_SECRET_ARN is set.") from exc

    region = os.getenv("AWS_REGION", "us-east-1")
    client = boto3.client("secretsmanager", region_name=region)
    resp = client.get_secret_value(SecretId=secret_arn)
    return json.loads(resp.get("SecretString") or "{}")


def parse_schema_tables(schema_path: str | Path | None = None) -> list[str]:
    path = Path(schema_path or DEFAULT_SCHEMA_PATH)
    text = path.read_text(encoding="utf-8")
    tables: list[str] = []
    for match in re.finditer(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z0-9_]+)", text, re.IGNORECASE):
        table_name = match.group(1)
        if table_name not in tables:
            tables.append(table_name)
    return tables


def _table_upsert_clause(table_name: str, columns: list[str]) -> tuple[str, list[str]]:
    pk_columns = {
        "transactions": ["txn_id"],
        "cards": ["card_id"],
        "fuel_prices": ["city", "station"],
        "spend_series": ["month"],
        "users": ["id"],
        "auth_credentials": ["user_id"],
    }.get(table_name, [])
    if not pk_columns:
        return (
            f"INSERT INTO {table_name} ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(columns))})",
            columns,
        )

    conflict_target = ", ".join(pk_columns)
    updates = ", ".join(f"{col}=EXCLUDED.{col}" for col in columns if col not in pk_columns)
    sql = (
        f"INSERT INTO {table_name} ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(columns))}) "
        f"ON CONFLICT ({conflict_target}) DO UPDATE SET {updates}"
    )
    return sql, columns


def _split_sql_statements(sql_text: str) -> list[str]:
    statements: list[str] = []
    buffer: list[str] = []
    in_single = False
    in_double = False
    for char in sql_text:
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        if char == ";" and not in_single and not in_double:
            statement = "".join(buffer).strip()
            if statement:
                statements.append(statement)
            buffer = []
            continue
        buffer.append(char)
    tail = "".join(buffer).strip()
    if tail:
        statements.append(tail)
    return statements


def _execute_sql_script(conn: Any, sql_text: str) -> None:
    with conn.cursor() as cur:
        for statement in _split_sql_statements(sql_text):
            cur.execute(statement)


def ensure_database_exists(conn_config: dict[str, Any]) -> None:
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise SystemExit("psycopg is required. Install it with: pip install 'psycopg[binary]'") from exc

    admin_config = dict(conn_config)
    admin_config["dbname"] = "postgres"
    conn = psycopg.connect(**admin_config)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (conn_config["dbname"],))
            exists = cur.fetchone()
        if not exists:
            with conn.cursor() as cur:
                cur.execute(f'CREATE DATABASE "{conn_config["dbname"]}"')
    finally:
        conn.close()


def _write_rows(conn: Any, table_name: str, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    columns = list(rows[0].keys())
    sql, _ = _table_upsert_clause(table_name, columns)
    with conn.cursor() as cur:
        for row in rows:
            values = [row.get(col) for col in columns]
            cur.execute(sql, values)


def load_sqlite_to_postgres(
    sqlite_db: str | Path | None = None,
    schema_path: str | Path | None = None,
    env_path: str | Path | None = None,
    reset: bool = False,
    dry_run: bool = False,
) -> dict[str, int]:
    sqlite_path = Path(sqlite_db or DEFAULT_SQLITE_DB)
    if not sqlite_path.exists():
        raise FileNotFoundError(f"SQLite DB not found: {sqlite_path}")

    schema_path = Path(schema_path or DEFAULT_SCHEMA_PATH)
    if not schema_path.exists():
        raise FileNotFoundError(f"Schema file not found: {schema_path}")

    # Import the existing ETL builder so the import matches the contract shape.
    sys.path.insert(0, str(REPO_ROOT))
    from scripts.etl_from_sqlite import build_all

    data = build_all(str(sqlite_path))
    table_names = [name for name in parse_schema_tables(schema_path) if name in data]

    if dry_run:
        return {name: len(data[name]) for name in table_names}

    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise SystemExit("psycopg is required. Install it with: pip install 'psycopg[binary]'") from exc

    conn_config = load_connection_config(env_path)
    ensure_database_exists(conn_config)
    conn = psycopg.connect(**conn_config)
    try:
        schema_sql = schema_path.read_text(encoding="utf-8")
        _execute_sql_script(conn, schema_sql)
        if reset:
            with conn.cursor() as cur:
                for name in table_names:
                    cur.execute(f"TRUNCATE TABLE {name} RESTART IDENTITY CASCADE")
        for table_name in table_names:
            _write_rows(conn, table_name, data[table_name])
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return {name: len(data[name]) for name in table_names}


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite-db", default=str(DEFAULT_SQLITE_DB), help="Path to the SQLite DB to import")
    parser.add_argument("--schema", default=str(DEFAULT_SCHEMA_PATH), help="Path to the PostgreSQL schema SQL file")
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_PATH), help="Path to the .env file with PostgreSQL credentials")
    parser.add_argument("--reset", action="store_true", help="Truncate target tables before loading")
    parser.add_argument("--dry-run", action="store_true", help="Build the ETL payload without connecting to PostgreSQL")
    return parser


def main() -> int:
    parser = build_argument_parser()
    args = parser.parse_args()
    result = load_sqlite_to_postgres(
        sqlite_db=args.sqlite_db,
        schema_path=args.schema,
        env_path=args.env_file,
        reset=args.reset,
        dry_run=args.dry_run,
    )
    for table_name, count in result.items():
        print(f"{table_name}: {count} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
