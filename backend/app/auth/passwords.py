"""Password hashing with Argon2id.

Parameters follow the OWASP Password Storage Cheat Sheet minimum for Argon2id
(m=19 MiB, t=2, p=1). This keeps memory per hash small enough for small cloud instances;
auth endpoints are rate limited to bound concurrent hashing.
"""

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

_hasher = PasswordHasher(time_cost=2, memory_cost=19_456, parallelism=1)

# Verified against when the user does not exist, so response timing does not reveal
# whether an email is registered.
_DUMMY_HASH = _hasher.hash("dummy-password-for-timing-equalisation")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)
