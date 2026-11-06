from __future__ import annotations

# import an additional thing for proper PyInstaller freeze support
from multiprocessing import freeze_support


if __name__ == "__main__":
    freeze_support()
    import io
    import os
    import sys
    import signal
    import asyncio
    import logging
    import argparse
    import warnings
    import traceback
    import tkinter as tk
    from tkinter import messagebox
    from typing import NoReturn, TYPE_CHECKING

    import truststore
    truststore.inject_into_ssl()

    from translate import _
    from twitch import Twitch
    from settings import Settings
    from version import __version__
    from exceptions import CaptchaRequired
    from utils import lock_file, resource_path, set_root_icon
    from constants import LOGGING_LEVELS, SELF_PATH
    from accounts import (
        AccountEntry,
        AccountRuntime,
        DEFAULT_ACCOUNT,
        apply_proxy_pool,
        assign_proxies,
        install_log_handler,
        load_registry_all,
        proxy_status,
        read_proxy_pool_file,
        save_registry,
    )
    from shell import MinerShell

    if TYPE_CHECKING:
        from _typeshed import SupportsWrite

    warnings.simplefilter("default", ResourceWarning)

    # import tracemalloc
    # tracemalloc.start(3)

    if sys.version_info < (3, 10):
        raise RuntimeError("Python 3.10 or higher is required")

    # Suppress X11 Input Method registration on Linux to prevent
    # XWayland/Mutter lockups during heavy Tkinter layout updates.
    if sys.platform.startswith("linux") and "XMODIFIERS" not in os.environ:
        os.environ["XMODIFIERS"] = "@im=none"

    class Parser(argparse.ArgumentParser):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self._message: io.StringIO = io.StringIO()

        def _print_message(self, message: str, file: SupportsWrite[str] | None = None) -> None:
            self._message.write(message)
            # print(message, file=self._message)

        def exit(self, status: int = 0, message: str | None = None) -> NoReturn:
            try:
                super().exit(status, message)  # sys.exit(2)
            finally:
                messagebox.showerror("Argument Parser Error", self._message.getvalue())

    class ParsedArgs(argparse.Namespace):
        _verbose: int
        _debug_ws: bool
        _debug_gql: bool
        log: bool
        tray: bool
        dump: bool

        # TODO: replace int with union of literal values once typeshed updates
        @property
        def logging_level(self) -> int:
            return LOGGING_LEVELS[min(self._verbose, 4)]

        @property
        def debug_ws(self) -> int:
            """
            If the debug flag is True, return DEBUG.
            If the main logging level is DEBUG, return INFO to avoid seeing raw messages.
            Otherwise, return NOTSET to inherit the global logging level.
            """
            if self._debug_ws:
                return logging.DEBUG
            elif self._verbose >= 4:
                return logging.INFO
            return logging.NOTSET

        @property
        def debug_gql(self) -> int:
            if self._debug_gql:
                return logging.DEBUG
            elif self._verbose >= 4:
                return logging.INFO
            return logging.NOTSET

    # handle input parameters
    # NOTE: parser output is shown via message box
    # we also need a dummy invisible window for the parser
    root = tk.Tk()
    root.overrideredirect(True)
    root.withdraw()
    set_root_icon(root, resource_path("icons/pickaxe.ico"))
    root.update()
    parser = Parser(
        SELF_PATH.name,
        description="A program that allows you to mine timed drops on Twitch.",
    )
    parser.add_argument("--version", action="version", version=f"v{__version__}")
    parser.add_argument("-v", dest="_verbose", action="count", default=0)
    parser.add_argument("--tray", action="store_true")
    parser.add_argument("--log", action="store_true")
    parser.add_argument("--dump", action="store_true")
    # undocumented debug args
    parser.add_argument(
        "--debug-ws", dest="_debug_ws", action="store_true", help=argparse.SUPPRESS
    )
    parser.add_argument(
        "--debug-gql", dest="_debug_gql", action="store_true", help=argparse.SUPPRESS
    )
    args = parser.parse_args(namespace=ParsedArgs())
    # load settings
    try:
        settings = Settings(args)
    except Exception:
        messagebox.showerror(
            _("gui", "accounts", "error"),
            "载入设置文件时出错：\n\n" + traceback.format_exc(),
        )
        sys.exit(4)
    # dummy window isn't needed anymore
    root.destroy()
    # get rid of unneeded objects
    del root, parser

    # client run
    async def main() -> None:
        # An absent registry means "plain single-account install": behave exactly
        # like the upstream build, straight out of the working directory.
        entries = load_registry_all()
        if not entries:
            entries = [AccountEntry(DEFAULT_ACCOUNT)]
        # Bind each account to its own exit before anything is built, so the
        # very first request already leaves through the assigned proxy. A pool
        # file that exists makes the binding mandatory - see below.
        pool = read_proxy_pool_file()
        apply_proxy_pool(entries, pool)
        runtimes = [
            AccountRuntime(entry, args) for entry in entries if entry.enabled
        ]

        # Language and log levels are process-wide, so the first account decides.
        primary = runtimes[0]
        primary.build()
        settings = primary.settings

        try:
            _.set_language(settings.language)
        except ValueError:
            # this language doesn't exist - stick to English
            pass

        # handle logging stuff
        if settings.logging_level > logging.DEBUG:
            # redirect the root logger into a NullHandler, effectively ignoring
            # all logging calls that aren't ours.
            logging.getLogger().addHandler(logging.NullHandler())
        logger = logging.getLogger("TwitchDrops")
        logger.setLevel(settings.logging_level)
        logging.getLogger("TwitchDrops.gql").setLevel(settings.debug_gql)
        logging.getLogger("TwitchDrops.websocket").setLevel(settings.debug_ws)

        # remaining accounts construct their own Settings + client
        for runtime in runtimes[1:]:
            runtime.build()

        # Per-account log files, filtered by the account context variable. This
        # has to happen before the accounts are filtered below: an account that
        # gets refused over its missing proxy is exactly the one whose log the
        # user will go looking for, so the handler must already be attached.
        handlers: list[logging.Handler] = []
        for runtime in runtimes:
            if not runtime.settings.log:
                continue
            handler = install_log_handler(runtime.name, runtime.paths)
            if handler is not None:
                handlers.append(handler)

        # One lock file per account. Besides keeping two copies of the whole
        # program from fighting over the same files, this also skips an account
        # that is already running in another instance.
        locks = []
        active: list[AccountRuntime] = []
        for runtime in runtimes:
            # Refuse an account that would leave over the bare connection while
            # a pool is configured: falling back to the direct route would hand
            # the real IP to Twitch, which is worse than not mining at all.
            if pool and not runtime.entry.proxy:
                message = _("gui", "accounts", "no_proxy").format(
                    name=runtime.display_name
                )
                logger.error(message)
                continue
            success, file = lock_file(runtime.paths.lock)
            if not success:
                logger.warning(_("gui", "accounts", "in_use").format(name=runtime.name))
                continue
            locks.append(file)
            active.append(runtime)
        if not active:
            sys.exit(3)
        if pool:
            logger.info(proxy_status([runtime.entry for runtime in active]))

        # one window, one tray icon, one Tk event pump - one tab per account
        shell = MinerShell()

        for runtime in active:
            shell.add_account(runtime)
        shell.refresh_title()

        if settings.tray and sys.platform != "darwin":
            shell.hide()
        else:
            shell.show()

        loop = asyncio.get_running_loop()
        if sys.platform == "linux":
            loop.add_signal_handler(signal.SIGINT, lambda *_: shell.close())
            loop.add_signal_handler(signal.SIGTERM, lambda *_: shell.close())

        exit_status = 0
        # Accounts added at runtime append their task here; the wait loop above
        # picks them up on its next round.
        tasks: list[asyncio.Task[None]] = []

        async def run_account(runtime: AccountRuntime) -> None:
            nonlocal exit_status
            client = runtime.twitch
            gui = runtime.gui
            assert client is not None and gui is not None
            try:
                await runtime.run()
            except CaptchaRequired:
                exit_status = 1
                gui.prevent_close()
                client.print(_("error", "captcha"))
            except Exception:
                exit_status = 1
                gui.prevent_close()
                client.print("Fatal error encountered:\n")
                client.print(traceback.format_exc())
            finally:
                client.print(_("gui", "status", "exiting"))
                await client.shutdown()
            if not gui.close_requested:
                # user didn't request the closure of this account
                gui.tray.change_icon("error")
                client.print(_("status", "terminated"))
                gui.status.update(_("gui", "status", "terminated"))
                shell.grab_attention(sound=True)
            # the window stays open so the error is readable; the account only
            # finishes once the user closes it
            await gui.wait_until_closed()
            # save after wait, the user may still edit settings in the meantime
            client.save(force=True)
            gui.stop()
            gui.close_window()

        def add_account_at_runtime(raw_name: str) -> str | None:
            """
            Build, register and start one account while the program runs.

            Returns an error message for the caller to display, or None on
            success. Everything the startup path does for an account happens
            here too, in the same order, so a tab added at runtime is
            indistinguishable from one created at launch.
            """
            try:
                entry = AccountEntry(raw_name)
            except ValueError:
                return _("gui", "accounts", "invalid")
            if any(rt.name.casefold() == entry.name.casefold() for rt in runtimes):
                return _("gui", "accounts", "duplicate").format(name=entry.name)
            # A new account joins the same split as the running ones: it takes a
            # free proxy before it is built, and is refused when the pool is
            # exhausted rather than leaving over the bare connection.
            if pool:
                registry = load_registry_all()
                registry.append(entry)
                assign_proxies(registry, pool)
                entry = next(
                    e for e in registry
                    if e.name.casefold() == entry.name.casefold()
                )
                save_registry(registry)
                if not entry.proxy:
                    return _("gui", "accounts", "no_proxy").format(
                        name=entry.name
                    )
            runtime = AccountRuntime(entry, args)
            try:
                runtime.build()
            except Exception:
                logger.exception(f"Could not create account {entry.name!r}")
                return _("gui", "accounts", "error")
            success, lock = lock_file(runtime.paths.lock)
            if not success:
                lock.close()
                return _("gui", "accounts", "in_use").format(name=entry.name)
            handler = None
            # the new account inherits the log level of the account that started
            # it: `--log` is process-wide, so mirror the primary account instead
            if runtime.settings is not None and runtime.settings.log:
                handler = install_log_handler(entry.name, runtime.paths)
                if handler is not None:
                    handlers.append(handler)
            try:
                shell.add_account(runtime)
            except Exception:
                logger.exception(f"Could not build the tab for account {entry.name!r}")
                if handler is not None:
                    logger.removeHandler(handler)
                    handler.close()
                lock.close()
                return _("gui", "accounts", "error")
            # persist before starting, so a crash still leaves a usable registry
            registry = load_registry_all()
            if not any(e.name.casefold() == entry.name.casefold() for e in registry):
                registry.append(entry)
                save_registry(registry)
            runtimes.append(runtime)
            locks.append(lock)
            tasks.append(asyncio.create_task(run_account(runtime)))
            shell.refresh_title()
            shell.show()
            return None

        shell.add_account_hook = add_account_at_runtime

        try:
            tasks.extend(asyncio.create_task(run_account(runtime)) for runtime in active)
            # The account registry can grow while the program runs (the "+" tab),
            # so wait in rounds instead of one `gather` over a fixed list.
            while True:
                pending = [task for task in tasks if not task.done()]
                if not pending:
                    break
                await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        finally:
            if sys.platform == "linux":
                loop.remove_signal_handler(signal.SIGINT)
                loop.remove_signal_handler(signal.SIGTERM)
            if shell.tray is not None:
                shell.tray.stop()
            for handler in handlers:
                logger.removeHandler(handler)
                handler.close()
            for file in locks:
                file.close()

        sys.exit(exit_status)

    try:
        asyncio.run(main())
    finally:
        pass
