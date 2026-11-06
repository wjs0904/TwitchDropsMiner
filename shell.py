"""
Single window, several accounts.

`GUIManager` used to own the Tk root, the event pump, the tray icon and the
Windows window procedure. All four are process-wide resources: two of them cannot
exist twice, and the pump must not be duplicated either. `MinerShell` takes those
over and gives every account nothing but a notebook tab to build its widgets in.

The tabs are the account selector, so the "currently selected account" is simply
the currently selected tab; the tray icon and the theme follow it.
"""

from __future__ import annotations

import sys
import ctypes
import asyncio
import logging
import tkinter as tk
from tkinter import ttk
from typing import Any, Callable, NoReturn, TYPE_CHECKING

from constants import WINDOW_TITLE, State
from utils import resource_path, set_root_icon, _T
from translate import _

if sys.platform == "win32":
    import win32api
    import win32con
    import win32gui

if TYPE_CHECKING:
    from gui import GUIManager, TrayIcon
    from twitch import Twitch


logger = logging.getLogger("TwitchDrops")


class MinerShell:
    """
    Owns the one and only Tk root and everything that is not per-account.

    Accounts are registered with `add_account`, which creates a tab. Awaiting
    `wait_until_closed` blocks until the user closes the shared window, and
    `close` marks every account for shutdown.
    """

    def __init__(self) -> None:
        self.tabs: list[GUIManager] = []
        self.tray: TrayIcon | None = None
        self._poll_task: asyncio.Task[NoReturn] | None = None
        self._closing = asyncio.Event()
        self._active: GUIManager | None = None

        self.root = root = tk.Tk(className=WINDOW_TITLE)
        # withdraw immediately to prevent the window from flashing
        root.withdraw()
        set_root_icon(root, resource_path("icons/pickaxe.ico"))
        root.title(WINDOW_TITLE)
        root.bind_all("<KeyPress-Escape>", self._on_escape)

        self._style = ttk.Style(root)
        self._orig_theme: str = ''
        try:
            self._orig_theme = self._style.theme_use()
        except Exception:
            self._orig_theme = ''

        # a host frame that fills the window, holding the account notebook
        self._host = ttk.Frame(root, padding=8)
        self._host.grid(column=0, row=0, sticky="nsew")
        root.rowconfigure(0, weight=1)
        root.columnconfigure(0, weight=1)
        self._notebook = ttk.Notebook(self._host)
        self._notebook.grid(column=0, row=0, sticky="nsew")
        self._host.rowconfigure(0, weight=1)
        self._host.columnconfigure(0, weight=1)
        self._notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        # Windows shutdown handling lives at the window level
        if sys.platform == "win32":
            root.update()
            self._message_map = {
                win32con.WM_CLOSE: self.close,
                win32con.WM_QUERYENDSESSION: self.close,
            }
            self._old_wnd_proc = win32gui.SetWindowLong(
                self._handle, win32con.GWL_WNDPROC, self._wnd_proc
            )
            ctypes.windll.user32.ShutdownBlockReasonCreate(
                self._handle, ctypes.c_wchar_p(_("gui", "status", "exiting"))
            )
        else:
            root.protocol("WM_DELETE_WINDOW", self.close)
            root.protocol("WM_DESTROY_WINDOW", self.close)

    # --------------------------------------------------------------- window
    @property
    def _handle(self) -> int:
        return int(self.root.wm_frame(), 16)

    def _wnd_proc(self, hwnd, msg, w_param, l_param):
        if msg == win32con.WM_DESTROY:
            win32api.SetWindowLong(self._handle, win32con.GWL_WNDPROC, self._old_wnd_proc)
        if msg in self._message_map:
            return self._message_map[msg](w_param, l_param)
        return win32gui.CallWindowProc(self._old_wnd_proc, hwnd, msg, w_param, l_param)

    def show(self) -> None:
        self.root.deiconify()

    def hide(self) -> None:
        self.root.withdraw()

    # -------------------------------------------------------------- accounts
    def add_account(self, runtime: Any) -> GUIManager:
        """Create a tab for `runtime` and build its widgets inside it."""
        frame = ttk.Frame(self._notebook)
        self._notebook.add(frame, text=runtime.display_name)
        runtime.attach(self, frame)
        gui = runtime.gui
        assert gui is not None
        # widgets are built with ttk defaults, so push the palette down now
        gui.apply_theme(gui._twitch.settings.dark_mode)
        self.tabs.append(gui)
        if self._active is None and len(self.tabs) == 1:
            self._active = gui
        return gui

    def detach_account(self, gui: GUIManager) -> None:
        """Remove one account's tab; tear the window down when the last one goes."""
        if gui in self.tabs:
            self.tabs.remove(gui)
        if self._active is gui:
            self._active = self.tabs[0] if self.tabs else None
        if not self.tabs:
            self._teardown()

    def _on_tab_changed(self, event: Any) -> None:
        index = self._notebook.index("current")
        if 0 <= index < len(self.tabs):
            gui = self.tabs[index]
            if gui is not self._active:
                self._active = gui
                # the tray icon and the theme are shared - follow the selection
                tray = getattr(gui, "tray", None)
                if tray is not None and hasattr(tray, "resync"):
                    tray.resync()
                gui.apply_theme(gui._twitch.settings.dark_mode)
            self.root.focus_set()

    @property
    def active_gui(self) -> GUIManager | None:
        return self._active

    @property
    def _theme_dark(self) -> bool:
        return self._active._twitch.settings.dark_mode if self._active else False

    # ------------------------------------------------------------------ tray
    def ensure_tray_icon(self, factory: Callable[[], TrayIcon]) -> TrayIcon:
        """pystray owns a worker thread per icon, so exactly one may exist."""
        if self.tray is None:
            self.tray = factory()
        return self.tray

    # ------------------------------------------------------------- event pump
    def start_poll(self) -> None:
        if self._poll_task is None:
            self._poll_task = asyncio.create_task(self._poll())

    async def _poll(self) -> None:
        """
        Runs the single Tk event loop for the whole process via asyncio.

        0.05s gives similar performance and CPU usage to `mainloop`, without
        threads, thread safety, `loop.call_soon_threadsafe` or futures.
        """
        do_one_event = self.root.dooneevent
        DONT_WAIT = 1 << 1  # TKINTER_DONT_WAIT

        while True:
            try:
                while do_one_event(DONT_WAIT):
                    pass
            except tk.TclError:
                # root window was destroyed
                break
            await asyncio.sleep(0.05)

        self._poll_task = None

    # ---------------------------------------------------------------- closing
    def close(self, *args) -> int:
        """
        Window close request: ask every account to shut down.

        This is what `wait_until_closed` waits for - the window itself is only
        destroyed later, once every account has finished its shutdown sequence
        (that ordering is what keeps `save(force=True)` from losing settings the
        user changed right before closing).
        """
        self._closing.set()
        for gui in list(self.tabs):
            gui._close_requested.set()
            try:
                gui._twitch.close()
            except Exception:
                logger.exception(f"Error while closing account {gui._twitch.paths.name}")
        return 0

    @property
    def close_requested(self) -> bool:
        return self._closing.is_set()

    async def wait_until_closed(self) -> None:
        await self._closing.wait()

    def _teardown(self) -> None:
        self._closing.set()
        if self._poll_task is not None:
            self._poll_task.cancel()
            self._poll_task = None
        if sys.platform == "win32":
            try:
                ctypes.windll.user32.ShutdownBlockReasonDestroy(self._handle)
            except Exception:
                pass
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    # ------------------------------------------------------------------ misc
    def _on_escape(self, event: Any) -> None:
        if self._active is not None:
            self._active.unfocus(event)

    def refresh_title(self) -> None:
        names = ", ".join(gui._twitch.paths.name for gui in self.tabs)
        self.root.title(f"{WINDOW_TITLE} [{names}]" if names else WINDOW_TITLE)

    def grab_attention(self, *, sound: bool = True) -> None:
        if self.tray is not None:
            self.tray.restore()
        self.root.focus_set()
        if sound:
            self.root.bell()
