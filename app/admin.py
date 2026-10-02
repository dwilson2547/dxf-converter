"""Account administration. Accounts are created here, not by self-sign-up.

In the cluster:
    kubectl -n dxf-converter exec -it deploy/dxf-converter -- python3 -m app.admin add-user dan
    kubectl -n dxf-converter exec -it deploy/dxf-converter -- python3 -m app.admin check

Passwords are prompted for (no echo), or read from stdin with --password-stdin.
They are never taken as a command-line argument, which would leave them in
shell history and the process list.
"""

from __future__ import annotations

import argparse
import getpass
import sys

from sqlalchemy import func, select

from .store import Database, ObjectStore, StoreConfig
from .store import auth
from .store.models import Scan, User


def _password(args) -> str:
    if args.password_stdin:
        return sys.stdin.readline().rstrip("\n")
    pw = getpass.getpass("password: ")
    if getpass.getpass("again: ") != pw:
        raise SystemExit("passwords don't match")
    return pw


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python3 -m app.admin")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("add-user", "set-password"):
        c = sub.add_parser(name)
        c.add_argument("username")
        c.add_argument("--password-stdin", action="store_true")
    sub.add_parser("list-users")
    c = sub.add_parser("delete-user")
    c.add_argument("username")
    c.add_argument("--yes", action="store_true", help="also deletes the user's scans")
    sub.add_parser("check", help="reach the database and the bucket")
    args = p.parse_args(argv)

    cfg = StoreConfig.from_env()
    if not cfg.enabled:
        raise SystemExit("DATABASE_URL is not set: accounts are disabled")
    db = Database(cfg.database_url)
    db.create_schema()

    try:
        if args.cmd == "add-user":
            pw = _password(args)
            with db.session() as s:
                auth.create_user(s, args.username, pw)
            print(f"created {args.username}")

        elif args.cmd == "set-password":
            pw = _password(args)
            with db.session() as s:
                auth.set_password(s, args.username, pw)
            print(f"password changed for {args.username}; existing logins signed out")

        elif args.cmd == "list-users":
            with db.session() as s:
                rows = s.execute(
                    select(User.username, User.created_at, func.count(Scan.id))
                    .outerjoin(Scan, Scan.user_id == User.id)
                    .group_by(User.id).order_by(User.username)).all()
            for name, created, scans in rows:
                print(f"{name:<24} {created:%Y-%m-%d}  {scans} scans")

        elif args.cmd == "delete-user":
            with db.session() as s:
                user = s.scalar(select(User).where(User.username == args.username))
                if not user:
                    raise SystemExit(f"no user {args.username!r}")
                n = s.scalar(select(func.count(Scan.id)).where(Scan.user_id == user.id))
                if n and not args.yes:
                    raise SystemExit(f"{args.username} has {n} saved scans; "
                                     "rerun with --yes to delete them too")
                if cfg.objects_enabled:
                    ObjectStore(cfg).delete_prefix(f"users/{user.id}/")
                s.delete(user)
            print(f"deleted {args.username}")

        elif args.cmd == "check":
            with db.session() as s:
                users = s.scalar(select(func.count(User.id)))
            print(f"database  ok ({users} users)")
            if cfg.objects_enabled:
                ObjectStore(cfg).ping()
                print(f"bucket    ok ({cfg.s3_endpoint}/{cfg.s3_bucket})")
            else:
                print("bucket    not configured (S3_* unset)")
    except auth.AuthError as exc:
        raise SystemExit(str(exc)) from None
    return 0


if __name__ == "__main__":
    sys.exit(main())
