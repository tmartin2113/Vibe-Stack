"""Database Tool — inspect schemas, run queries, and check migrations."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
from typing import Any, Dict, List, Optional

from .registry import Tool, ToolCategory, ToolResult

logger = logging.getLogger(__name__)

# Hard ceiling to prevent unbounded result sets
_MAX_ROWS = 500
_QUERY_TIMEOUT = 30

# Statements that modify data — blocked unless allow_writes is True
_WRITE_PREFIXES = (
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "TRUNCATE",
    "GRANT", "REVOKE", "VACUUM", "REINDEX",
)


class DatabaseTool(Tool):
    """Inspect database schemas and run queries.

    Supports PostgreSQL (via ``psql``) and SQLite (via Python stdlib).
    Connection is specified per-call or via the ``DATABASE_URL`` env var.

    Actions:
        schema   — list tables and their columns
        query    — run a SQL query (read-only by default)
        explain  — show the query execution plan
        tables   — list table names only
    """

    def __init__(self):
        super().__init__(
            name="database",
            description=(
                "Inspect database schemas and run SQL queries. Supports PostgreSQL "
                "and SQLite. Use 'schema' to list tables/columns, 'query' to run "
                "SELECT statements, 'explain' for query plans, 'tables' to list tables."
            ),
            category=ToolCategory.SPECIALIZED,
        )

    def _get_parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "Action: 'tables', 'schema', 'query', or 'explain'",
                },
                "connection": {
                    "type": "string",
                    "description": (
                        "Connection string. PostgreSQL: 'postgres://user:pass@host:port/db'. "
                        "SQLite: '/path/to/file.db'. Defaults to DATABASE_URL env var."
                    ),
                },
                "sql": {
                    "type": "string",
                    "description": "SQL query (required for 'query' and 'explain' actions)",
                },
                "table": {
                    "type": "string",
                    "description": "Table name (optional for 'schema' — show only this table)",
                },
                "limit": {
                    "type": "integer",
                    "description": f"Max rows to return (default 100, max {_MAX_ROWS})",
                    "default": 100,
                },
                "allow_writes": {
                    "type": "boolean",
                    "description": "Allow INSERT/UPDATE/DELETE etc. (default: false)",
                    "default": False,
                },
            },
            "required": ["action"],
        }

    def execute(  # type: ignore[override]
        self,
        action: str,
        connection: str = "",
        sql: str = "",
        table: str = "",
        limit: int = 100,
        allow_writes: bool = False,
        **kwargs: Any,
    ) -> ToolResult:
        if not action or action not in ("tables", "schema", "query", "explain"):
            return ToolResult(
                success=False, output="",
                error="Invalid action. Must be one of: tables, schema, query, explain",
            )

        conn = connection or os.environ.get("DATABASE_URL", "")
        if not conn:
            return ToolResult(
                success=False, output="",
                error="No connection string provided and DATABASE_URL not set.",
            )

        limit = min(max(limit, 1), _MAX_ROWS)

        # Route to the right backend
        if conn.endswith(".db") or conn.endswith(".sqlite") or conn.startswith("sqlite:"):
            return self._handle_sqlite(action, conn, sql, table, limit, allow_writes)
        elif conn.startswith(("postgres://", "postgresql://")):
            return self._handle_postgres(action, conn, sql, table, limit, allow_writes)
        else:
            return ToolResult(
                success=False, output="",
                error=f"Unsupported connection string format. Use postgres://... or /path/to.db",
            )

    # ── SQLite ────────────────────────────────────────────────────────

    def _handle_sqlite(
        self, action: str, conn: str, sql: str, table: str, limit: int, allow_writes: bool,
    ) -> ToolResult:
        path = conn.replace("sqlite:", "").replace("//", "")
        if not os.path.exists(path):
            return ToolResult(success=False, output="", error=f"SQLite file not found: {path}")

        try:
            db = sqlite3.connect(path, timeout=_QUERY_TIMEOUT)
            db.row_factory = sqlite3.Row
            cur = db.cursor()

            if action == "tables":
                result = self._sqlite_tables(cur)
            elif action == "schema":
                result = self._sqlite_schema(cur, table)
            elif action == "query":
                result = self._sqlite_query(cur, sql, limit, allow_writes)
            elif action == "explain":
                result = self._sqlite_query(cur, f"EXPLAIN QUERY PLAN {sql}", limit, False)
            else:
                result = ToolResult(success=False, output="", error=f"Unknown action: {action}")

            db.close()
            return result

        except Exception as e:
            return ToolResult(success=False, output="", error=f"SQLite error: {e}")

    def _sqlite_tables(self, cur: sqlite3.Cursor) -> ToolResult:
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = [row[0] for row in cur.fetchall()]
        return ToolResult(
            success=True,
            output="\n".join(tables) if tables else "No tables found.",
            metadata={"count": len(tables)},
        )

    def _sqlite_schema(self, cur: sqlite3.Cursor, table: str) -> ToolResult:
        if table:
            cur.execute(f"PRAGMA table_info({table})")
            cols = cur.fetchall()
            if not cols:
                return ToolResult(success=False, output="", error=f"Table not found: {table}")
            lines = [f"## {table}\n"]
            lines.append("| # | Column | Type | Nullable | Default | PK |")
            lines.append("|---|--------|------|----------|---------|-----|")
            for c in cols:
                nullable = "YES" if not c[3] else "NO"
                pk = "YES" if c[5] else ""
                lines.append(f"| {c[0]} | {c[1]} | {c[2]} | {nullable} | {c[4] or ''} | {pk} |")
            return ToolResult(success=True, output="\n".join(lines), metadata={"table": table})

        # All tables
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = [row[0] for row in cur.fetchall()]
        output_parts = []
        for t in tables:
            cur.execute(f"PRAGMA table_info({t})")
            cols = cur.fetchall()
            col_defs = ", ".join(f"{c[1]} {c[2]}" for c in cols)
            output_parts.append(f"**{t}** ({len(cols)} cols): {col_defs}")
        return ToolResult(
            success=True,
            output="\n\n".join(output_parts) if output_parts else "No tables found.",
            metadata={"table_count": len(tables)},
        )

    def _sqlite_query(
        self, cur: sqlite3.Cursor, sql: str, limit: int, allow_writes: bool,
    ) -> ToolResult:
        if not sql or not sql.strip():
            return ToolResult(success=False, output="", error="No SQL query provided")

        normalized = sql.strip().upper()
        if not allow_writes:
            for prefix in _WRITE_PREFIXES:
                if normalized.startswith(prefix):
                    return ToolResult(
                        success=False, output="",
                        error=f"Write operation '{prefix}' blocked. Set allow_writes=true to enable.",
                    )

        # Inject LIMIT if not present on SELECT
        query = sql.strip()
        if normalized.startswith("SELECT") and "LIMIT" not in normalized:
            query = f"{query} LIMIT {limit}"

        try:
            cur.execute(query)
            if cur.description is None:
                # Non-SELECT statement
                return ToolResult(
                    success=True,
                    output=f"Statement executed. Rows affected: {cur.rowcount}",
                    metadata={"rowcount": cur.rowcount},
                )

            columns = [d[0] for d in cur.description]
            rows = cur.fetchmany(limit)
            return self._format_rows(columns, [tuple(r) for r in rows], limit)

        except Exception as e:
            return ToolResult(success=False, output="", error=f"Query error: {e}")

    # ── PostgreSQL ────────────────────────────────────────────────────

    def _handle_postgres(
        self, action: str, conn: str, sql: str, table: str, limit: int, allow_writes: bool,
    ) -> ToolResult:
        if action == "tables":
            return self._pg_run(conn, (
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname = 'public' ORDER BY tablename"
            ))
        elif action == "schema":
            if table:
                return self._pg_run(conn, (
                    "SELECT column_name, data_type, is_nullable, column_default "
                    f"FROM information_schema.columns WHERE table_name = '{self._pg_escape(table)}' "
                    "AND table_schema = 'public' ORDER BY ordinal_position"
                ))
            else:
                return self._pg_run(conn, (
                    "SELECT table_name, column_name, data_type, is_nullable "
                    "FROM information_schema.columns "
                    "WHERE table_schema = 'public' "
                    "ORDER BY table_name, ordinal_position"
                ))
        elif action == "query":
            return self._pg_query(conn, sql, limit, allow_writes)
        elif action == "explain":
            if not sql or not sql.strip():
                return ToolResult(success=False, output="", error="No SQL query provided")
            return self._pg_run(conn, f"EXPLAIN ANALYZE {sql}")
        else:
            return ToolResult(success=False, output="", error=f"Unknown action: {action}")

    def _pg_escape(self, value: str) -> str:
        """Basic escape for values interpolated into metadata queries only."""
        return value.replace("'", "''").replace("\\", "\\\\").replace(";", "")

    def _pg_query(
        self, conn: str, sql: str, limit: int, allow_writes: bool,
    ) -> ToolResult:
        if not sql or not sql.strip():
            return ToolResult(success=False, output="", error="No SQL query provided")

        normalized = sql.strip().upper()
        if not allow_writes:
            for prefix in _WRITE_PREFIXES:
                if normalized.startswith(prefix):
                    return ToolResult(
                        success=False, output="",
                        error=f"Write operation '{prefix}' blocked. Set allow_writes=true to enable.",
                    )

        # Inject LIMIT if not present on SELECT
        query = sql.strip().rstrip(";")
        if normalized.startswith("SELECT") and "LIMIT" not in normalized:
            query = f"{query} LIMIT {limit}"

        return self._pg_run(conn, query)

    def _pg_run(self, conn: str, sql: str) -> ToolResult:
        """Execute SQL via psql and return formatted output."""
        try:
            result = subprocess.run(
                [
                    "psql", conn,
                    "--no-psqlrc",
                    "-P", "pager=off",
                    "-P", "format=csv",
                    "-c", sql,
                ],
                capture_output=True,
                text=True,
                timeout=_QUERY_TIMEOUT,
                env={
                    "PATH": os.environ.get("PATH", "/usr/bin:/usr/local/bin"),
                    "HOME": os.environ.get("HOME", "/tmp"),
                    # Suppress password prompt — connection string has credentials
                    "PGCONNECT_TIMEOUT": "10",
                },
            )

            if result.returncode != 0:
                error = result.stderr.strip()
                return ToolResult(success=False, output="", error=f"psql error: {error}")

            output = result.stdout.strip()
            if not output:
                return ToolResult(success=True, output="Query returned no results.", metadata={})

            # Parse CSV output into markdown table
            lines = output.split("\n")
            if len(lines) >= 1:
                return self._csv_to_markdown(lines)

            return ToolResult(success=True, output=output, metadata={})

        except FileNotFoundError:
            return ToolResult(
                success=False, output="",
                error="psql not found. Install postgresql-client.",
            )
        except subprocess.TimeoutExpired:
            return ToolResult(
                success=False, output="",
                error=f"Query timed out after {_QUERY_TIMEOUT}s",
            )
        except Exception as e:
            return ToolResult(success=False, output="", error=f"PostgreSQL error: {e}")

    # ── Formatting ────────────────────────────────────────────────────

    def _csv_to_markdown(self, lines: List[str]) -> ToolResult:
        """Convert psql CSV output to a markdown table."""
        import csv
        import io

        reader = csv.reader(io.StringIO("\n".join(lines)))
        rows = list(reader)
        if not rows:
            return ToolResult(success=True, output="No results.", metadata={})

        headers = rows[0]
        data = rows[1:]

        # Build markdown table
        md_lines = []
        md_lines.append("| " + " | ".join(headers) + " |")
        md_lines.append("| " + " | ".join("---" for _ in headers) + " |")
        for row in data:
            # Pad row to match headers if needed
            padded = row + [""] * (len(headers) - len(row))
            md_lines.append("| " + " | ".join(padded[:len(headers)]) + " |")

        return ToolResult(
            success=True,
            output="\n".join(md_lines),
            metadata={"row_count": len(data), "columns": headers},
        )

    def _format_rows(
        self, columns: List[str], rows: List[tuple], limit: int,
    ) -> ToolResult:
        """Format SQLite rows as a markdown table."""
        md_lines = []
        md_lines.append("| " + " | ".join(columns) + " |")
        md_lines.append("| " + " | ".join("---" for _ in columns) + " |")
        for row in rows:
            md_lines.append("| " + " | ".join(str(v) if v is not None else "NULL" for v in row) + " |")

        truncated = len(rows) >= limit
        output = "\n".join(md_lines)
        if truncated:
            output += f"\n\n*Results limited to {limit} rows.*"

        return ToolResult(
            success=True,
            output=output,
            metadata={"row_count": len(rows), "columns": columns, "truncated": truncated},
        )
