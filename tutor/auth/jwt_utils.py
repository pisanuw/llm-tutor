"""JWT creation and validation for the tutor's session cookies."""

from __future__ import annotations

import datetime
import uuid
from typing import Tuple

from jose import jwt

from tutor.config import get_config
from tutor.utctime import utcnow

ALGORITHM = "HS256"


def create_token(student_id: int) -> Tuple[str, str, datetime.datetime]:
    """Return (encoded_jwt, jti, expires_at)."""
    cfg = get_config()
    jti = str(uuid.uuid4())
    expires_at = utcnow() + datetime.timedelta(days=cfg["jwt_expiry_days"])
    payload = {
        "sub": str(student_id),
        "jti": jti,
        "exp": expires_at,
    }
    token = jwt.encode(payload, cfg["secret_key"], algorithm=ALGORITHM)
    return token, jti, expires_at


def decode_token(token: str) -> dict:
    """Decode and return payload. Raises JWTError on failure."""
    cfg = get_config()
    return jwt.decode(token, cfg["secret_key"], algorithms=[ALGORITHM])
