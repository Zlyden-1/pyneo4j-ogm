"""
Entry point for the CLI. It parses the arguments and calls the corresponding function.
"""
import asyncio
from argparse import ArgumentParser, ArgumentTypeError
from asyncio import iscoroutinefunction
from typing import Any

from pyneo4j_ogm.migrations.actions import create, down, init, status, up
from pyneo4j_ogm.migrations.models import StatusActionFormat
from pyneo4j_ogm.migrations.utils.migration import RunMigrationCount


def parse_migration_count(arg: Any) -> RunMigrationCount:
    if arg == "all":
        return arg
    else:
        try:
            count = int(arg)
            if count < 1:
                raise ValueError("Migration count must be greater than 0")

            return count
        except ValueError as exc:
            raise ArgumentTypeError("Migration count must be an integer or 'all'") from exc


def cli() -> None:
    """
    Function that parses the CLI arguments and calls the corresponding function.
    """
    parser = ArgumentParser(prog="pyneo4j_ogm", description="Migration CLI pyneo4j-ogm models")
    subparsers = parser.add_subparsers(dest="command", title="Commands", metavar="")

    # Parser for `ìnit` command
    init_parser = subparsers.add_parser("init", help="Initialize migrations for this project")
    init_parser.add_argument(
        "--migration-dir",
        help="Path to the directory where the migrations will be stored",
        default="migrations",
        dest="migration_dir",
        required=False,
    )
    init_parser.set_defaults(func=init)

    # Parser for `create` command
    create_parser = subparsers.add_parser("create", help="Creates a new migration file")
    create_parser.add_argument("name", help="Name of the migration")
    create_parser.add_argument(
        "-c", "--config", help="Path to a config file", dest="config_path", default=None, required=False
    )
    create_parser.set_defaults(func=create)

    # Parser for `up` command
    up_parser = subparsers.add_parser("up", help="Applies the defined number of migrations")
    up_parser.add_argument(
        "-c", "--config", help="Path to a config file", dest="config_path", default=None, required=False
    )
    up_parser.add_argument(
        "-n",
        "--number",
        help="Number of migrations to apply. Can either be a integer or 'all'. Omit to apply all pending migrations",
        type=parse_migration_count,
        default="all",
        dest="up_count",
        required=False,
    )
    up_parser.set_defaults(func=up)

    # Parser for `down` command
    down_parser = subparsers.add_parser("down", help="Rollbacks the defined number of migrations")
    down_parser.add_argument(
        "-c", "--config", help="Path to a config file", dest="config_path", default=None, required=False
    )
    down_parser.add_argument(
        "-n",
        "--number",
        dest="down_count",
        help="""Number of migrations to rollback. Can either be a integer or 'all'.
        If omitted, rolls back the last migration""",
        type=parse_migration_count,
        default=1,
        required=False,
    )
    down_parser.set_defaults(func=down)

    # Parser for `status` command
    status_parser = subparsers.add_parser("status", help="Shows the status of all migrations")
    status_parser.add_argument(
        "-c", "--config", help="Path to a config file", dest="config_path", default=None, required=False
    )
    status_parser.add_argument(
        "-f",
        "--format",
        help="Output format",
        choices=[format.value for format in StatusActionFormat],
        required=False,
    )
    status_parser.set_defaults(func=status)

    args = parser.parse_args()

    if args.command:
        if iscoroutinefunction(args.func):
            asyncio.run(args.func(args))
        else:
            args.func(args)
    else:
        parser.print_help()
