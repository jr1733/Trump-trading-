"""Single-user bearer-token authentication.

Why a bearer token and not a session cookie: the token travels in the
`Authorization` header, which browsers do not attach automatically to
cross-site requests. That removes the CSRF surface entirely rather than
mitigating it with a synchroniser token.

The token is compared with `secrets.compare_digest` so the comparison does not
leak length or prefix information through timing.
"""

from __future__ import annotations

import secrets

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from .models import User

UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Missing or invalid credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


def verify_token(authorization: str | None = Header(default=None)) -> None:
    if not authorization:
        raise UNAUTHORIZED
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise UNAUTHORIZED
    if not secrets.compare_digest(token, settings.app_auth_token):
        raise UNAUTHORIZED


def current_user(
    _: None = Depends(verify_token), db: Session = Depends(get_db)
) -> User:
    """The single Phase 1 user. Created on demand so a fresh DB just works."""
    user = db.execute(select(User).order_by(User.created_at)).scalars().first()
    if user is None:
        user = User(display_name="Operator")
        db.add(user)
        db.commit()
        db.refresh(user)
    return user
