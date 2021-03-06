#!/usr/bin/env python3.6
import asyncio
import locale
import logging
import os
import sys
from argparse import ArgumentParser
from importlib import import_module
from pathlib import Path
from typing import Callable

import uvloop
from aiohttp.web import Application, run_app
from pydantic import BaseSettings as PydanticBaseSettings
from pydantic.utils import import_string

from .logs import ColouredAccessLogger, setup_logging
from .network import check_server, wait_for_services
from .settings import BaseSettings

logger = logging.getLogger('atoolbox.cli')
commands = {}


def command(func: Callable):
    commands[func.__name__] = func
    return func


@command
def web(args, settings: BaseSettings):
    logger.info('running web server at %s...', settings.port)
    create_app: Callable[[BaseSettings], Application] = import_string(settings.create_app)
    wait_for_services(settings)
    app = create_app(settings=settings)
    kwargs = dict(port=settings.port, shutdown_timeout=8, print=lambda *args: None)  # pragma: no branch
    if args.access_log:
        kwargs.update(access_log_class=ColouredAccessLogger, access_log=logging.getLogger('atoolbox.access'))
    else:
        kwargs['access_log'] = None
    run_app(app, **kwargs)


@command
def worker(args, settings: BaseSettings):
    if settings.worker_func:
        logger.info('running worker...')
        worker_func: Callable[[BaseSettings], None] = import_string(settings.worker_func)
        wait_for_services(settings)
        worker_func(settings=settings)
    else:
        raise CliError("settings.worker_path not set, can't run the worker")


@command
def patch(args, settings: BaseSettings):
    logger.info('running patch...')
    from .patch_methods import run_patch

    wait_for_services(settings)
    args.patches_path and import_module(args.patches_path)
    if args.extra:
        patch_name = args.extra[0]
        extra_args = args.extra[1:]
    else:
        patch_name = None
        extra_args = ()
    return run_patch(settings, patch_name, args.live, extra_args)


@command
def reset_database(args, settings: BaseSettings):
    logger.info('running reset_database...')
    from .db import reset_database

    wait_for_services(settings)
    reset_database(settings)


@command
def flush_redis(args, settings: BaseSettings):
    from .db.redis import flush_redis

    flush_redis(settings)


@command
def check_web(args, settings: BaseSettings):
    url = exp_status = None
    if args.extra:
        url = args.extra[0]
        if len(args.extra) == 2:
            exp_status = int(args.extra[1])

    url = url or os.getenv('ATOOLBOX_CHECK_URL') or f'http://localhost:{settings.port}/'
    exp_status = exp_status or int(os.getenv('ATOOLBOX_CHECK_STATUS') or 200)
    logger.info('checking server is running at "%s" expecting %d...', url, exp_status)
    return check_server(url, exp_status)


class CliError(RuntimeError):
    pass


def main(*args) -> int:
    parser = ArgumentParser(description='aiohttp-toolbox command line interface')
    parser.add_argument('command', type=str, choices=list(commands.keys()), help='The command to run')
    parser.add_argument(
        '--root',
        dest='root',
        default=os.getenv('ATOOLBOX_ROOT_DIR', '.'),
        help=(
            'root directory to run the command from, defaults to to the environment variable '
            '"ATOOLBOX_ROOT_DIR" or "."'
        ),
    )
    parser.add_argument(
        '--settings-path',
        dest='settings_path',
        default=os.getenv('ATOOLBOX_SETTINGS', 'settings.Settings'),
        help=(
            'settings path (dotted, relative to the root directory), defaults to to the environment variable '
            '"ATOOLBOX_SETTINGS" or "settings.Settings"'
        ),
    )
    parser.add_argument('--verbose', action='store_true', help='whether to pring debug logs')
    parser.add_argument(
        '--log',
        default=os.getenv('ATOOLBOX_LOG_NAME', 'app'),
        help='Root name of logs for the app, defaults to to the environment variable "ATOOLBOX_LOG_NAME" or "app"',
    )
    parser.add_argument(
        '--live',
        action='store_true',
        help='whether to run patches as live, default false, only applies to the "patch" command.',
    )
    parser.add_argument(
        '--access-log',
        dest='access_log',
        action='store_true',
        help='whether run the access logger on web, default false, only applies to the "web" command.',
    )
    parser.add_argument(
        '--patches-path', help='patch to import before running patches, only applies to the "patch" command.'
    )
    parser.add_argument('extra', nargs='*', default=[], help='Extra arguments to pass to the command.')
    try:
        ns, extra = parser.parse_known_args(args)
    except SystemExit:
        return 1

    ns.extra.extend(extra)
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    logging_client = setup_logging(debug=ns.verbose, main_logger_name=ns.log)
    try:
        sys.path.append(os.getcwd())
        root_dir = str(Path(ns.root).resolve())
        sys.path.append(root_dir)
        os.chdir(root_dir)

        try:
            settings_cls = import_string(ns.settings_path)
        except (ModuleNotFoundError, ImportError) as exc:
            raise CliError(f'unable to import "{ns.settings_path}", {exc.__class__.__name__}: {exc}')

        if not isinstance(settings_cls, type) or not issubclass(settings_cls, PydanticBaseSettings):
            raise CliError(f'settings "{settings_cls}" (from "{ns.settings_path}"), is not a valid Settings class')

        settings = settings_cls()
        locale.setlocale(locale.LC_ALL, getattr(settings, 'locale', 'en_US.utf8'))

        func = commands[ns.command]
        return func(ns, settings) or 0
    except CliError as exc:
        logger.error('%s', exc)
        return 1
    finally:
        loop = asyncio.get_event_loop()
        if logging_client and not loop.is_closed():
            transport = logging_client.remote.get_transport()
            transport and loop.run_until_complete(transport.close())


def cli():  # pragma: no cover
    sys.exit(main(*sys.argv[1:]))


if __name__ == '__main__':  # pragma: no cover
    cli()
