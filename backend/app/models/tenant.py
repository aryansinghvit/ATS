from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import CheckConstraint, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

SUBDOMAIN_PATTERN = r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$"
HEX_COLOR_PATTERN = r"^#[0-9A-Fa-f]{6}$"


class Tenant(TimestampMixin, Base):
    """A company workspace. Row-Level Security restricts every session to its own tenant row."""

    __tablename__ = "tenants"
    __table_args__ = (
        CheckConstraint(f"subdomain ~ '{SUBDOMAIN_PATTERN}'", name="subdomain_format"),
        CheckConstraint(f"primary_color ~ '{HEX_COLOR_PATTERN}'", name="primary_color_format"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    # Clerk organization id (e.g. "org_2abc..."); the identity-provider handle for this tenant.
    clerk_org_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    subdomain: Mapped[str] = mapped_column(String(63), nullable=False, unique=True)
    custom_domain: Mapped[str | None] = mapped_column(String(255), unique=True)
    logo_url: Mapped[str | None] = mapped_column(Text)
    primary_color: Mapped[str] = mapped_column(
        String(7), nullable=False, server_default=text("'#2563EB'")
    )
    settings_json: Mapped[dict[str, Any]] = mapped_column(
        nullable=False, server_default=text("'{}'::jsonb")
    )
