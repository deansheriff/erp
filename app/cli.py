"""`kx` command-line tool for operators.

uv run kx generate-key     print a new random secret for KX_SECRET_KEY
uv run kx check-db         verify the database is reachable and the role is safe
"""

import argparse
import secrets
import sys


def _generate_key(_: argparse.Namespace) -> int:
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kx", description="Kxmeleon ERP operator commands")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("generate-key", help="print a new random secret").set_defaults(
        func=_generate_key
    )
    sub.add_parser("check-db", help="check database connectivity and role safety").set_defaults(
        func=_check_db
    )
    args = parser.parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
