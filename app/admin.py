"""Account administration from the command line, run inside the pod.

Sign-in and the admin role are authentik's (app/oidc.py): a user's account
is created at their first sign-in, and admins are the members of the
dxf-converter-admins group. This covers what's left:

    kubectl -n dxf-converter exec deploy/dxf-converter -- python3 -m app.admin list-users
    ... python3 -m app.admin rename-user OLD NEW   # e.g. so a pre-3.0 account
                                                   # matches its authentik username
    ... python3 -m app.admin sign-out NAME         # end all of NAME's sessions
    ... python3 -m app.admin delete-user NAME [--yes]
    ... python3 -m app.admin check
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import func, select

from .store import Database, ObjectStore, StoreConfig
from .store import auth, content
from .store.models import Scan, User


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python3 -m app.admin")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list-users")
    c = sub.add_parser("rename-user")
    c.add_argument("old")
    c.add_argument("new")
    c = sub.add_parser("sign-out", help="end every session the user has")
    c.add_argument("username")
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
        if args.cmd == "rename-user":
            with db.session() as s:
                auth.rename_user(s, args.old, args.new)
            print(f"renamed {auth.normalize(args.old)} -> {auth.normalize(args.new)}")

        elif args.cmd == "sign-out":
            with db.session() as s:
                user = s.scalar(select(User).where(User.username == auth.normalize(args.username)))
                if not user:
                    raise SystemExit(f"no user {args.username!r}")
                n = auth.revoke_user_tokens(s, user.id)
            print(f"signed out {user.username} ({n} sessions)")

        elif args.cmd == "list-users":
            with db.session() as s:
                rows = s.execute(
                    select(User.username, User.is_admin, User.created_at, func.count(Scan.id),
                           User.password_hash)
                    .outerjoin(Scan, Scan.user_id == User.id)
                    .group_by(User.id).order_by(User.username)).all()
            for name, is_admin, created, scans, pw in rows:
                print(f"{name:<24} {'admin' if is_admin else '     '}  "
                      f"{created:%Y-%m-%d}  {scans} scans"
                      + ("  (pre-3.0, not yet claimed via authentik)" if pw else ""))

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
