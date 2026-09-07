import argparse
import sys
from getpass import getpass

import sqlalchemy as sa

from app.auth.security import hasher
from app.db import schema as t
from app.db.connection import engine


def main() -> None:
    parser = argparse.ArgumentParser(description="Создать первого администратора FindCar")
    parser.add_argument("username")
    parser.add_argument(
        "--password-stdin",
        action="store_true",
        help="Прочитать пароль из stdin без вывода (для автоматизации)",
    )
    args = parser.parse_args()
    if not 1 <= len(args.username.strip()) <= 80:
        parser.error("Имя должно содержать от 1 до 80 символов")
    password = (
        sys.stdin.readline().rstrip("\r\n")
        if args.password_stdin
        else getpass("Пароль (не менее 12 символов): ")
    )
    confirmation = password if args.password_stdin else getpass("Повторите пароль: ")
    if len(password) < 12 or len(password) > 128 or password != confirmation:
        parser.error("Пароли должны совпадать и содержать 12–128 символов")
    with engine().begin() as conn:
        conn.execute(sa.text("SELECT pg_advisory_xact_lock(7140081)"))
        if conn.execute(sa.select(t.users.c.id).where(t.users.c.role == "admin")).first():
            parser.error("Администратор уже существует; используйте закрытый admin API")
        conn.execute(
            t.users.insert().values(
                username=args.username.strip(), password_hash=hasher.hash(password), role="admin"
            )
        )
    print("Администратор создан")


if __name__ == "__main__":
    main()
