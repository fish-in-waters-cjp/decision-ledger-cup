"""Decision Ledger: organizational decision memory with deviation detection and assumption watch."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("decision-ledger")
except PackageNotFoundError:  # running from a source tree without an installed distribution
    __version__ = "0.0.0"


def main(argv: Sequence[str] | None = None) -> None:
    """Command-line entry point: ``serve`` the web app or run the credential-free ``demo``."""
    from decision_ledger import bootstrap

    parser = argparse.ArgumentParser(prog="decision-ledger")
    commands = parser.add_subparsers(dest="command", required=True)
    serve_cmd = commands.add_parser("serve", help="run the API and web UI")
    serve_cmd.add_argument("--host")
    serve_cmd.add_argument("--port", type=int)
    demo_cmd = commands.add_parser("demo", help="run the scripted demo with the Fake backend (no credentials)")
    demo_cmd.add_argument("--db", default=":memory:", help="SQLite path (default: in-memory)")
    args = parser.parse_args(argv)

    settings = bootstrap.Settings.from_env()
    from decision_ledger.domain.errors import SchemaVersionMismatch

    try:
        if args.command == "serve":
            bootstrap.serve(settings, args.host, args.port)
            return
        from dataclasses import replace

        provider, project = bootstrap.build_provider(replace(settings, llm_backend="fake", db_path=args.db))
        summary = bootstrap.run_demo(provider.for_project(project.id))
    except (bootstrap.ModelNotAllowed, bootstrap.InvalidSettings) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc
    except SchemaVersionMismatch as exc:
        print(
            f"Database {exc.path} has schema version {exc.found}; this version needs {exc.expected}.\n"
            "The database was not modified. Delete the database file, restart, then import your sources again.",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc
    for name, count in summary.items():
        print(f"{name}: {count}")
