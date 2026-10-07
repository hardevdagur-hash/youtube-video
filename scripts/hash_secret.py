# ruff: noqa: T201  (CLI: printing is its output)
"""Generate credentials for AUTH_USERS, API_KEYS and JWT_SECRET_KEY.

Usage:
    python scripts/hash_secret.py user <username> [--role admin|user]   (prompts for password)
    python scripts/hash_secret.py apikey <name> [--role admin|user]
    python scripts/hash_secret.py jwt-secret

Only hashes go into .env / the secret store. Raw API keys are printed once; store them
in the client's own secret store.
"""

from __future__ import annotations

import argparse
import getpass
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from security.web_auth import VALID_ROLES, generate_api_key, hash_password  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    user = sub.add_parser("user", help="hash a login password for AUTH_USERS")
    user.add_argument("username")
    user.add_argument("--role", choices=sorted(VALID_ROLES), default="user")

    key = sub.add_parser("apikey", help="generate an API key for API_KEYS")
    key.add_argument("name")
    key.add_argument("--role", choices=sorted(VALID_ROLES), default="user")

    sub.add_parser("jwt-secret", help="generate a JWT_SECRET_KEY")

    args = parser.parse_args(argv)

    if args.command == "user":
        password = getpass.getpass("Password: ")
        if len(password) < 12:
            print("Password must be at least 12 characters.", file=sys.stderr)
            return 1
        if getpass.getpass("Repeat password: ") != password:
            print("Passwords do not match.", file=sys.stderr)
            return 1
        print(f"AUTH_USERS entry:\n{args.username}:{args.role}:{hash_password(password)}")
    elif args.command == "apikey":
        raw, digest = generate_api_key()
        print(f"API key (give to the client, shown once):\n{raw}\n")
        print(f"API_KEYS entry:\n{args.name}:{args.role}:{digest}")
    else:
        print(f"JWT_SECRET_KEY={secrets.token_urlsafe(48)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
