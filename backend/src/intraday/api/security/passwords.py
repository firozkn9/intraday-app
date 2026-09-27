"""Password hashing (argon2id) and policy."""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 256

_hasher = PasswordHasher()  # argon2id with library defaults (RFC 9106 low-memory profile)

# Verified when the email is unknown so response time doesn't reveal which emails exist.
_DUMMY_HASH = _hasher.hash("dummy-password-for-timing-equalisation")


class WeakPassword(ValueError):
    pass


def check_policy(password: str, *, email: str | None = None) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise WeakPassword(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise WeakPassword(f"password must be at most {MAX_PASSWORD_LENGTH} characters")
    if email and password.lower() == email.lower():
        raise WeakPassword("password must not equal the email address")
    if len(set(password)) < 5:
        raise WeakPassword("password is too repetitive")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)
