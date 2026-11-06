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

from yarl import URL

from constants import FILE_FORMATTER, WORKING_DIR, AccountPaths
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

# Optional list of exits, one per line. Present means "every enabled account
# must get its own entry from here"; absent means "the per-account settings
# file decides", which is what a plain single-account install does.
PROXY_POOL_PATH: Path = Path(WORKING_DIR, "proxies.txt")

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


class AccountFilter(logging.Filter):
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


def install_log_handler(name: str, paths: AccountPaths) -> logging.Handler | None:
    """
    Attach this account's own `log.txt`, when the user asked for a log file.

    Shared by the startup path and by accounts added while the program is
    already running, so a tab created later logs exactly like the first one.
    """
    runtime_logger = logging.getLogger("TwitchDrops")
    paths.ensure()
    try:
        # Without an explicit encoding the handler uses the locale default,
        # which mangles every non-ASCII message on a Chinese Windows install.
        handler = logging.FileHandler(paths.log, encoding="utf-8")
    except OSError:
        logger.warning(f"Could not open the log file for account {name!r}")
        return None
    handler.setFormatter(FILE_FORMATTER)
    handler.addFilter(AccountFilter(name))
    runtime_logger.addHandler(handler)
    return handler


_PROXY_SCHEMES = frozenset({"http", "https", "socks5", "socks4", "socks5h", "socks4a"})
# A host is either a dotted IPv4 literal or a plain hostname. Anything with a
# space, a slash, or a stray comma is a pasted comment, not a proxy, and must be
# rejected instead of being turned into a URL that fails much later.
_PROXY_HOST_RE = re.compile(r"^[A-Za-z0-9._-]+$|^\[[0-9A-Fa-f:]+\]$")


def validate_proxy(raw: str) -> str:
    r"""
    Return a normalised proxy URL, or raise ValueError.

    Only the parts we can actually check are enforced: a known scheme, a
    plausible host and an explicit port. Credentials are allowed and passed
    through unchanged, since a per-account authenticated proxy is the point of
    this field. An empty string means "no proxy for this account".
    """
    cleaned = raw.strip()
    if not cleaned:
        return ""
    if "://" not in cleaned:
        # bare host:port is what people paste from a proxy list
        cleaned = f"http://{cleaned}"
    parsed = URL(cleaned)
    if parsed.scheme not in _PROXY_SCHEMES:
        raise ValueError(f"unsupported proxy scheme: {parsed.scheme!r}")
    host = parsed.host or ""
    if not host or not _PROXY_HOST_RE.match(host):
        raise ValueError(f"proxy host looks wrong: {host!r}")
    # `port` falls back to the scheme default, so a line without a port would
    # silently become :80 - which is not what anyone meant to configure.
    if parsed.explicit_port is None:
        raise ValueError(f"proxy needs an explicit port: {raw!r}")
    if not (0 < parsed.explicit_port < 65536):
        raise ValueError(f"proxy port out of range: {parsed.explicit_port}")
    return cleaned


class AccountEntry:
    """Persisted description of one account."""

    __slots__ = ("name", "enabled", "proxy")

    def __init__(self, name: str, *, enabled: bool = True, proxy: str = ""):
        self.name = validate_name(name)
        self.enabled = enabled
        # Empty means "inherit whatever the account's own settings file says",
        # which keeps accounts.json optional for people who set the proxy in
        # the settings page instead.
        self.proxy = validate_proxy(proxy)

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {"name": self.name, "enabled": self.enabled}
        if self.proxy:
            data["proxy"] = self.proxy
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> AccountEntry:
        name = validate_name(str(data.get("name", "")))
        return cls(
            name,
            enabled=bool(data.get("enabled", True)),
            proxy=str(data.get("proxy", "") or ""),
        )

    def __repr__(self) -> str:
        return f"AccountEntry({self.name!r}, enabled={self.enabled}, proxy={self.proxy!r})"


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


def assign_proxies(
    entries: list[AccountEntry], pool: list[str]
) -> list[AccountEntry]:
    """
    Give every enabled account a distinct proxy from `pool`.

    Accounts that already carry a proxy keep it - a re-run must not silently
    move an account onto a different exit IP, because a stable exit is worth
    more than a fresh one when it comes to avoiding automated checks.

    Accounts without a proxy are matched against the ports that are still free,
    in registry order. When the pool runs out the remaining accounts are left
    without a proxy, and `missing_proxies()` reports them so the caller can
    refuse to start them instead of letting them go out over the bare
    connection.
    """
    used = {entry.proxy for entry in entries if entry.proxy}
    free = [proxy for proxy in pool if proxy not in used]
    for entry in entries:
        if not entry.enabled or entry.proxy:
            continue
        if not free:
            break
        entry.proxy = free.pop(0)
    return entries


def missing_proxies(entries: list[AccountEntry]) -> list[AccountEntry]:
    """Enabled accounts that would still leave over the bare connection."""
    return [entry for entry in entries if entry.enabled and not entry.proxy]


def read_proxy_pool_file(path: Path | None = None) -> list[str]:
    """
    Load the proxy pool from disk, or return an empty list when it is absent.

    An empty list is meaningful: the caller treats it as "no pool configured"
    and leaves each account's proxy to its own settings file.
    """
    target = PROXY_POOL_PATH if path is None else path
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError:
        logger.warning(f"Could not read the proxy pool at {target}")
        return []
    return read_proxy_pool(raw)


def apply_proxy_pool(
    entries: list[AccountEntry], pool: list[str]
) -> list[AccountEntry]:
    """
    Bind each enabled account to its own proxy from `pool`, then persist.

    Called once at startup and again whenever an account is added, so a new
    account never goes out over the bare connection while the pool still has a
    free entry.
    """
    if pool:
        assign_proxies(entries, pool)
        save_registry(entries)
    return entries


def proxy_status(entries: list[AccountEntry]) -> str:
    """One-line summary for the log and the settings page."""
    enabled = [entry for entry in entries if entry.enabled]
    if not enabled:
        return "no enabled accounts"
    bound = sum(1 for entry in enabled if entry.proxy)
    return f"{bound}/{len(enabled)} enabled accounts have their own proxy"


def read_proxy_pool(raw: str) -> list[str]:
    """
    Parse a proxy list pasted as text.

    One proxy per line, `#` starts a comment, blank lines are skipped, and a
    bare `host:port` gets an `http://` scheme. Duplicates are collapsed so the
    same exit is never handed to two accounts.
    """
    pool: list[str] = []
    seen: set[str] = set()
    for line in raw.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        try:
            proxy = validate_proxy(line)
        except ValueError:
            logger.warning(f"Skipping invalid proxy line: {line!r}")
            continue
        if proxy in seen:
            continue
        seen.add(proxy)
        pool.append(proxy)
    return pool


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
        # The registry wins over the per-account settings file: that is the only
        # way to change an account's exit point without opening its settings
        # page, and it keeps the whole split visible in one place.
        if self.entry.proxy:
            self.settings.proxy = URL(self.entry.proxy)
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
