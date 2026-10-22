# Quickbase Table Refresh

A Python tool that refreshes connected Quickbase tables through the normal browser interface.
It provides a command line interface and an optional HTTP API.
Both use headless Playwright Chromium and the same refresh implementation.

The tool clicks **Refresh Data** once, then waits for the matching refresh task to succeed.
With an ID before the click, it compares task identities against the earlier status.
Without that baseline, it requires a current task from the click response. An earlier success cannot complete the new request.
Quickbase handles its normal CAPTCHA evaluation. The tool does not bypass account challenges or retry an uncertain start.

This project depends on an unsupported Quickbase UI workflow and internal status responses.
Changes to Quickbase can require code or selector updates.
Local tests demonstrate the refresh logic. They do not establish compatibility with every Quickbase account.

## Project guide

| File | Purpose |
| --- | --- |
| [`qb_refresh.py`](qb_refresh.py) | Configuration, browser login, task correlation, polling, and CLI |
| [`api_server.py`](api_server.py) | FastAPI service with bearer authentication |
| [`refresh_config.example.json`](refresh_config.example.json) | Generic configuration for a new installation |
| [`docs/architecture.md`](docs/architecture.md) | Control flow, design choices, and limitations |
| [`docs/reference.md`](docs/reference.md) | Configuration, results, API contract, and recovery |
| [`tests/`](tests/) | Unit tests, API tests, and browser tests with a local fixture |
| [`scripts/create_share_bundle.py`](scripts/create_share_bundle.py) | Archive builder with an explicit list of shared files |

## Install for a local review

Use Python 3.11 or later. Local validation uses Python 3.13.7 on Windows 11.
Start PowerShell in the project folder.

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $PWD 'work\.browsers'
.\.venv\Scripts\python.exe -m playwright install chromium
```

`requirements-dev.txt` includes the CLI, API, and test dependencies.
For a CLI installation, use `requirements.txt`.
For an API installation, use `requirements-api.txt`.
`requirements-tested.txt` records the full development environment. It is a version snapshot, not a cross-platform lock file.

## Run the local tests

```powershell
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $PWD 'work\.browsers'
.\.venv\Scripts\python.exe -m pytest -q
```

The tests require no Quickbase credentials. Browser tests use a local HTTP server and synthetic account data.
They cover login, saved session reuse, task correlation, iframe controls, and CAPTCHA rejection after one click.
The API tests replace the refresh function and exercise authentication and error responses.

If Playwright or Chromium is absent, browser tests skip. If their optional dependencies are absent, API tests skip.
A complete test run requires `requirements-dev.txt` and Chromium. Review the passed and skipped counts.

To inspect the browser workflow alone:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_browser.py -q
```

## Configure a Quickbase account

For a new installation, create the local files from the examples:

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
if (-not (Test-Path refresh_config.json)) { Copy-Item refresh_config.example.json refresh_config.json }
```

1. In `refresh_config.json`, replace the example realm and table URL with your target.
2. Set `replication_id` to the connected table's ID, or leave it `null` for discovery.
3. In `.env`, enter `QB_USERNAME` and `QB_PASSWORD`.
4. Put passwords with spaces or `#` inside quotes.

Existing environment variables take precedence over `.env`.
The CLI loads `.env` beside the selected configuration file.
A Quickbase user token cannot authenticate this browser login.
MFA, SSO, and external sign-in flows can require an existing valid browser session.
The tool does not provide an interactive session-import command.

The [configuration reference](docs/reference.md#configuration) lists every field and default.

## Use the CLI

Sign in and save the session without a refresh:

```powershell
.\.venv\Scripts\python.exe qb_refresh.py login --table target
```

Inspect the control and current status without a refresh:

```powershell
.\.venv\Scripts\python.exe qb_refresh.py inspect --table target
```

Start one refresh and wait for its result:

```powershell
.\.venv\Scripts\python.exe qb_refresh.py refresh --table target
```

The CLI writes a JSON result to standard output.
A successful operation returns exit code `0`. A controlled error returns `1`. A keyboard interrupt returns `130`.
`--config PATH` selects another configuration file. `--table NAME` selects a configured alias.

The saved session defaults to `.state/browser.json`.
A file lock prevents concurrent operations that share this state file.
The lock releases at process exit. Its `.lock` file can remain on disk.

## Start the optional API

1. Set a separate random `QB_REFRESH_API_KEY` in `.env` or the server environment.
2. Start one API worker:

```powershell
.\.venv\Scripts\python.exe -m uvicorn api_server:app --host 127.0.0.1 --port 8765 --workers 1
```

`GET /health` reports process health. It does not authenticate with Quickbase.
`POST /refresh/{table_name}` requires the API key and waits for the refresh result.
The server accepts configured aliases. Callers cannot supply target URLs or browser configuration.

From a shell that contains the same API key:

```powershell
$headers = @{ Authorization = "Bearer $env:QB_REFRESH_API_KEY" }
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8765/refresh/target' -Headers $headers
```

The server loads `.env` for its own process. It does not populate the calling shell's environment.
The [API and operations reference](docs/reference.md#http-api) describes responses, deployment, timeouts, and retries.

## Share the project

Create the review archive:

```powershell
.\.venv\Scripts\python.exe scripts/create_share_bundle.py
```

The archive is `dist/quickbase-table-refresh.zip`.
It contains source code, documentation, tests, dependency lists, and generic examples.
The builder includes only the files named in `SHARE_FILES`.
It excludes `.env`, `.state`, `refresh_config.json`, virtual environments, browser downloads, and local artifacts.

The local configuration and session remain available in this workspace.
The ignore rules also exclude them from future Git commits.

## Recover from an uncertain refresh

If the result is `START_UNCONFIRMED` or `REFRESH_TIMEOUT`, inspect the table history before another refresh.
Use the same procedure after a client disconnect, browser error, or interrupted request.
A Quickbase job can continue after the tool stops. The tool does not click again automatically.

The [error reference](docs/reference.md#errors-and-recovery) maps each controlled error to its meaning and next action.
