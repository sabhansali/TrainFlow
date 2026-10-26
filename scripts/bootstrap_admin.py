
"""One-time interactive setup for the first TrainFlow administrator."""

from getpass import getpass

from src.authentication import bootstrap_admin, count_users
from src.database import DEFAULT_DB_PATH, initialize_database


def main():
    db_path = initialize_database(DEFAULT_DB_PATH)

    if count_users(db_path) != 0:
        print(
            "Administrator bootstrap is disabled because accounts already exist."
        )
        print("Use an existing administrator account to manage users.")
        return

    username = input("New administrator username: ").strip()
    password = getpass("New administrator password: ")
    confirmation = getpass("Confirm password: ")

    if password != confirmation:
        print("Passwords do not match. No account was created.")
        return

    try:
        user = bootstrap_admin(db_path, username, password)
    except (ValueError, PermissionError) as exc:
        print(f"Administrator setup failed: {exc}")
        return

    print("Administrator created successfully.")
    print(f"Username: {user['username']}")
    print(f"User ID: {user['user_id']}")
    print("Store the user ID securely for the local authentication workflow.")


if __name__ == "__main__":
    main()
