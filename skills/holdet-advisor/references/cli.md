# Source-based CLI

Use the repository's Python environment and source file. No CLI installer or running website is required.

In PowerShell, after locating the repository:

    $holdetRepo = (Resolve-Path .).Path
    $holdetPython = Join-Path $holdetRepo '.venv\Scripts\python.exe'
    $holdetCli = Join-Path $holdetRepo 'cli\main.py'
    & $holdetPython -X utf8 $holdetCli --help
    & $holdetPython -X utf8 $holdetCli data paths --json

Use the actual repository path instead of dot when the chat is elsewhere. Check that the environment exists. If absent, an existing Python 3.14 environment with the project dependencies can run the source; explain missing requirements rather than installing software implicitly.

The path preflight reports paths and existence without creating anything. Windows sandbox redirection is detected when an existing desktop cache is available; the decision is reported on stderr. Explicit paths remain authoritative. For split storage:

    & $holdetPython -X utf8 $holdetCli --data-dir '<local application root>' --config-dir '<roaming configuration directory>' games

The data root contains data/ and exports/. The config directory directly contains groups.json, hub-settings.json, and optional team-strategies.json. Never pass the snapshots directory as the data root or mistake an empty alternate location for an empty user account.

## Cache-only reads

    & $holdetPython -X utf8 $holdetCli games
    & $holdetPython -X utf8 $holdetCli context '<game slug or exact saved name>' --history
    & $holdetPython -X utf8 $holdetCli context '<game>' --team '<team ID>' --analysis
    & $holdetPython -X utf8 $holdetCli context '<game>' --competition '<competition ID>'
    & $holdetPython -X utf8 $holdetCli rules '<game>'
    & $holdetPython -X utf8 $holdetCli strategy '<game>' --team '<team ID>'
    & $holdetPython -X utf8 $holdetCli refresh '<game>'

Context returns the shared complete scope export plus an advisory extension containing rules, evidence identity, and saved policies. It preserves stable entry IDs, performance statistics, popularity, units, numeric values, and source quality. History includes the latest corrected player snapshot per local round. Analysis adds descriptive metrics and provenance. Neither option fetches history.

Default game context uses tracked competition members and the selected own team. Games also lists other cached teams, selectable with repeatable --team. Competition selections cannot include nonmembers. A later fetch of an old round does not replace the highest-round roster or catalogue. Historical summaries do not prove that exact historical lineups were captured.

Read stdout as JSON and stderr as diagnostics. --output explicitly writes the same JSON to a file instead of printing it.

## Explicit data changes

    & $holdetPython -X utf8 $holdetCli refresh '<game>' --execute
    & $holdetPython -X utf8 $holdetCli refresh --all --execute
    & $holdetPython -X utf8 $holdetCli refresh '<game>' --competition '<ID>' --execute
    & $holdetPython -X utf8 $holdetCli refresh --status '<run ID>'
    & $holdetPython -X utf8 $holdetCli refresh --resume '<run ID>'
    & $holdetPython -X utf8 $holdetCli refresh --cancel '<run ID>'

Without --execute, refresh previews pending work using local data. Execution uses the same durable worker, lock, retry journal, validation, and tournament postprocessing as the dashboard. All excludes archived games. Force also retrieves fresh sources. Execution waits and prints one final JSON document, with progress on stderr. Partial/failed results have nonzero exit status; readable previous data is retained. Inspect context quality afterwards.

An unregistered game can be explicitly registered with games --add URL --name NAME before shared refresh. Registration is a configuration write.

Existing players URL --format json and teams URL commands fetch and write snapshots/exports. Player JSON from that command is a filtered table, not the complete canonical context. Prefer context for analysis.

Validate accepts a JSON file or --proposal - for stdin. Exit 0 means valid; exit 1 means invalid, unverified, or a reported operational error. Argparse usage errors use exit 2.
