# Use lcsc2fusion from another Claude (MCP)

`lcsc2fusion.mcp` is a small MCP (Model Context Protocol) server. It lets Claude Code, Claude
Desktop or any other MCP client search LCSC parts, inspect them, convert them to Eagle `.lbr`
libraries and, through the running Fusion add-in, put them straight into the "LCSC2Fusion
Library" in Fusion. It speaks newline delimited JSON-RPC 2.0 over stdio, needs Python 3.11+ and
has no dependencies beyond the standard library. It reuses the same code as the command line tool
(cache, rate limit, retry), so it never talks to EasyEDA any differently than `python -m lcsc2fusion`.

## Tools

| Tool | What it does | Fusion needed |
| --- | --- | --- |
| `lcsc_search` | Search by LCSC number (`C2040`) or free text. Returns `lcsc_id`, `mpn`, `manufacturer`, `package`, `description`, `library_type`, `stock`, `lcsc_url`, `datasheet_url`. Arguments: `query` (max 100 characters), `limit` (1 to 20, default 10). | no |
| `lcsc_part_info` | Shows what a conversion would produce without writing files: MPN, package, pad and pin counts, whether a 3D model exists, warnings of the source data, links. Argument: `lcsc_id`. | no |
| `lcsc_convert` | Writes `<id>.lbr` (and with `with_3d` also `<id>.step`, `<id>.placed.step`, `<id>.3d.json`) and returns absolute paths, warnings and the attribution note. Arguments: `lcsc_id`, `with_3d` (default false), `out_dir` (default `~/LCSC2Fusion/libraries`). | no |
| `fusion_status` | Is the add-in running, is it busy? Never fails when it is absent and says how to start it. | checks it |
| `fusion_add_part` | Asks the add-in to download a part and merge it into the "LCSC2Fusion Library" in the active Fusion project. Arguments: `lcsc_id`, `with_3d` (default false), `wait` (default true), `timeout_s` (10 to 600, default 240). | yes |
| `fusion_job` | Current state of a job started by `fusion_add_part`. Argument: `job`. | yes |

Results come as text for the model plus `structuredContent` with the same facts as data. Expected
problems (unknown part, network error, add-in not running, add-in busy, a failed job) are tool
results with `isError` true and a message that says what to do next, not protocol errors.
Arguments that break the schema (a bad part number, an out of range limit, unknown argument
names) are JSON-RPC `-32602` errors.

`fusion_add_part` with `wait` true polls the job once a second and, when the client sent a
progress token, reports every new step as a `notifications/progress` message. If `timeout_s`
passes first, the tool returns the running state (not an error); the job keeps running in Fusion
and `fusion_job` shows how it ends.

With `with_3d` Fusion opens a 3D package editor with the model already in place. A person has to
check it and press Finish, then Save, then OK. Fusion can be unresponsive for up to about a minute
while it works, and the add-in processes one part at a time (a second request while one runs
gets "busy").

## Install and register

The repository ships a project scope configuration, `.mcp.json`:

```json
{"mcpServers": {"lcsc2fusion": {"command": "python", "args": ["-m", "lcsc2fusion.mcp"]}}}
```

### Claude Code, project scope

Start `claude` in the repository folder. Claude Code asks once whether to trust the project
server `lcsc2fusion` from `.mcp.json`; approve it. The server runs with the repository as working
directory, so `python -m lcsc2fusion.mcp` finds the package without installing anything. Check
with `/mcp` inside Claude Code. `python` has to be 3.11 or newer and on `PATH`; no packages are
needed.

### Claude Code, user scope (works from any folder)

Use absolute paths and point `PYTHONPATH` at the repository:

```
claude mcp add lcsc2fusion --scope user -e PYTHONPATH=C:\path\to\lcsc2fusion -- C:\path\to\lcsc2fusion\.venv\Scripts\python.exe -m lcsc2fusion.mcp
```

On macOS or Linux use the matching `python` of your virtualenv and a POSIX path. Remove it with
`claude mcp remove lcsc2fusion --scope user`.

### Claude Desktop

Edit `claude_desktop_config.json` (Settings, Developer, Edit Config) and restart the app. On
Windows the backslashes in JSON strings must be doubled:

```json
{
  "mcpServers": {
    "lcsc2fusion": {
      "command": "C:\\path\\to\\lcsc2fusion\\.venv\\Scripts\\python.exe",
      "args": ["-m", "lcsc2fusion.mcp"],
      "env": { "PYTHONPATH": "C:\\path\\to\\lcsc2fusion" }
    }
  }
}
```

### Installed package

After `pip install -e .` in the virtualenv, the console command `lcsc2fusion-mcp` exists (for
example `C:\path\to\lcsc2fusion\.venv\Scripts\lcsc2fusion-mcp.exe`). Use that path as the command
and drop `args` and `PYTHONPATH`. `lcsc2fusion-mcp --version` prints the version; the server
itself is silent on stdout except for protocol messages and logs to stderr only.

## Fusion add-in and `api.json`

`lcsc_search`, `lcsc_part_info` and `lcsc_convert` work without Fusion. The `fusion_*` tools talk
to the LCSC2Fusion add-in, so Fusion has to be running with the add-in started (Utilities,
Add-Ins, Scripts and Add-Ins, Shift+S; the add-in also starts with Fusion when "Run on startup"
is on) and signed in to a Hub project.

While the add-in runs it listens on a random port of `127.0.0.1` and writes the port and a secret
token to `api.json` next to its other state file:

- Windows: `%LOCALAPPDATA%\LCSC2Fusion\api.json`
- macOS: `~/Library/Application Support/LCSC2Fusion/api.json`
- Linux: `~/.local/state/lcsc2fusion/api.json`

The add-in deletes the file when it stops. `LCSC2FUSION_API` (a full file path) overrides the
location, which is mostly useful for tests. The MCP server reads the file for every call, so
restarting the add-in needs no restart of the MCP server. If Fusion crashed and left a stale
file, the tools report "connection refused" and say how to start the add-in again.

The local API (all requests carry `Authorization: Bearer <token>`; POST also `Content-Type:
application/json` and `X-LCSC: 1`; no `Origin` header):

| Request | Answer |
| --- | --- |
| `GET /api/local/ping` | `{"ok": true, "version": 1, "busy": false}` |
| `POST /api/local/add` with `{"lcsc_id": "C2040", "with_3d": false, "fusion": true}` | `202 {"job": 7}`, or 409 busy, 400 bad request, 503 unavailable |
| `GET /api/local/job/7` | job document: `state` (`running`, `done`, `failed`), `status`, `message`, `steps`, `added`, `fusion` |

## Security notes

- The token and the add-in URL are read only from `api.json`, only the exact form
  `http://127.0.0.1:<port>` is accepted (checked again right before connecting), redirects are
  never followed, and the token is never logged, returned in a tool result or put in an error
  message. A 401 or 403 makes the client re-read `api.json` once, then stop with "restart the
  add-in".
- All tool arguments are validated strictly: `lcsc_id` must be `C` and 1 to 12 digits, queries
  are at most 100 characters without control characters, flags must be real booleans, numbers
  must be finite and in range, unknown arguments are refused.
- `out_dir` must be an absolute local path or start with `~`. Empty values, NUL bytes, relative
  paths and network or device paths (`\\host\share`, `\\?\`) are refused. The folder is created
  and a test file is written to prove it is writable. File names inside it are derived from the
  validated part number, never from free text.
- Text that comes from LCSC or EasyEDA (descriptions, part names) is shortened and stripped of
  control characters before it is returned, because a model reads it. Datasheet links are
  returned only when they are plain `http` or `https` URLs.
- Failures return a short message to the model. Stack traces go to stderr (the log of the MCP
  client), never into a tool result.
- The server only talks to EasyEDA, LCSC (the datasheet link lookup) and JLCPCB (free text search)
  through the existing client (disk cache, minimum interval between requests, retry, fail fast after
  an HTTP 403 or 429) and to the local add-in. The hosts and what each one receives are listed in
  the Privacy section of the [README](../README.md).
  It starts no other network connection and runs no shell commands. Requests are handled one at
  a time, with socket timeouts of 20 s for EasyEDA, 10 s for pings and polls and 35 s for the
  add request.
- A project scope `.mcp.json` runs code from the repository on your machine, which is why Claude
  Code asks for approval. Read it before approving.

## Terms of use and attribution

The library data (symbols, footprints, 3D models) comes from the JLCEDA/EasyEDA Official Library
through unofficial endpoints and belongs to JLCEDA/EasyEDA. Use it for yourself, with the same
limits as in the [README](../README.md): their terms forbid bulk or systematic automated access
and the use of the content for machine learning, and commercial use is granted only inside
EasyEDA. Do not point an agent at this server to crawl the catalogue or to convert many parts
unattended. The client rate limits and caches, but you are responsible for what you ask it to do.
Generated libraries and `<id>.3d.json` files carry the required attribution, and `lcsc_convert`
returns it in every result. Do not redistribute the STEP files. Check footprint and pinout
against the datasheet before ordering boards. This is not legal advice.

## Example

```
You: Add a 10k 0603 resistor from LCSC to my Fusion library.

Claude: (lcsc_search, query "10k 0603 resistor", limit 5)
  1. C25804  <mpn>  <manufacturer>  0603 | Basic | stock <n>
  2. ...
  I will take C25804. (lcsc_part_info, lcsc_id C25804)
  C25804: 2 pads, 2 pins, 3D model available. (fusion_status) The add-in is running and ready.
  (fusion_add_part, lcsc_id C25804; progress: Downloading, Building library, Merging)
  C25804 was merged into the Fusion library 'LCSC2Fusion Library'.
  Please check footprint and pinout against the datasheet before using it on a board.

You: Do the same with the 3D model.

Claude: (fusion_add_part, lcsc_id C25804, with_3d true)
  A 3D package editor is open in Fusion with the model in place. Please check it and press
  Finish, then Save, then OK. Fusion may be slow to respond for up to a minute.
```

Without Fusion, ask Claude to "convert C25804 to a library file": it calls `lcsc_convert` and
tells you the path of the `.lbr` it wrote.
