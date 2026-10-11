"""SQL builders for the Row-Level Security pattern of project.md Section 5.

Shared by Alembic migrations so every tenant-scoped table (Phase 1 and later) gets an identical,
reviewed policy.
"""

from __future__ import annotations

import re

TENANT_SETTING = "app.current_tenant_id"
CURRENT_TENANT_SQL = f"NULLIF(current_setting('{TENANT_SETTING}', true), '')::uuid"

_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")


def _ident(name: str) -> str:
    if not _IDENTIFIER.match(name):
        raise ValueError(f"Unsafe SQL identifier: {name!r}")
    return name


def tenant_isolation_policy_name(table: str) -> str:
    return f"tenant_isolation_{_ident(table)}"


def enable_tenant_rls_sql(table: str, tenant_column: str = "tenant_id") -> list[str]:
    """Enable RLS on ``table`` and restrict reads *and* writes to the current tenant."""
    table, column = _ident(table), _ident(tenant_column)
    predicate = f"{column} = {CURRENT_TENANT_SQL}"
    return [
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"CREATE POLICY {tenant_isolation_policy_name(table)} ON {table} "
        f"USING ({predicate}) WITH CHECK ({predicate})",
    ]


def disable_tenant_rls_sql(table: str) -> list[str]:
    table = _ident(table)
    return [
        f"DROP POLICY IF EXISTS {tenant_isolation_policy_name(table)} ON {table}",
        f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY",
    ]
