"""Outils bases de données de l'agent : SQLite (stdlib) et PostgreSQL (psycopg2).

Sécurité :
    - SQLite : chemin confiné à la sandbox ; mode lecture seule par défaut
      (PRAGMA query_only) — toute écriture est rejetée par la base elle-même ;
    - PostgreSQL : DSN lu dans l'argument `dsn` ou la variable AGENT_PG_DSN ;
      filtre anti-écriture par mot-clé quand readonly=true (défaut) ;
      statement_timeout appliqué à chaque session.
"""

import os
import re
import sqlite3
from pathlib import Path

from tools.sandbox import safe_resolve

DEFAULT_MAX_ROWS = 50
PG_CONNECT_TIMEOUT_S = 10

# Mots-clés de modification de données, détectés sur la requête entière
# (couvre aussi les CTE "WITH ... DELETE").
_WRITE_KEYWORDS = re.compile(
    r"\b(insert|update|delete|drop|alter|truncate|create|grant|revoke|copy|"
    r"vacuum|analyze|comment|call|do|reindex|lock)\b",
    re.IGNORECASE,
)


def _assert_readonly_sql(query: str, engine: str) -> None:
    match = _WRITE_KEYWORDS.search(query or "")
    if match:
        raise PermissionError(
            f"[{engine}] Requête d'écriture refusée en mode lecture seule : "
            f"mot-clé interdit '{match.group(1).upper()}'. "
            "Relancez avec readonly=false si l'écriture est voulue."
        )


def _cell(value):
    """Rend une cellule sérialisable (bytes -> str)."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


# --- SQLite ----------------------------------------------------------------------
def sqlite_query(db_path: str, query: str, readonly: bool = True,
                 max_rows: int = DEFAULT_MAX_ROWS) -> dict:
    """Exécute une requête SQL sur une base SQLite située dans la sandbox.

    readonly=true (défaut) : connexion en lecture seule stricte (PRAGMA
    query_only) — INSERT/UPDATE/DDL refusés. Mettre readonly=false pour
    écrire (création de base autorisée si le parent existe).
    """
    db_file: Path = safe_resolve(db_path, must_exist=bool(readonly))
    if readonly and not db_file.is_file():
        raise FileNotFoundError(f"Base SQLite introuvable : {db_file}")

    if readonly:
        _assert_readonly_sql(query, "SQLite")

    conn = sqlite3.connect(str(db_file))
    try:
        if readonly:
            conn.execute("PRAGMA query_only = ON")
        cur = conn.execute(query)

        result: dict = {"db": str(db_file), "readonly": bool(readonly)}
        if cur.description is not None:
            columns = [col[0] for col in cur.description]
            rows = cur.fetchmany(max(1, int(max_rows)) + 1)
            truncated = len(rows) > int(max_rows)
            result.update(
                {
                    "columns": columns,
                    "rows": [[_cell(v) for v in row] for row in rows[: int(max_rows)]],
                    "row_count": min(len(rows), int(max_rows)),
                    "truncated": truncated,
                }
            )
        else:
            result["rows_affected"] = cur.rowcount
        if not readonly:
            conn.commit()
        return result
    except sqlite3.Error as exc:
        if readonly:
            raise RuntimeError(f"SQLite (lecture seule) : {exc}") from exc
        raise RuntimeError(f"SQLite : {exc}") from exc
    finally:
        conn.close()


# --- PostgreSQL ---------------------------------------------------------------------
def postgres_query(query: str, dsn: str | None = None, readonly: bool = True,
                   max_rows: int = DEFAULT_MAX_ROWS, timeout_s: float = 30) -> dict:
    """Exécute une requête SQL sur PostgreSQL.

    DSN : argument `dsn` ou variable d'environnement AGENT_PG_DSN, ex. :
        postgresql://user:password@localhost:5432/mabase
    Nécessite `pip install psycopg2-binary` (voir requirements.txt).
    """
    dsn = dsn or os.getenv("AGENT_PG_DSN")
    if not dsn:
        raise RuntimeError(
            "DSN PostgreSQL manquant : passez 'dsn' ou définissez la variable "
            "d'environnement AGENT_PG_DSN (ex. postgresql://user:pwd@host:5432/base)."
        )

    try:
        import psycopg2
        import psycopg2.extras
    except ImportError as exc:
        raise RuntimeError(
            "psycopg2 n'est pas installé : lancez 'pip install psycopg2-binary'."
        ) from exc

    if readonly:
        _assert_readonly_sql(query, "PostgreSQL")
    timeout_s = max(1.0, min(float(timeout_s), 300.0))

    conn = psycopg2.connect(dsn, connect_timeout=PG_CONNECT_TIMEOUT_S)
    try:
        with conn.cursor() as cur:
            # statement_timeout : entier construit localement -> pas d'injection
            cur.execute(f"SET statement_timeout = {int(timeout_s * 1000)}")
            cur.execute(query)

            result: dict = {"readonly": bool(readonly), "statement_timeout_s": timeout_s}
            if cur.description is not None:
                columns = [desc[0] for desc in cur.description]
                rows = cur.fetchmany(max(1, int(max_rows)) + 1)
                result.update(
                    {
                        "columns": columns,
                        "rows": [
                            [_cell(v) for v in row] for row in rows[: int(max_rows)]
                        ],
                        "row_count": min(len(rows), int(max_rows)),
                        "truncated": len(rows) > int(max_rows),
                    }
                )
            else:
                result["rows_affected"] = cur.rowcount
            if not readonly:
                conn.commit()
            return result
    finally:
        conn.close()