"""Account administration from the command line, run inside the pod.

First run — create the admin and print its generated password:
    kubectl -n dxf-converter exec deploy/dxf-converter -- python3 -m app.admin init-admin

Other commands, the same way:
    ... python3 -m app.admin add-user dan [--admin]
    ... python3 -m app.admin check

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
from .store import auth, content
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
    c = sub.add_parser("init-admin", help="create the first admin with a generated password")
    c.add_argument("--username", default="admin")
    c.add_argument("--reset", action="store_true",
                   help="admin exists: give it a new generated password instead")
    for name in ("add-user", "set-password"):
        c = sub.add_parser(name)
        c.add_argument("username")
        c.add_argument("--password-stdin", action="store_true")
        if name == "add-user":
            c.add_argument("--admin", action="store_true")
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
        if args.cmd == "init-admin":
            name = auth.normalize(args.username)
            pw = auth.generate_password()
            with db.session() as s:
                existing = s.scalar(select(User).where(User.username == name))
                admins = s.scalar(select(func.count(User.id)).where(User.is_admin.is_(True)))
                if existing and not args.reset:
                    raise SystemExit(f"{name!r} already exists; --reset gives it a new "
                                     "generated password")
                if not existing and admins and not args.reset:
                    raise SystemExit(f"an admin already exists ({admins}); "
                                     "use add-user --admin for another")
                if existing:
                    if not existing.is_admin:
                        raise SystemExit(f"{name!r} exists but is not an admin")
                    auth.set_password(s, name, pw)
                else:
                    auth.create_user(s, name, pw, is_admin=True)
            # Printed once and stored nowhere: only the argon2 hash is kept.
            print(f"admin user:     {name}")
            print(f"admin password: {pw}")
            print("Save it now; it is not shown again. Change it later with set-password.")

        elif args.cmd == "add-user":
            pw = _password(args)
            with db.session() as s:
                auth.create_user(s, args.username, pw, is_admin=args.admin)
            print(f"created {auth.normalize(args.username)}" + (" (admin)" if args.admin else ""))

        elif args.cmd == "set-password":
            pw = _password(args)
            with db.session() as s:
                auth.set_password(s, args.username, pw)
            print(f"password changed for {args.username}; existing logins signed out")

        elif args.cmd == "list-users":
            with db.session() as s:
                rows = s.execute(
                    select(User.username, User.is_admin, User.created_at, func.count(Scan.id))
                    .outerjoin(Scan, Scan.user_id == User.id)
                    .group_by(User.id).order_by(User.username)).all()
            for name, is_admin, created, scans in rows:
                print(f"{name:<24} {'admin' if is_admin else '     '}  "
                      f"{created:%Y-%m-%d}  {scans} scans")

        elif args.cmd == "delete-user":
            name = auth.normalize(args.username)
            with db.session() as s:
                user = s.scalar(select(User).where(User.username == name))
                if not user:
                    raise SystemExit(f"no user {name!r}")
                n, _ = content.content_counts(s, user.id)
                if n and not args.yes:
                    raise SystemExit(f"{name} has {n} saved scans; "
                                     "rerun with --yes to delete them too")
                content.delete_user(s, ObjectStore(cfg) if cfg.objects_enabled else None, user)
            print(f"deleted {name}")

        elif args.cmd == "check":
            with db.session() as s:
                users = s.scalar(select(func.count(User.id)))
            print(f"database  ok ({users} users)")
            if cfg.objects_enabled:
                ObjectStore(cfg).ping()
                print(f"bucket    ok ({cfg.s3_endpoint}/{cfg.s3_bucket})")
            else:
                print("bucket    not configured (S3_* unset)")
    except (auth.AuthError, content.LastAdminError) as exc:
        raise SystemExit(str(exc)) from None
    return 0


if __name__ == "__main__":
    sys.exit(main())
