import argparse
import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from main import ROLES, User, engine


ACCOUNT_FILE = Path(__file__).with_name("login.diploma-account")


def export_account(db):
    email = input("Existing account email: ").strip().lower()
    user = db.scalar(select(User).where(User.email == email))
    if user is None:
        raise ValueError("No account with this email exists in this database.")
    with ACCOUNT_FILE.open("x", encoding="utf-8") as file:
        json.dump({"email": user.email, "name": user.name, "role": user.role,
                   "password": user.password}, file, ensure_ascii=False)
    print(f"Exported to {ACCOUNT_FILE}. Transfer this file privately.")


def import_account(db):
    with ACCOUNT_FILE.open(encoding="utf-8") as file:
        data = json.load(file)
    fields = ("email", "name", "role", "password")
    if (not isinstance(data, dict) or not all(isinstance(data.get(key), str) and data[key] for key in fields)
            or data["role"] not in ROLES or not data["password"].startswith("$argon2")):
        raise ValueError("Invalid account file.")
    db.connection().exec_driver_sql("BEGIN IMMEDIATE")
    existing = db.scalar(select(User).where(User.email == data["email"]))
    if existing:
        if all(getattr(existing, key) == data[key] for key in fields):
            print("This account is already imported.")
            return
        raise ValueError("An account with this email already exists. Import into a fresh installation.")
    db.add(User(**{key: data[key] for key in fields}))
    db.commit()
    print("Imported. Sign in with the same email and password.")


def main():
    parser = argparse.ArgumentParser(description="Move a local diploma portal account between computers.")
    parser.add_argument("action", choices=("export", "import"))
    args = parser.parse_args()
    try:
        with Session(engine) as db:
            if args.action == "export":
                export_account(db)
            else:
                import_account(db)
    except (OSError, ValueError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
