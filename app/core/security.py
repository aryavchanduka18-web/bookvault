"""Password hashing.

bcrypt directly, no wrapper library. bcrypt generates a random salt per
password and embeds it in the hash string, so no separate salt field is
needed on the user document.
"""

import bcrypt

from app.core.errors import ApiError

# bcrypt silently truncates anything past 72 bytes, which would make two
# different long passwords interchangeable. Reject instead of truncating.
MAX_PASSWORD_BYTES = 72


def hash_password(password: str) -> str:
    encoded = password.encode("utf-8")
    if len(encoded) > MAX_PASSWORD_BYTES:
        raise ApiError(
            400,
            "validation failed",
            f"password must be at most {MAX_PASSWORD_BYTES} bytes",
        )
    return bcrypt.hashpw(encoded, bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        # Malformed hash stored in the database.
        return False
