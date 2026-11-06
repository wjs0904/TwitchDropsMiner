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
    from constants import LOGGING_LEVELS, SELF_PATH, FILE_FORMATTER
    from accounts import (
        AccountEntry,
        AccountRuntime,
        DEFAULT_ACCOUNT,
        CURRENT_ACCOUNT,
        load_registry,
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

    class _AccountFilter(logging.Filter):
        """
        Keeps a per-account log file free of other accounts' records.

        Records emitted outside any account task (startup, shell, shutdown) are
        broadcast, which is why the `not current` case passes.
        """

        def __init__(self, account: str) -> None:
            super().__init__()
            self._account = account

        def filter(self, record: logging.LogRecord) -> bool:
            current = CURRENT_ACCOUNT.get()
            return not current or current == self._account

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
        entries = load_registry()
        if not entries:
            entries = [AccountEntry(DEFAULT_ACCOUNT)]
        runtimes = [AccountRuntime(entry, args) for entry in entries]

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

        # One lock file per account. Besides keeping two copies of the whole
        # program from fighting over the same files, this also skips an account
        # that is already running in another instance.
        locks = []
        active: list[AccountRuntime] = []
        for runtime in runtimes:
            success, file = lock_file(runtime.paths.lock)
            if not success:
                logger.warning(_("gui", "accounts", "in_use").format(name=runtime.name))
                continue
            locks.append(file)
            active.append(runtime)
        if not active:
            sys.exit(3)

        # per-account log files, filtered by the account context variable
        handlers: list[logging.Handler] = []
        for runtime in active:
            if not runtime.settings.log:
                continue
            runtime.paths.ensure()
            handler = logging.FileHandler(runtime.paths.log)
            handler.setFormatter(FILE_FORMATTER)
            handler.addFilter(_AccountFilter(runtime.name))
            logger.addHandler(handler)
            handlers.append(handler)

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

        try:
            await asyncio.gather(*(run_account(runtime) for runtime in active))
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
