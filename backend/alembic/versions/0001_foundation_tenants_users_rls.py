"""Phase 1 foundation: extensions, tenants, users, Row-Level Security, application role.

Revision ID: 0001_foundation
Revises:
Create Date: 2026-10-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import make_url

from app.core.config import get_settings
from app.core.rls import disable_tenant_rls_sql, enable_tenant_rls_sql
from app.models.tenant import HEX_COLOR_PATTERN, SUBDOMAIN_PATTERN

revision: str = "0001_foundation"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REQUIRED_EXTENSIONS = ("vector",)
# Present in the pgvector/pgvector:pg16 image. Not strictly required: gen_random_uuid() has been
# part of PostgreSQL core since v13, so they are enabled only where the server provides them.
OPTIONAL_EXTENSIONS = ("uuid-ossp", "pgcrypto")

USER_ROLES = ("admin", "recruiter", "hiring_manager", "interviewer")

# SECURITY DEFINER lookups that run as the migration (owner) role and therefore bypass RLS.
# They expose only a tenant id for a given identifier, which is what the API needs to decide
# which tenant to bind (SET LOCAL) before RLS-protected queries can see anything at all.
RESOLVER_FUNCTIONS = {
    "app_resolve_tenant_by_clerk_org": "clerk_org_id = p_key",
    "app_resolve_tenant_by_subdomain": "subdomain = lower(p_key)",
    "app_resolve_tenant_by_custom_domain": "custom_domain = lower(p_key)",
}


def _app_role() -> tuple[str, str]:
    url = make_url(get_settings().DATABASE_URL)
    role, password = url.username or "", url.password or ""
    if not role.replace("_", "").isalnum() or not role[0].isalpha():
        raise RuntimeError(f"Unsafe application role name in DATABASE_URL: {role!r}")
    return role, password


def upgrade() -> None:
    for ext in REQUIRED_EXTENSIONS:
        op.execute(f'CREATE EXTENSION IF NOT EXISTS "{ext}"')
    for ext in OPTIONAL_EXTENSIONS:
        op.execute(
            f"""
            DO $ext$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = '{ext}') THEN
                    CREATE EXTENSION IF NOT EXISTS "{ext}";
                ELSE
                    RAISE NOTICE 'extension "{ext}" is not available on this server; skipped';
                END IF;
            END
            $ext$;
            """
        )

    user_role = postgresql.ENUM(*USER_ROLES, name="user_role")
    user_role.create(op.get_bind())

    op.create_table(
        "tenants",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("clerk_org_id", sa.String(64), nullable=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("subdomain", sa.String(63), nullable=False),
        sa.Column("custom_domain", sa.String(255), nullable=True),
        sa.Column("logo_url", sa.Text(), nullable=True),
        sa.Column(
            "primary_color", sa.String(7), nullable=False, server_default=sa.text("'#2563EB'")
        ),
        sa.Column(
            "settings_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("clerk_org_id", name="uq_tenants_clerk_org_id"),
        sa.UniqueConstraint("subdomain", name="uq_tenants_subdomain"),
        sa.UniqueConstraint("custom_domain", name="uq_tenants_custom_domain"),
        sa.CheckConstraint(
            f"subdomain ~ '{SUBDOMAIN_PATTERN}'", name="ck_tenants_subdomain_format"
        ),
        sa.CheckConstraint(
            f"primary_color ~ '{HEX_COLOR_PATTERN}'", name="ck_tenants_primary_color_format"
        ),
    )

    op.create_table(
        "users",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE", name="fk_users_tenant_id_tenants"),
            nullable=False,
        ),
        sa.Column("clerk_user_id", sa.String(64), nullable=True),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("full_name", sa.String(255), nullable=False),
        sa.Column(
            "role",
            postgresql.ENUM(name="user_role", create_type=False),
            nullable=False,
            server_default=sa.text("'recruiter'"),
        ),
        sa.Column("department", sa.String(100), nullable=True),
        sa.Column("avatar_url", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("tenant_id", "email", name="uq_tenant_user_email"),
        sa.UniqueConstraint("tenant_id", "clerk_user_id", name="uq_tenant_user_clerk_id"),
    )
    op.create_index("idx_users_tenant_role", "users", ["tenant_id", "role"])

    # ---- Row-Level Security (project.md Section 5) --------------------------------------
    for statement in enable_tenant_rls_sql("tenants", tenant_column="id"):
        op.execute(statement)
    for statement in enable_tenant_rls_sql("users"):
        op.execute(statement)

    for function_name, predicate in RESOLVER_FUNCTIONS.items():
        op.execute(
            f"""
            CREATE FUNCTION {function_name}(p_key text)
            RETURNS uuid
            LANGUAGE sql STABLE SECURITY DEFINER
            SET search_path = pg_catalog, public
            AS $fn$ SELECT id FROM public.tenants WHERE {predicate} $fn$
            """
        )
        op.execute(f"REVOKE ALL ON FUNCTION {function_name}(text) FROM PUBLIC")

    # ---- Restricted runtime role (no SUPERUSER / BYPASSRLS / ownership) ---------------------
    role, password = _app_role()
    admin_role = op.get_bind().execute(sa.text("SELECT current_user")).scalar_one()
    if role == admin_role:
        raise RuntimeError(
            "DATABASE_URL and DATABASE_ADMIN_URL use the same role; the runtime role would own "
            "the tables and bypass Row-Level Security."
        )
    escaped_password = password.replace("'", "''")
    op.execute(
        f"""
        DO $role$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                CREATE ROLE {role} LOGIN PASSWORD '{escaped_password}'
                    NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
            END IF;
            EXECUTE format('GRANT CONNECT ON DATABASE %I TO {role}', current_database());
        END
        $role$;
        """
    )
    op.execute(f"GRANT USAGE ON SCHEMA public TO {role}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON tenants, users TO {role}")
    for function_name in RESOLVER_FUNCTIONS:
        op.execute(f"GRANT EXECUTE ON FUNCTION {function_name}(text) TO {role}")
    # Tables/sequences created by later migrations are automatically usable by the app role.
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {role}"
    )
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO {role}"
    )


def downgrade() -> None:
    role, _ = _app_role()
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM {role}"
    )
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE USAGE, SELECT ON SEQUENCES FROM {role}"
    )
    for function_name in RESOLVER_FUNCTIONS:
        op.execute(f"DROP FUNCTION IF EXISTS {function_name}(text)")
    for table in ("users", "tenants"):
        for statement in disable_tenant_rls_sql(table):
            op.execute(statement)
    op.drop_index("idx_users_tenant_role", table_name="users")
    op.drop_table("users")
    op.drop_table("tenants")
    postgresql.ENUM(name="user_role").drop(op.get_bind())
    # The cluster-wide application role and the extensions are intentionally left in place:
    # other databases on the same server (e.g. the test database) may depend on them.
