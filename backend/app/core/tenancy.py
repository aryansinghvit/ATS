"""Host-header tenant routing: ``acme.<root-domain>`` subdomains and custom enterprise domains."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from app.models.tenant import SUBDOMAIN_PATTERN

_SUBDOMAIN_RE = re.compile(SUBDOMAIN_PATTERN)
_INVALID_SUBDOMAIN_CHARS = re.compile(r"[^a-z0-9-]+")
_HOSTNAME_RE = re.compile(
    r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]([a-z0-9-]{0,61}[a-z0-9])?$"
)


@dataclass(frozen=True, slots=True)
class HostTenantKey:
    kind: Literal["subdomain", "custom_domain"]
    value: str


def _hostname(host: str) -> str | None:
    host = host.strip().lower()
    if not host or host.startswith("["):  # IPv6 literal
        return None
    hostname = host.rsplit(":", 1)[0] if ":" in host else host
    return hostname.rstrip(".") or None


def _is_ip(hostname: str) -> bool:
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return True


def parse_tenant_host(
    host: str | None, *, root_domain: str, reserved_subdomains: Iterable[str]
) -> HostTenantKey | None:
    """Return the tenant identifier implied by a Host header, or None for platform hosts."""
    hostname = _hostname(host or "")
    if hostname is None or _is_ip(hostname) or "." not in hostname or hostname == root_domain:
        return None
    suffix = f".{root_domain}"
    if hostname.endswith(suffix):
        label = hostname[: -len(suffix)]
        if "." in label or label in set(reserved_subdomains) or not _SUBDOMAIN_RE.match(label):
            return None
        return HostTenantKey("subdomain", label)
    return HostTenantKey("custom_domain", hostname)


def normalize_subdomain(raw: str, *, reserved_subdomains: Iterable[str]) -> str:
    """Turn an org slug/name into a valid DNS label, e.g. "Acme Corp!" -> "acme-corp"."""
    candidate = _INVALID_SUBDOMAIN_CHARS.sub("-", raw.strip().lower()).strip("-")[:63].strip("-")
    if not candidate or not _SUBDOMAIN_RE.match(candidate):
        raise ValueError(f"'{raw}' cannot be turned into a valid subdomain")
    if candidate in set(reserved_subdomains):
        raise ValueError(f"Subdomain '{candidate}' is reserved")
    return candidate


def normalize_custom_domain(raw: str) -> str:
    candidate = raw.strip().lower().rstrip(".")
    if not _HOSTNAME_RE.match(candidate) or _is_ip(candidate):
        raise ValueError(f"'{raw}' is not a valid domain name")
    return candidate
