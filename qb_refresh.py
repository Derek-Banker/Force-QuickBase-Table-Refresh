"""Refresh a connected Quickbase table through its normal headless browser UI."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit


_TASK_FIELDS = ("currentRefresh", "lastRefresh", "lastGoodRefresh")


class RefreshError(Exception):
    """An error that is safe to return through the CLI or API."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class TableTarget:
    """A configured table URL and optional refresh discovery overrides."""

    url: str
    replication_id: str | None = None
    refresh_selector: str | None = None


@dataclass(frozen=True)
class RefreshSettings:
    """Browser configuration shared by the CLI and API."""

    realm_url: str
    tables: dict[str, TableTarget]
    state_file: Path
    browser_channel: str = "chromium"
    navigation_timeout_seconds: float = 60
    start_timeout_seconds: float = 60
    refresh_timeout_seconds: float = 300
    poll_interval_seconds: float = 5
    username_selector: str = "input[name='loginid'], input[name='login'], input[name='username'], input[type='email'], #login"
    password_selector: str = "input[type='password']"
    submit_selector: str = "button[type='submit'], input[type='submit']"

    @classmethod
    def from_file(cls, path: str | Path) -> RefreshSettings:
        """Validate a JSON configuration and resolve its relative state path."""
        config_path = Path(path).resolve()
        try:
            data = json.loads(config_path.read_text(encoding="utf-8-sig"))
            if not isinstance(data, dict):
                raise ValueError
            realm = str(data["realm_url"]).rstrip("/")
            parsed = urlsplit(realm)
            if (parsed.scheme != "https" or not parsed.hostname
                    or not parsed.hostname.endswith(".quickbase.com")
                    or parsed.username or parsed.password or parsed.query
                    or parsed.fragment or parsed.path not in ("", "/")):
                raise ValueError
            targets = {}
            for name, target in data["tables"].items():
                if not re.fullmatch(r"[a-zA-Z0-9_-]+", name):
                    raise ValueError
                url = str(target["url"])
                parts = urlsplit(url)
                if (parts.scheme, parts.netloc) != (parsed.scheme, parsed.netloc):
                    raise ValueError
                if parts.username or parts.password or parts.fragment:
                    raise ValueError
                query_keys = {key.lower() for key in parse_qs(parts.query)}
                if query_keys & {"token", "usertoken", "ticket", "password"}:
                    raise ValueError
                replication_id = target.get("replication_id")
                if replication_id is not None:
                    replication_id = str(replication_id)
                    if not replication_id.isdigit():
                        raise ValueError
                selector = target.get("refresh_selector")
                if selector is not None and (not isinstance(selector, str) or not selector.strip()):
                    raise ValueError
                targets[name] = TableTarget(url, replication_id, selector)
            if not targets:
                raise ValueError
            state_file = Path(data.get("state_file", ".state/browser.json"))
            if not state_file.is_absolute():
                state_file = config_path.parent / state_file
            options = {key: data[key] for key in (
                "browser_channel", "navigation_timeout_seconds", "start_timeout_seconds",
                "refresh_timeout_seconds", "poll_interval_seconds", "username_selector",
                "password_selector", "submit_selector") if key in data}
            settings = cls(realm, targets, state_file.resolve(), **options)
            if settings.browser_channel not in ("chromium", "chrome", "msedge"):
                raise ValueError
            for selector in (settings.username_selector, settings.password_selector, settings.submit_selector):
                if not isinstance(selector, str) or not selector.strip():
                    raise ValueError
            for duration in (settings.navigation_timeout_seconds, settings.start_timeout_seconds,
                             settings.refresh_timeout_seconds, settings.poll_interval_seconds):
                if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not 0 < duration <= 3600:
                    raise ValueError
            if settings.poll_interval_seconds < 1:
                raise ValueError
            return settings
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            raise RefreshError("CONFIG_ERROR", "Cannot read the configuration. Check its paths, HTTPS realm, tables, and timeouts.") from None


@dataclass(frozen=True)
class RefreshResult:
    """A confirmed refresh result with UTC timestamps and elapsed seconds."""

    table: str
    replication_id: str
    refresh_id: str
    started_at: str
    finished_at: str
    duration_seconds: float
    message: str = "The connected table refreshed successfully."
    status: str = "completed"

    def to_dict(self) -> dict[str, Any]:
        """Return the fields in the CLI and API success response."""
        return asdict(self)


def load_environment(path: str | Path = ".env") -> None:
    """Read local environment settings without replacing existing variables."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        raise RefreshError("DEPENDENCY_MISSING", "Install the project requirements before running this command.") from None
    load_dotenv(dotenv_path=path, override=False)


@contextmanager
def session_lock(state_file: Path):
    """Prevent simultaneous browser sessions from changing the saved state."""
    state_file.parent.mkdir(parents=True, exist_ok=True)
    handle = open(str(state_file) + ".lock", "a+b")
    locked = False
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise RefreshError("BUSY", "Another process is using this browser session. Try again after it finishes.") from None
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise RefreshError("BUSY", "Another process is using this browser session. Try again after it finishes.") from None
        locked = True
        yield
    finally:
        if locked:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def task_identity(task: Any) -> str | None:
    """Prefer task IDs, then use the start time as a fallback identity."""
    if not isinstance(task, dict):
        return None
    for key in ("id", "taskId", "startTime"):
        if task.get(key) is not None:
            return f"{key}:{task[key]}"
    return None


def known_tasks(payload: dict[str, Any]) -> set[str]:
    """Collect task identities that exist before the refresh button click."""
    return {identity for key in _TASK_FIELDS
            if (identity := task_identity(payload.get(key))) is not None}


def safe_failure(message: Any, default_code: str = "REFRESH_FAILED") -> RefreshError:
    """Map upstream content to controlled errors without returning its secrets."""
    value = str(message).lower()
    if "captcha" in value or "couldn't verify the refresh" in value:
        return RefreshError("CAPTCHA_FAILED", "Quickbase rejected the browser CAPTCHA check. The script did not retry the refresh.")
    if any(text in value for text in ("sign in", "authentication", "unauthorized", "login required")):
        return RefreshError("AUTHENTICATION_REQUIRED", "Quickbase requires a valid browser login. Check the saved session or account credentials.")
    return RefreshError(default_code, "Quickbase did not complete the refresh. Check the connected table's refresh history.")


@dataclass
class RefreshTracker:
    """Correlate status responses with one new task for the configured table.

    The baseline contains tasks that exist before the click. The first new
    identity becomes the expected task. Only its successful lastRefresh completes
    the operation.
    """

    replication_id: str
    baseline: set[str] = field(default_factory=set)
    expected: str | None = None
    baseline_error: Any = None

    def observe(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Return the matching success, raise a safe error, or continue polling."""
        current = payload.get("currentRefresh")
        last = payload.get("lastRefresh")
        for task in (current, last):
            if not isinstance(task, dict):
                continue
            if str(task.get("replicationId", self.replication_id)) != self.replication_id:
                raise RefreshError("UNEXPECTED_RESPONSE", "Quickbase returned status for a different connected table.")
            identity = task_identity(task)
            if self.expected is None and identity and identity not in self.baseline:
                self.expected = identity
        if self.expected and task_identity(last) == self.expected:
            if last.get("status") == 3:
                return last
            if last.get("endTime") or not current:
                raise safe_failure(last.get("message") or payload.get("lastError"))
        # A rejected start can return HTTP 200 with no new task and a lastError.
        if not current and payload.get("lastError") and payload.get("lastError") != self.baseline_error:
            raise safe_failure(payload["lastError"])
        return None


def same_origin(url: str, realm: str) -> bool:
    """Compare schemes and authorities before browser or request inspection."""
    first, second = urlsplit(url), urlsplit(realm)
    return (first.scheme, first.netloc) == (second.scheme, second.netloc)


def refresh_request_id(url: str, realm: str, method: str) -> str | None:
    """Extract a replication ID only from a matching Quickbase refresh request."""
    if not same_origin(url, realm):
        return None
    parts = urlsplit(url)
    if method == "POST" and parts.path.rstrip("/") == "/qb/sync/refreshTasks":
        return parse_qs(parts.query).get("replicationID", [None])[0]
    if method == "GET":
        match = re.fullmatch(r"/qb/sync/refreshTasks/(\d+)/?", parts.path)
        if match:
            return match.group(1)
    return None


async def first_visible(locators):
    """Return the first visible match, with at most 20 matches per locator."""
    for locator in locators:
        for index in range(min(await locator.count(), 20)):
            candidate = locator.nth(index)
            if await candidate.is_visible():
                return candidate
    return None


def quickbase_frames(page, realm_url: str):
    """Select frames on the configured realm or Quickbase login origin."""
    return [frame for frame in page.frames
            if same_origin(frame.url, realm_url)
            or same_origin(frame.url, "https://login.quickbase.com")]


async def refresh_button(page, target: TableTarget, realm_url: str):
    """Find the table refresh control through an override or known UI labels."""
    label = re.compile(r"^refresh\s+data$", re.I)
    for frame in quickbase_frames(page, realm_url):
        if target.refresh_selector:
            button = await first_visible([frame.locator(target.refresh_selector)])
        else:
            button = await first_visible([
                frame.get_by_role("button", name=label),
                frame.get_by_role("link", name=label),
                frame.locator("a[href*='/action/RefreshTableButton']"),
                frame.locator("[title='Refresh data'], [title='Refresh Data'], [aria-label='Refresh data'], [aria-label='Refresh Data']"),
                frame.get_by_text(label, exact=True),
            ])
        if button is not None:
            return button
    return None


async def ensure_ready(page, settings: RefreshSettings, target: TableTarget):
    """Open the table and complete a supported login until its refresh control appears."""
    await page.goto(target.url, wait_until="domcontentloaded")
    deadline = time.monotonic() + settings.navigation_timeout_seconds
    username_sent = False
    password_sent = False
    while time.monotonic() < deadline:
        allowed_origin = (same_origin(page.url, settings.realm_url)
                          or same_origin(page.url, "https://login.quickbase.com"))
        if not allowed_origin:
            raise RefreshError("AUTHENTICATION_REQUIRED", "The account requires an external sign-in flow. A saved, valid browser session is required.")
        button = await refresh_button(page, target, settings.realm_url)
        if button is not None:
            return button
        frames = quickbase_frames(page, settings.realm_url)
        username = await first_visible([frame.locator(settings.username_selector) for frame in frames])
        password = await first_visible([frame.locator(settings.password_selector) for frame in frames])
        if password is not None and not password_sent:
            login_name, login_password = os.getenv("QB_USERNAME"), os.getenv("QB_PASSWORD")
            if not login_name or not login_password:
                raise RefreshError("AUTHENTICATION_REQUIRED", "Set QB_USERNAME and QB_PASSWORD locally, or supply a valid saved browser session.")
            if username is None and not username_sent:
                raise RefreshError("AUTHENTICATION_REQUIRED", "Cannot find the username field. Check the configured username_selector before trying again.")
            if username is not None:
                await username.fill(login_name)
            await password.fill(login_password)
            submit = await first_visible([frame.locator(settings.submit_selector) for frame in frames])
            if submit is None:
                raise RefreshError("AUTHENTICATION_REQUIRED", "Cannot find the login submit control. Check the configured login selectors.")
            password_sent = True
            await submit.click()
        elif username is not None and not username_sent and password is None:
            login_name = os.getenv("QB_USERNAME")
            if not login_name:
                raise RefreshError("AUTHENTICATION_REQUIRED", "Set QB_USERNAME and QB_PASSWORD locally, or supply a valid saved browser session.")
            await username.fill(login_name)
            submit = await first_visible([frame.locator(settings.submit_selector) for frame in frames])
            if submit is not None:
                username_sent = True
                await submit.click()
        await asyncio.sleep(0.25)
    if password_sent or username_sent or "signin" in page.url.lower() or "login" in page.url.lower():
        raise RefreshError("AUTHENTICATION_REQUIRED", "Browser login did not finish. Check credentials, MFA requirements, and the account's sign-in policy.")
    raise RefreshError("REFRESH_CONTROL_NOT_FOUND", "Cannot find Refresh Data. Check the target URL, table permissions, or refresh_selector.")


async def save_state(context, path: Path) -> None:
    """Replace the saved browser state atomically after a complete JSON write."""
    state = await context.storage_state(indexed_db=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            name = handle.name
            json.dump(state, handle)
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


async def response_payload(response) -> dict[str, Any]:
    """Validate refresh status JSON without exposing the upstream response content."""
    if response.status in (401, 403):
        raise RefreshError("AUTHENTICATION_REQUIRED", "Quickbase rejected the browser session or its refresh permissions.")
    if not 200 <= response.status < 300:
        raise RefreshError("REFRESH_FAILED", f"Quickbase returned HTTP {response.status}. Check the table refresh history.")
    try:
        payload = await response.json()
    except Exception:
        raise RefreshError("UNEXPECTED_RESPONSE", "Quickbase returned a non-JSON response. The browser session can require sign-in.") from None
    if not isinstance(payload, dict):
        raise RefreshError("UNEXPECTED_RESPONSE", "Quickbase returned an unexpected refresh status format.")
    if payload.get("errorCode") not in (None, 0, "0"):
        raise safe_failure(payload.get("errorMessage"))
    if not any(key in payload for key in (*_TASK_FIELDS, "lastError")):
        raise RefreshError("UNEXPECTED_RESPONSE", "Quickbase returned an unexpected refresh status format.")
    for key in _TASK_FIELDS:
        task = payload.get(key)
        if task is not None and not isinstance(task, dict):
            raise RefreshError("UNEXPECTED_RESPONSE", "Quickbase returned an unexpected refresh status format.")
    return payload


async def poll_status(
    context, settings: RefreshSettings, replication_id: str, timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """Read one refresh status response through the authenticated browser context."""
    response = await context.request.get(
        settings.realm_url + "/qb/sync/refreshTasks/" + replication_id,
        params={"getInitialStatus": "false", "includeSkipped": "false"},
        timeout=(timeout_seconds or settings.start_timeout_seconds) * 1000,
    )
    try:
        return await response_payload(response)
    finally:
        await response.dispose()


async def find_replication_id(page, target: TableTarget, observed: set[str]) -> str | None:
    """Use the configured ID, observed requests, or an unambiguous markup value."""
    if target.replication_id:
        if observed and target.replication_id not in observed:
            raise RefreshError("CONFIG_ERROR", "The configured replication_id does not match the connected table's page.")
        return target.replication_id
    # Quickbase normally reads the status when it opens the connected table.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if len(observed) == 1:
            return next(iter(observed))
        if len(observed) > 1:
            break
        await asyncio.sleep(0.2)
    # Some Quickbase views embed the identifier in their initial page markup.
    markup = await page.content()
    ids = set(re.findall(r'''(?:localReplicationId|replicationId|replicationID)["']?\s*[:=]\s*["']?(\d+)''', markup))
    if len(ids) == 1:
        return next(iter(ids))
    if len(observed) > 1 or len(ids) > 1:
        raise RefreshError("REPLICATION_ID_REQUIRED", "Set this table's replication_id in the configuration. The page exposed more than one identifier.")
    return None


async def _run_browser(
    settings: RefreshSettings, table_name: str, operation: str = "refresh",
) -> RefreshResult | dict[str, Any]:
    """Run a browser operation while the saved session remains exclusively locked."""
    if table_name not in settings.tables:
        raise RefreshError("TABLE_NOT_CONFIGURED", "This table name is not in the server configuration.")
    target = settings.tables[table_name]
    try:
        from playwright.async_api import async_playwright, TimeoutError as BrowserTimeout, Error as BrowserError
    except ImportError:
        raise RefreshError("DEPENDENCY_MISSING", "Install the project requirements and the Playwright Chromium browser.") from None
    with session_lock(settings.state_file):
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True, channel=settings.browser_channel)
                try:
                    context_options = {}
                    if settings.state_file.exists():
                        try:
                            context_options["storage_state"] = json.loads(settings.state_file.read_text(encoding="utf-8"))
                        except (OSError, ValueError):
                            raise RefreshError("CONFIG_ERROR", "The saved browser session file is invalid. Remove it and sign in again.") from None
                    context = await browser.new_context(**context_options)
                    context.set_default_timeout(settings.navigation_timeout_seconds * 1000)
                    page = await context.new_page()
                    observed: set[str] = set()

                    def observe_request(request):
                        identifier = refresh_request_id(request.url, settings.realm_url, request.method)
                        if identifier and identifier.isdigit():
                            observed.add(identifier)

                    page.on("request", observe_request)
                    button = await ensure_ready(page, settings, target)
                    await save_state(context, settings.state_file)
                    if operation == "login":
                        return {"status": "authenticated", "table": table_name}
                    replication_id = await find_replication_id(page, target, observed)
                    baseline = await poll_status(context, settings, replication_id) if replication_id else {}
                    if baseline.get("currentRefresh"):
                        raise RefreshError("BUSY", "This connected table already has a refresh in progress. No refresh was started.")
                    if operation == "inspect":
                        return {"status": "ready", "table": table_name, "replication_id": replication_id}
                    started_at = datetime.now(timezone.utc).isoformat()
                    started_clock = time.monotonic()

                    def is_refresh_response(response):
                        found_id = refresh_request_id(response.url, settings.realm_url, response.request.method)
                        return (response.request.method == "POST" and found_id is not None
                                and found_id.isdigit() and (replication_id is None or found_id == replication_id))

                    try:
                        async with page.expect_response(is_refresh_response, timeout=settings.start_timeout_seconds * 1000) as pending:
                            await button.click(timeout=settings.start_timeout_seconds * 1000)
                        response = await pending.value
                    except BrowserTimeout:
                        raise RefreshError("START_UNCONFIRMED", "No matching refresh response arrived after one button click. Check table history before trying again.") from None
                    found_id = refresh_request_id(response.url, settings.realm_url, "POST")
                    if replication_id is None:
                        replication_id = found_id
                    payload = await response_payload(response)
                    # With no pre-click status, only a current task from this POST
                    # is usable. A lastRefresh value can be a previous success.
                    if not baseline:
                        current = payload.get("currentRefresh")
                        if not current:
                            if payload.get("lastError"):
                                raise safe_failure(payload["lastError"])
                            raise RefreshError("START_UNCONFIRMED", "Quickbase returned no current task and no baseline was available. Set replication_id to enable complete correlation.")
                        if current.get("startTime"):
                            try:
                                task_start = datetime.fromisoformat(str(current["startTime"]).replace("Z", "+00:00"))
                                click_time = datetime.fromisoformat(started_at)
                                if task_start.tzinfo and (click_time - task_start).total_seconds() > 10:
                                    raise RefreshError("BUSY", "Quickbase returned a refresh that started before this request. No new start was confirmed.")
                            except ValueError:
                                raise RefreshError("START_UNCONFIRMED", "Quickbase returned an unknown task timestamp. Set replication_id for baseline correlation.") from None
                    tracker = RefreshTracker(replication_id, known_tasks(baseline), baseline_error=baseline.get("lastError"))
                    completed = tracker.observe(payload)
                    deadline = time.monotonic() + settings.refresh_timeout_seconds
                    start_deadline = started_clock + settings.start_timeout_seconds
                    while completed is None:
                        # Before task discovery, both timeout limits apply.
                        active_deadline = deadline if tracker.expected else min(deadline, start_deadline)
                        remaining = active_deadline - time.monotonic()
                        if remaining <= 0:
                            if not tracker.expected:
                                raise RefreshError("START_UNCONFIRMED", "Quickbase did not identify a new refresh. Check table history before trying again.")
                            raise RefreshError("REFRESH_TIMEOUT", "The refresh did not finish before the timeout. It can still be running. Check table history before retrying.")
                        await asyncio.sleep(min(settings.poll_interval_seconds, remaining))
                        remaining = active_deadline - time.monotonic()
                        if remaining <= 0:
                            continue
                        try:
                            completed = tracker.observe(await poll_status(
                                context, settings, replication_id,
                                min(settings.start_timeout_seconds, remaining)))
                        except BrowserTimeout:
                            raise RefreshError("REFRESH_TIMEOUT", "The refresh status request timed out. Check table history before trying again.") from None
                    await save_state(context, settings.state_file)
                    return RefreshResult(
                        table=table_name, replication_id=replication_id,
                        refresh_id=str(completed.get("id") or completed.get("taskId") or tracker.expected),
                        started_at=started_at, finished_at=datetime.now(timezone.utc).isoformat(),
                        duration_seconds=round(time.monotonic() - started_clock, 3),
                    )
                finally:
                    await browser.close()
        except RefreshError:
            raise
        except BrowserTimeout:
            raise RefreshError("BROWSER_TIMEOUT", "The browser timed out before the operation completed. Check table history before retrying.") from None
        except BrowserError:
            # Playwright errors can contain full URLs with CAPTCHA tokens.
            raise RefreshError("BROWSER_ERROR", "The browser operation failed. Check browser installation, network access, and the configured page selectors.") from None
        except (OSError, ValueError):
            raise RefreshError("CONFIG_ERROR", "Cannot read or write the browser session. Check the session path and file permissions.") from None


async def run_browser(
    settings: RefreshSettings, table_name: str, operation: str = "refresh",
) -> RefreshResult | dict[str, Any]:
    """Run login, inspection, or refresh with safe session-path errors."""
    try:
        return await _run_browser(settings, table_name, operation)
    except OSError:
        raise RefreshError("CONFIG_ERROR", "Cannot read or write the browser session. Check the session path and file permissions.") from None


async def refresh_table(settings: RefreshSettings, table_name: str) -> RefreshResult:
    """Click once and return only after the matching refresh completes."""
    return await run_browser(settings, table_name, "refresh")


def main(argv: list[str] | None = None) -> int:
    """Run one CLI operation and print a JSON result or controlled error."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("login", "inspect", "refresh"),
        help="Save a login, inspect readiness, or start one refresh and wait.",
    )
    parser.add_argument(
        "--config", default="refresh_config.json",
        help="JSON configuration path (default: refresh_config.json).",
    )
    parser.add_argument(
        "--table", default="target",
        help="Configured table alias (default: target).",
    )
    args = parser.parse_args(argv)
    try:
        config_path = Path(args.config).resolve()
        load_environment(config_path.parent / ".env")
        settings = RefreshSettings.from_file(config_path)
        result = asyncio.run(run_browser(settings, args.table, args.command))
        print(json.dumps(result.to_dict() if isinstance(result, RefreshResult) else result))
        return 0
    except RefreshError as error:
        print(json.dumps({"status": "error", "error": {"code": error.code, "message": error.message}}))
        return 1
    except KeyboardInterrupt:
        print(json.dumps({"status": "error", "error": {"code": "INTERRUPTED", "message": "Operation interrupted. Check table history before trying again."}}))
        return 130


if __name__ == "__main__":
    sys.exit(main())
