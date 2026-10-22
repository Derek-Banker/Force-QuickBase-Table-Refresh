# Configuration and operations reference

## Configuration

The CLI defaults to `refresh_config.json` in the working directory.
The API selects `QB_REFRESH_CONFIG` from its process environment, or defaults to the same file.
The API selects this path before it loads `.env`.

The configuration uses JSON. The loader accepts UTF-8 with or without a byte order mark.
Unknown fields have no effect.

### Global fields

| Field | Default | Meaning |
| --- | --- | --- |
| `realm_url` | Required | HTTPS origin on a `.quickbase.com` host, without credentials, path, query, or fragment |
| `tables` | Required | Nonempty mapping of aliases to target objects |
| `state_file` | `.state/browser.json` | Playwright session file, relative to the configuration directory or absolute |
| `browser_channel` | `chromium` | `chromium`, `chrome`, or `msedge`. Every channel runs headlessly. |
| `navigation_timeout_seconds` | `60` | Navigation and page-control timeout, also the login readiness window |
| `start_timeout_seconds` | `60` | Click/response timeout, task identification window, and maximum status-request timeout |
| `refresh_timeout_seconds` | `300` | Completion polling window after the click response |
| `poll_interval_seconds` | `5` | Delay between status requests |
| `username_selector` | See the code block | CSS selector for a username field |
| `password_selector` | `input[type='password']` | CSS selector for a password field |
| `submit_selector` | `button[type='submit'], input[type='submit']` | CSS selector for the login submit control |

The default `username_selector` is:

```text
input[name='loginid'], input[name='login'], input[name='username'], input[type='email'], #login
```

Timeouts accept numbers greater than zero and no greater than `3600`. Booleans are invalid.
`poll_interval_seconds` must also be at least `1`.
Login selectors must be nonempty strings. Selector syntax remains subject to Playwright validation.

The timeouts apply to separate phases. They do not form a single deadline for the entire command.
The total duration can exceed the completion timeout because login, discovery, baseline requests, and the click occur first.

### Table fields

Aliases contain only letters, digits, underscores, and hyphens.
Each alias has these fields:

| Field | Default | Meaning |
| --- | --- | --- |
| `url` | Required | Table home or report URL with the same HTTPS scheme and authority as `realm_url` |
| `replication_id` | `null` | Connected-table replication ID as digits. This ID differs from the app ID and table ID. |
| `refresh_selector` | `null` | Optional nonempty CSS selector for the refresh control |

Target URLs cannot contain credentials or fragments.
The loader rejects nonempty query parameters named `token`, `usertoken`, `ticket`, or `password`, regardless of case.
The loader does not verify that a URL identifies a connected table. The browser performs that verification through the page controls.

With `replication_id: null`, the browser discovers the ID from status requests or page markup.
As a final fallback, it reads the ID from the button's normal POST response.
Multiple discovered IDs require an explicit `replication_id`.
An explicit ID supports a baseline request before the click and improves task correlation.

### Environment variables

| Variable | Purpose |
| --- | --- |
| `QB_USERNAME` | Username for the normal password login |
| `QB_PASSWORD` | Password for the normal password login |
| `QB_REFRESH_API_KEY` | Required bearer secret for API startup |
| `QB_REFRESH_CONFIG` | API configuration path, read from the process environment before `.env` |
| `PLAYWRIGHT_BROWSERS_PATH` | Browser installation directory used by Playwright |

The CLI and API load `.env` beside the selected configuration file.
Existing process variables take precedence.
Relative `PLAYWRIGHT_BROWSERS_PATH` values depend on the working directory.
If the saved session remains valid, the username and password are unnecessary.

## CLI results

| Command | Successful result |
| --- | --- |
| `login` | `{"status": "authenticated", "table": "target"}` |
| `inspect` | `{"status": "ready", "table": "target", "replication_id": "100001"}` |
| `refresh` | The completed result shown next |

`login` verifies that the refresh control is visible and saves the session. It does not inspect refresh status.
`inspect` also attempts ID discovery and a baseline status request.
It returns `BUSY` for a current refresh. If discovery requires a click, its `replication_id` can be `null`.
Neither command clicks **Refresh Data**.

Example refresh result with synthetic values:

```json
{
  "table": "target",
  "replication_id": "100001",
  "refresh_id": "101",
  "started_at": "2026-01-01T12:00:00+00:00",
  "finished_at": "2026-01-01T12:00:17+00:00",
  "duration_seconds": 17.0,
  "message": "The connected table refreshed successfully.",
  "status": "completed"
}
```

The timestamps use UTC. The duration starts immediately before the click and includes the wait for completion.
It excludes browser startup, login, and the baseline request.
The `refresh_id` normally contains the task ID, with the tracker's identity as a fallback.

A controlled CLI error uses this envelope:

```json
{
  "status": "error",
  "error": {
    "code": "CAPTCHA_FAILED",
    "message": "Quickbase rejected the browser CAPTCHA check. The script did not retry the refresh."
  }
}
```

Successful commands return exit code `0`. Controlled errors return `1`. Keyboard interrupts return `130`.
Argument errors use argparse output and exit code `2`.

## HTTP API

The API loads its configuration and bearer key once at startup.
A missing or blank key prevents startup. Configuration changes require a restart.
Interactive API documentation and the OpenAPI endpoint are disabled.

| Route | Authentication | Response |
| --- | --- | --- |
| `GET /health` | None | `200`, `{"status": "ok"}` |
| `POST /refresh/{table_name}` | `Authorization: Bearer <key>` | `200` with the completed refresh result |

The refresh endpoint has no request body. It waits until completion or an error.
Authentication occurs before table selection. An authorized request for an unknown alias returns `404` with code `UNKNOWN_TABLE`.
An invalid or absent bearer key returns `401` with code `UNAUTHORIZED` and a `WWW-Authenticate: Bearer` header.

API errors use FastAPI's `detail` envelope:

```json
{
  "detail": {
    "code": "BUSY",
    "message": "Another process is using this browser session. Try again after it finishes."
  }
}
```

`/health` reports that startup succeeded. It does not verify browser installation, Quickbase access, or refresh permissions.

## Errors and recovery

The HTTP column describes errors from the refresh endpoint after successful API startup.
Configuration and dependency errors can instead prevent startup.

| Code | HTTP | Meaning and next action |
| --- | --- | --- |
| `BUSY` | `409` | The session lock is held, an existing refresh is active, or the response identifies an older task. Inspect current activity. |
| `AUTHENTICATION_REQUIRED` | `401` | Quickbase login or permissions failed. Verify credentials, session validity, and the sign-in policy. |
| `CAPTCHA_FAILED` | `422` | Quickbase rejected the normal browser check. Inspect table history and account access. |
| `REFRESH_TIMEOUT` | `504` | Completion or a status request timed out. Inspect table history before another refresh. |
| `CONFIG_ERROR` | `500` | Configuration or session storage is invalid or inaccessible. Verify paths, values, and permissions. |
| `REFRESH_FAILED` | `502` | Quickbase reported an error or returned an unsuccessful HTTP status. Inspect table history. |
| `START_UNCONFIRMED` | `502` | The tool cannot correlate a new task after one click. Inspect table history before another refresh. |
| `UNEXPECTED_RESPONSE` | `502` | The status format or replication ID is unexpected. Inspect upstream behavior and table history. |
| `REPLICATION_ID_REQUIRED` | `502` | Discovery found multiple IDs. Set the target's `replication_id`. |
| `REFRESH_CONTROL_NOT_FOUND` | `502` | The control is absent. Verify the URL, permissions, and `refresh_selector`. |
| `BROWSER_TIMEOUT` | `502` | A browser phase timed out. Inspect table history before another refresh. |
| `BROWSER_ERROR` | `502` | A browser operation failed. Verify browser installation, network access, and selectors. Inspect table history before another refresh. |
| `DEPENDENCY_MISSING` | `502` | A Python dependency is absent. Install the selected requirements and Chromium. |
| `TABLE_NOT_CONFIGURED` | N/A | The CLI or Python caller supplied an unknown alias. The API returns `UNKNOWN_TABLE` instead. |
| `INTERRUPTED` | N/A | The CLI received a keyboard interrupt. Inspect table history before another refresh. |

An error after the click does not prove that Quickbase stopped the job.
A disconnected caller also cannot assume cancellation. The project has no automatic retry, cancellation endpoint, or job queue.

## Deployment and local data

1. Install the Python requirements and Playwright Chromium under the service account.
2. Use absolute paths for `QB_REFRESH_CONFIG` and `PLAYWRIGHT_BROWSERS_PATH` in the service environment.
3. Restrict access to `.env`, the local configuration, and `.state` to the service account.
4. Run one API worker on loopback.
5. For remote callers, place the service behind an authenticated HTTPS reverse proxy.
6. Set proxy and client timeouts longer than the expected total operation duration.

The browser requires outbound access to Quickbase and the resources that its login and CAPTCHA pages load.
The bearer key grants access to every configured alias.
The API provides no per-table authorization, rate limit, or persistent audit log.
It does not queue a busy request.

The session file contains cookies and browser storage. These values can grant account access.
The code saves session state through a temporary file and an atomic replacement.
Filesystem permissions depend on the host and service account.
The tool does not save screenshots, HAR captures, or browser traces.

The OS lock applies only to processes that share the same state-file path.
Separate state files, separate hosts, and manual Quickbase activity remain outside this lock.
The lock file can remain after an operation without an active lock.
