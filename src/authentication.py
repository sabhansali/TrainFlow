
"""TrainFlow: password hashing, authentication, and database-backed roles."""

import hashlib
import hmac
import os
import re
import uuid
from pathlib import Path

from src.audit import append_audit_event
from src.database import connect_db, initialize_database, utc_now


SUPPORTED_ROLES = {
    "ADMIN",
    "MANAGER",
    "LND_REVIEWER",
    "EMPLOYEE",
    "READ_ONLY",
}

PBKDF2_ITERATIONS = 310_000
SALT_BYTES = 16
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{3,64}$")


def validate_username(username: str) -> str:
    if not isinstance(username, str):
        raise ValueError("Username must be text")

    username = username.strip()

    if not USERNAME_PATTERN.fullmatch(username):
        raise ValueError(
            "Username must be 3-64 characters using letters, numbers, "
            "periods, underscores, or hyphens"
        )

    return username


def validate_password(password: str) -> None:
    if not isinstance(password, str):
        raise ValueError("Password must be text")

    if len(password) < 12:
        raise ValueError("Password must contain at least 12 characters")

    if len(password) > 1024:
        raise ValueError("Password is too long")

    if "\x00" in password:
        raise ValueError("Password cannot contain a null character")


def hash_password(password: str) -> str:
    """Return a salted PBKDF2 password hash."""

    validate_password(password)

    salt = os.urandom(SALT_BYTES)

    derived = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PBKDF2_ITERATIONS,
        dklen=32,
    )

    return (
        f"pbkdf2_sha256${PBKDF2_ITERATIONS}"
        f"${salt.hex()}${derived.hex()}"
    )


def verify_password(password: str, stored_hash: str) -> bool:
    """Verify a PBKDF2 password hash without exposing comparison timing."""

    if not isinstance(password, str) or not isinstance(stored_hash, str):
        return False

    try:
        algorithm, iterations_text, salt_hex, expected_hex = (
            stored_hash.split("$")
        )

        if algorithm != "pbkdf2_sha256":
            return False

        iterations = int(iterations_text)

        # Reject malformed or unexpectedly extreme work factors.
        if not 100_000 <= iterations <= 2_000_000:
            return False

        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(expected_hex)

        if len(salt) != SALT_BYTES or len(expected) != 32:
            return False

        derived = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt,
            iterations,
            dklen=32,
        )

        return hmac.compare_digest(derived, expected)

    except (ValueError, TypeError):
        return False


def count_users(db_path: str | Path) -> int:
    path = initialize_database(db_path)

    with connect_db(path) as connection:
        row = connection.execute(
            "SELECT COUNT(*) AS count FROM users"
        ).fetchone()

    return int(row["count"])


def _identity_from_row(row) -> dict:
    return {
        "user_id": row["user_id"],
        "username": row["username"],
        "role": row["role"],
    }


def get_active_identity(
    db_path: str | Path,
    user_id: str,
) -> dict | None:
    """
    Reload a user's current role and active status from the database.

    Do not trust a role supplied by a form or copied from session data.
    """

    if not isinstance(user_id, str) or not user_id:
        return None

    path = initialize_database(db_path)

    with connect_db(path) as connection:
        row = connection.execute(
            """
            SELECT user_id, username, role
            FROM users
            WHERE user_id = ? AND is_active = 1
            """,
            (user_id,),
        ).fetchone()

    return _identity_from_row(row) if row else None


def require_roles(
    db_path: str | Path,
    user_id: str,
    allowed_roles: set[str],
) -> dict:
    """Return the active identity only when its database role is allowed."""

    if not allowed_roles or not allowed_roles.issubset(SUPPORTED_ROLES):
        raise ValueError("allowed_roles contains an unsupported role")

    identity = get_active_identity(db_path, user_id)

    if identity is None:
        raise PermissionError("User is missing or inactive")

    if identity["role"] not in allowed_roles:
        raise PermissionError("Insufficient permissions")

    return identity


def bootstrap_admin(
    db_path: str | Path,
    username: str,
    password: str,
) -> dict:
    """
    Create the first administrator, only when no accounts exist.

    Call explicitly from a controlled setup step, not on every app rerun.
    """

    path = initialize_database(db_path)
    username = validate_username(username)
    password_hash = hash_password(password)
    user_id = str(uuid.uuid4())
    now = utc_now()

    with connect_db(path) as connection:
        connection.execute("BEGIN IMMEDIATE")

        count = connection.execute(
            "SELECT COUNT(*) AS count FROM users"
        ).fetchone()["count"]

        if count != 0:
            raise PermissionError(
                "Bootstrap is allowed only when no users exist"
            )

        connection.execute(
            """
            INSERT INTO users (
                user_id, username, password_hash, role,
                is_active, created_at, last_login_at
            ) VALUES (?, ?, ?, 'ADMIN', 1, ?, NULL)
            """,
            (user_id, username, password_hash, now),
        )

        append_audit_event(
            path,
            event_type="ADMIN_BOOTSTRAP",
            actor_id=user_id,
            details={"username": username, "role": "ADMIN"},
            connection=connection,
        )

    return {
        "user_id": user_id,
        "username": username,
        "role": "ADMIN",
    }


def create_user(
    db_path: str | Path,
    actor_user_id: str,
    username: str,
    password: str,
    role: str,
) -> dict:
    """Allow an active database-authenticated administrator to create users."""

    path = initialize_database(db_path)
    username = validate_username(username)

    if not isinstance(role, str) or role.upper() not in SUPPORTED_ROLES:
        raise ValueError("Unsupported role")

    role = role.upper()
    password_hash = hash_password(password)
    user_id = str(uuid.uuid4())
    now = utc_now()

    with connect_db(path) as connection:
        connection.execute("BEGIN IMMEDIATE")

        actor = connection.execute(
            """
            SELECT user_id, username, role
            FROM users
            WHERE user_id = ? AND is_active = 1
            """,
            (actor_user_id,),
        ).fetchone()

        if actor is None or actor["role"] != "ADMIN":
            raise PermissionError(
                "Only an active administrator can create users"
            )

        connection.execute(
            """
            INSERT INTO users (
                user_id, username, password_hash, role,
                is_active, created_at, last_login_at
            ) VALUES (?, ?, ?, ?, 1, ?, NULL)
            """,
            (user_id, username, password_hash, role, now),
        )

        append_audit_event(
            path,
            event_type="USER_CREATED",
            actor_id=actor_user_id,
            details={
                "new_user_id": user_id,
                "username": username,
                "role": role,
            },
            connection=connection,
        )

    return {
        "user_id": user_id,
        "username": username,
        "role": role,
    }


def authenticate_user(
    db_path: str | Path,
    username: str,
    password: str,
) -> dict | None:
    """
    Authenticate credentials.

    Returns the user's ID, username and role on success; otherwise None.
    The caller must reload the active identity for each protected action.
    """

    if not isinstance(username, str) or not isinstance(password, str):
        return None

    path = initialize_database(db_path)

    with connect_db(path) as connection:
        row = connection.execute(
            """
            SELECT user_id, username, password_hash, role
            FROM users
            WHERE username = ? AND is_active = 1
            """,
            (username.strip(),),
        ).fetchone()

        if row is None:
            # Perform comparable password-hash work to reduce username timing leaks.
            dummy = hash_password("TrainFlow-dummy-password-123!")
            verify_password(password, dummy)
            return None

        if not verify_password(password, row["password_hash"]):
            return None

        connection.execute(
            """
            UPDATE users
            SET last_login_at = ?
            WHERE user_id = ?
            """,
            (utc_now(), row["user_id"]),
        )

        identity = _identity_from_row(row)

    append_audit_event(
        path,
        event_type="LOGIN_SUCCESS",
        actor_id=identity["user_id"],
        details={"username": identity["username"]},
    )

    return identity


def set_user_active(
    db_path: str | Path,
    actor_user_id: str,
    target_user_id: str,
    is_active: bool,
) -> None:
    """Enable or disable a user account with an audit event."""

    if not isinstance(is_active, bool):
        raise ValueError("is_active must be True or False")

    path = initialize_database(db_path)

    with connect_db(path) as connection:
        connection.execute("BEGIN IMMEDIATE")

        actor = connection.execute(
            """
            SELECT role FROM users
            WHERE user_id = ? AND is_active = 1
            """,
            (actor_user_id,),
        ).fetchone()

        if actor is None or actor["role"] != "ADMIN":
            raise PermissionError(
                "Only an active administrator can change account status"
            )

        target = connection.execute(
            """
            SELECT username FROM users
            WHERE user_id = ?
            """,
            (target_user_id,),
        ).fetchone()

        if target is None:
            raise ValueError("Target user does not exist")

        if target_user_id == actor_user_id and not is_active:
            raise ValueError("An administrator cannot deactivate themselves")

        connection.execute(
            """
            UPDATE users
            SET is_active = ?
            WHERE user_id = ?
            """,
            (int(is_active), target_user_id),
        )

        append_audit_event(
            path,
            event_type="USER_STATUS_CHANGED",
            actor_id=actor_user_id,
            details={
                "target_user_id": target_user_id,
                "username": target["username"],
                "is_active": is_active,
            },
            connection=connection,
        )
