"""
Multi-account support.

Design notes
------------
All accounts run inside a **single** process and a **single** event loop, so every
per-account resource must be an explicit object rather than a module-level global.
That is the whole reason `AccountPaths` exists: `COOKIES_PATH` / `SETTINGS_PATH` /
`CACHE_PATH` used to be module constants, and two concurrently running accounts
would overwrite each other's files.

Log routing between accounts is done with a `ContextVar`. Every account task is
started through `AccountRuntime._run_context`, which sets `CURRENT_ACCOUNT` inside
the task's own context copy. Child tasks inherit that context, and the per-account
logging handler installed on the console widget filters on it.
"""

from __future__ import annotations

import contextvars
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from constants import WORKING_DIR, AccountPaths
from translate import _
from utils import json_load, json_save

if TYPE_CHECKING:
    from settings import Settings
    from twitch import Twitch
    from gui import GUIManager
    from main import ParsedArgs

logger = logging.getLogger("TwitchDrops")

# Name of the account whose coroutine tree is currently executing.
# Empty string means "shell/global" - those records go to every console.
CURRENT_ACCOUNT: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_account", default=""
)

REGISTRY_PATH: Path = Path(WORKING_DIR, "accounts.json")

# Marker value stored in the registry for the account that lives in the working dir.
DEFAULT_ACCOUNT = "default"


# Account names become directory names under `accounts/`, so they have to be
# safe to use as a path segment: no separators, no dots-dots, no reserved
# device names. CJK is allowed so the tab labels can stay readable.
_NAME_RE = re.compile(r"^[^\x00-<>:\"/\|?*]{1,32}$")
_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
)


def validate_name(name: str) -> str:
    r"""
    Return a normalised account name, or raise ValueError.

    The name is used verbatim as a directory name, so anything that could
    escape `accounts/` (`..`, `/`, `\`) or hit a Windows device name is
    rejected instead of being silently sanitised - silently rewriting a name
    would make an existing account unreachable.
    """
    cleaned = name.strip()
    if not cleaned:
        raise ValueError("account name is empty")
    if not _NAME_RE.match(cleaned):
        raise ValueError(f"account name contains forbidden characters: {name!r}")
    if cleaned in {".", ".."} or cleaned.endswith(".") or cleaned.endswith(" "):
        raise ValueError(f"account name is not a valid directory name: {name!r}")
    if cleaned.casefold() in _RESERVED_NAMES:
        raise ValueError(f"account name is a reserved device name: {name!r}")
    return cleaned


class AccountEntry:
    """Persisted description of one account."""

    __slots__ = ("name", "enabled")

    def __init__(self, name: str, *, enabled: bool = True):
        self.name = validate_name(name)
        self.enabled = enabled

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "enabled": self.enabled}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> AccountEntry:
        name = validate_name(str(data.get("name", "")))
        return cls(name, enabled=bool(data.get("enabled", True)))

    def __repr__(self) -> str:
        return f"AccountEntry({self.name!r}, enabled={self.enabled})"


def load_registry() -> list[AccountEntry]:
    """
    Read `accounts.json`.

    A missing (or empty) registry means "single account, old behaviour": the
    program runs exactly like the upstream version, straight out of the working
    directory. Disabled accounts are filtered out - use `load_registry_all()`
    when they have to stay visible.
    """
    return [entry for entry in load_registry_all() if entry.enabled]


def load_registry_all() -> list[AccountEntry]:
    """
    Read `accounts.json` keeping disabled entries.

    The account manager needs to show - and be able to re-enable - accounts that
    are currently switched off, so the filter that the mining loop applies must
    not be applied here.
    """
    raw = json_load(REGISTRY_PATH, {"accounts": []}, merge=False)
    entries: list[AccountEntry] = []
    seen: set[str] = set()
    for item in raw.get("accounts", []):
        try:
            entry = AccountEntry.from_json(item)
        except (ValueError, AttributeError, TypeError):
            logger.warning(f"Skipping invalid account entry: {item!r}")
            continue
        key = entry.name.casefold()
        if key in seen:
            logger.warning(f"Duplicate account name ignored: {entry.name!r}")
            continue
        seen.add(key)
        entries.append(entry)
    return entries


def save_registry(entries: list[AccountEntry]) -> None:
    json_save(
        REGISTRY_PATH,
        {"accounts": [entry.to_json() for entry in entries]},
        sort=True,
    )


def paths_for(entry: AccountEntry) -> AccountPaths:
    return AccountPaths.for_account(entry.name, WORKING_DIR)


class AccountRuntime:
    """
    Owns one account's settings, Twitch client and GUI manager.

    The GUI manager is created lazily through `attach`, because the Tk widgets for
    an account can only be built once the shell has created its tab.
    """

    __slots__ = (
        "entry", "paths", "args", "settings", "twitch", "gui",
        "shell", "tab", "task", "status",
    )

    def __init__(self, entry: AccountEntry, args: ParsedArgs):
        self.entry = entry
        self.paths: AccountPaths = paths_for(entry)
        self.args = args
        self.settings: Settings | None = None
        self.twitch: Twitch | None = None
        self.gui: GUIManager | None = None
        self.shell: Any = None
        self.tab: Any = None
        self.task: Any = None
        self.status: str = "starting"

    # -- identity ---------------------------------------------------------
    @property
    def name(self) -> str:
        return self.entry.name

    @property
    def display_name(self) -> str:
        if self.name == DEFAULT_ACCOUNT:
            return _("gui", "accounts", "default")
        return self.name

    # -- construction -----------------------------------------------------
    def build(self) -> None:
        """Instantiate settings + client. The GUI is attached later by the shell."""
        from settings import Settings
        from twitch import Twitch

        self.paths.ensure()
        self.settings = Settings(self.args, self.paths)
        self.twitch = Twitch(self.settings, self.paths)

    def attach(self, shell: Any, tab: Any) -> None:
        """Build this account's widgets inside `tab`, owned by `shell`."""
        from gui import GUIManager

        self.shell = shell
        self.tab = tab
        assert self.twitch is not None
        self.gui = self.twitch.gui = GUIManager(self.twitch, shell=shell, master=tab)

    # -- execution --------------------------------------------------------
    async def run(self) -> int:
        """
        Run this account until it exits.

        `CURRENT_ACCOUNT` is set *inside* the task's own context copy, so it is
        visible to every child task this account creates and invisible to the
        other accounts. This is what lets one global console handler filter
        records per account without touching a single `logger.info(...)` call.
        """
        assert self.twitch is not None
        CURRENT_ACCOUNT.set(self.name)
        self.status = "running"
        try:
            await self.twitch.run()
        except Exception:
            self.status = "error"
            raise
        finally:
            self.status = "stopped"
        return 0

    def __repr__(self) -> str:
        return f"AccountRuntime({self.name!r}, status={self.status!r})"
