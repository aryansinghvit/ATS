"""Mint a Clerk-shaped mock bearer token for local development (AUTH_MOCK_MODE only).

Usage (repo root):
    .venv/bin/python scripts/mint_dev_token.py --org org_demo --role recruiter [--user user_demo]

The API must run with ENVIRONMENT=development, AUTH_MOCK_MODE=true and the same MOCK_JWT_SECRET.
The identity must already exist in PostgreSQL (POST /api/v1/auth/sync-user).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("AUTH_MOCK_MODE", "true")

from app.core.security import create_mock_token  # noqa: E402

ROLES = ("admin", "recruiter", "hiring_manager", "interviewer")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--org", required=True, help="Clerk organization id, e.g. org_demo")
    parser.add_argument("--role", choices=ROLES, default="recruiter")
    parser.add_argument("--user", help="Clerk user id (default: user_<role>_<org>)")
    parser.add_argument("--hours", type=int, default=8, help="Token lifetime in hours")
    args = parser.parse_args()
    print(
        create_mock_token(
            user_id=args.user or f"user_{args.role}_{args.org}",
            org_id=args.org,
            org_role=f"org:{args.role}",
            expires_in=args.hours * 3600,
        )
    )


if __name__ == "__main__":
    main()
