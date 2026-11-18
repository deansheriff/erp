"""`kx` command-line tool for operators.

uv run kx generate-key [--fernet]   print a new random secret (KX_SECRET_KEY / KX_ENCRYPTION_KEY)
uv run kx check-db                  verify the database is reachable and the role is safe
uv run kx setup-token               print a fresh one-time code for the setup wizard
"""

import argparse
import secrets
import sys


def _generate_key(args: argparse.Namespace) -> int:
    if args.fernet:
        from app.core.secrets import generate_encryption_key

        print(generate_encryption_key())
    else:
        print(secrets.token_urlsafe(48))
    return 0


def _check_db(_: argparse.Namespace) -> int:
    from app.core.db import UnsafeDatabaseRole, check_database, get_engine, verify_database_role

    engine = get_engine()
    try:
        check_database(engine)
        verify_database_role(engine)
    except UnsafeDatabaseRole as exc:
        print(f"Unsafe database role: {exc}", file=sys.stderr)
        return 2
    print("Database reachable; application role is safe.")
    return 0


def _setup_token(_: argparse.Namespace) -> int:
    from app.core.db import session_scope
    from app.core.errors import Conflict
    from app.identity import service as identity

    try:
        with session_scope() as db:
            code = identity.issue_setup_token(db)
    except Conflict:
        print("Kxmeleon is already set up. Setup codes are no longer issued.", file=sys.stderr)
        return 1
    print(f"Setup code: {code}  (valid for {identity.SETUP_TOKEN_MINUTES} minutes; open /setup)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kx", description="Kxmeleon ERP operator commands")
    sub = parser.add_subparsers(dest="command", required=True)
    gen = sub.add_parser("generate-key", help="print a new random secret")
    gen.add_argument("--fernet", action="store_true", help="a key for KX_ENCRYPTION_KEY")
    gen.set_defaults(func=_generate_key)
    sub.add_parser("check-db", help="check database connectivity and role safety").set_defaults(
        func=_check_db
    )
    sub.add_parser("setup-token", help="issue a one-time setup code").set_defaults(
        func=_setup_token
    )
    args = parser.parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
