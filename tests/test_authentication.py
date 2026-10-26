
import pytest

from src.authentication import (
    authenticate_user,
    bootstrap_admin,
    count_users,
    create_user,
    get_active_identity,
    hash_password,
    require_roles,
    set_user_active,
    verify_password,
)


def test_password_hash_is_salted():
    first = hash_password("TrainFlow-Secure-Password-123!")
    second = hash_password("TrainFlow-Secure-Password-123!")

    assert first != second
    assert verify_password("TrainFlow-Secure-Password-123!", first)
    assert not verify_password("Wrong-Password-123!", first)


def test_short_password_is_rejected():
    with pytest.raises(ValueError):
        hash_password("short")


def test_bootstrap_admin_only_works_once(tmp_path):
    path = tmp_path / "trainflow.db"

    admin = bootstrap_admin(
        path,
        "trainflowadmin",
        "Admin-Secure-Password-123!",
    )

    assert admin["role"] == "ADMIN"
    assert count_users(path) == 1

    with pytest.raises(PermissionError, match="no users exist"):
        bootstrap_admin(
            path,
            "anotheradmin",
            "Another-Secure-Password-123!",
        )


def test_login_returns_authenticated_identity(tmp_path):
    path = tmp_path / "trainflow.db"

    admin = bootstrap_admin(
        path,
        "trainflowadmin",
        "Admin-Secure-Password-123!",
    )

    identity = authenticate_user(
        path,
        "trainflowadmin",
        "Admin-Secure-Password-123!",
    )

    assert identity["user_id"] == admin["user_id"]
    assert identity["role"] == "ADMIN"


def test_wrong_password_is_rejected(tmp_path):
    path = tmp_path / "trainflow.db"

    bootstrap_admin(
        path,
        "trainflowadmin",
        "Admin-Secure-Password-123!",
    )

    assert authenticate_user(
        path,
        "trainflowadmin",
        "Wrong-Password-123!",
    ) is None


def test_unknown_user_is_rejected(tmp_path):
    path = tmp_path / "trainflow.db"

    bootstrap_admin(
        path,
        "trainflowadmin",
        "Admin-Secure-Password-123!",
    )

    assert authenticate_user(
        path,
        "unknownuser",
        "Any-Password-123!",
    ) is None


def test_admin_can_create_user(tmp_path):
    path = tmp_path / "trainflow.db"

    admin = bootstrap_admin(
        path,
        "trainflowadmin",
        "Admin-Secure-Password-123!",
    )

    manager = create_user(
        path,
        actor_user_id=admin["user_id"],
        username="trainingmanager",
        password="Manager-Secure-Password-123!",
        role="MANAGER",
    )

    assert manager["role"] == "MANAGER"
    assert count_users(path) == 2


def test_non_admin_cannot_create_users(tmp_path):
    path = tmp_path / "trainflow.db"

    admin = bootstrap_admin(
        path,
        "trainflowadmin",
        "Admin-Secure-Password-123!",
    )

    manager = create_user(
        path,
        actor_user_id=admin["user_id"],
        username="trainingmanager",
        password="Manager-Secure-Password-123!",
        role="MANAGER",
    )

    with pytest.raises(PermissionError):
        create_user(
            path,
            actor_user_id=manager["user_id"],
            username="anotheruser",
            password="Another-Secure-Password-123!",
            role="EMPLOYEE",
        )


def test_role_is_loaded_from_database(tmp_path):
    path = tmp_path / "trainflow.db"

    admin = bootstrap_admin(
        path,
        "trainflowadmin",
        "Admin-Secure-Password-123!",
    )

    manager = create_user(
        path,
        actor_user_id=admin["user_id"],
        username="trainingmanager",
        password="Manager-Secure-Password-123!",
        role="MANAGER",
    )

    identity = get_active_identity(path, manager["user_id"])

    assert identity["role"] == "MANAGER"

    with pytest.raises(PermissionError):
        require_roles(
            path,
            manager["user_id"],
            {"ADMIN"},
        )


def test_inactive_user_cannot_pass_role_check(tmp_path):
    path = tmp_path / "trainflow.db"

    admin = bootstrap_admin(
        path,
        "trainflowadmin",
        "Admin-Secure-Password-123!",
    )

    manager = create_user(
        path,
        actor_user_id=admin["user_id"],
        username="trainingmanager",
        password="Manager-Secure-Password-123!",
        role="MANAGER",
    )

    set_user_active(
        path,
        actor_user_id=admin["user_id"],
        target_user_id=manager["user_id"],
        is_active=False,
    )

    assert get_active_identity(path, manager["user_id"]) is None

    with pytest.raises(PermissionError):
        require_roles(
            path,
            manager["user_id"],
            {"MANAGER"},
        )


def test_employee_cannot_deactivate_another_account(tmp_path):
    path = tmp_path / "trainflow.db"

    admin = bootstrap_admin(
        path,
        "trainflowadmin",
        "Admin-Secure-Password-123!",
    )

    manager = create_user(
        path,
        actor_user_id=admin["user_id"],
        username="trainingmanager",
        password="Manager-Secure-Password-123!",
        role="MANAGER",
    )

    with pytest.raises(PermissionError):
        set_user_active(
            path,
            actor_user_id=manager["user_id"],
            target_user_id=admin["user_id"],
            is_active=False,
        )
