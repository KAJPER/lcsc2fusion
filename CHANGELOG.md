# Changelog

All notable changes to LCSC2Fusion are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). The section of a version is copied into the
notes of its GitHub release by `scripts/release_notes.py`, so keep every entry understandable on its own.

## [Unreleased]

### Added

- Update notice. When the window opens, the add-in asks GitHub whether a newer stable release exists: one
  HTTPS request to `api.github.com` for the latest release of `KAJPER/lcsc2fusion`, at most once a day and not
  again for 6 hours after a failed attempt. If there is a newer one, a banner above the search field says
  "Version X is available (you have Y)." with a "Download installer" link (the installer file of that release),
  a "What is new" link (the release page) and a "Dismiss" button that hides the notice until a still newer
  version appears. The banner also lists the steps: run the installer, then in Fusion press Shift+S, open
  Add-Ins, select LCSC2Fusion, press Stop, then Run.
- Settings, Updates: a checkbox "Check for updates once a day" (off means no automatic request at all) and a
  "Check now" button that asks at once and says whether you have the latest version, a newer one is available
  or the check failed. The group also shows when the last check was. The choice and the last result are kept in
  `%LOCALAPPDATA%\LCSC2Fusion\update.json`.
- Privacy of the check: the request carries only the version of the add-in in the User-Agent header, with no
  account, token, cookie or machine id. GitHub sees your IP address, like any web server you contact. The
  release text is never shown. Offline, or behind a proxy that blocks `api.github.com`, the automatic check
  silently does nothing; the system proxy settings are used.
- The add-in never updates itself: nothing is downloaded or run automatically. You run the installer, which
  upgrades in place and keeps your data, and then Stop and Run the add-in in Fusion.
- English and Polish texts for the update notice and the new settings.
- Release files can now be code signed by the maintainer on his own PC (`scripts/build_release.py --sign`, `scripts/sign_release.py verify` and `upload`), so that Windows can show the publisher name instead of "Unknown publisher". Releases stay unsigned until a certificate is in use.

### Security

- The answer from GitHub is treated as untrusted. Only the tag, the draft and pre-release flags and the names
  of the attached files are read. The links are built by the add-in from a validated version number (the
  installer link only when the release lists that exact installer file) and the page checks them again against
  exact prefixes before they become `href` attributes. The response is capped at 256 KiB, certificates are
  verified, redirects stay on the same host and an older or equal version is never offered.

## [0.1.0] - 2026-10-14

First public release. Confirmed by hand in Fusion on Windows: generated libraries import cleanly, the
accumulating library in the Hub works, and the 3D package editor opens with the model in place. Not yet
confirmed by hand in Fusion: the restyled window with settings and LCSC links, the MCP server and the local
API (checklist gate 6 in `docs/MANUAL_TEST.md`). macOS is untested.

### Added

- Converter core in plain Python, standard library only. It reads a part from the unofficial EasyEDA/LCSC
  endpoints (disk cache for 7 days, rate limiting, retry, part search), parses the symbol and the footprint
  and writes an Eagle `.lbr` library, with a linter that checks the result against what Fusion accepts.
- Command line: `python -m lcsc2fusion C2040 -o out/` converts one or several parts, accepts a search phrase
  (`--search`, `--pick`), draws PNG previews of the footprint and the symbol (`--render`) and downloads the
  3D model (`--3d`).
- Optional 3D model: the STEP file is checked, moved into the package frame (millimetres, Z up, origin at the
  footprint origin) and written as `<id>.placed.step` with a `<id>.3d.json` record of placement and checksums.
- Fusion add-in with its own window (Edge or Chrome in app mode with a private profile), served by a small
  HTTP server on `127.0.0.1`. It stays open when Fusion changes environment. The button is in Utilities, ADD-INS
  of the Design, Schematic, Board and Library environments.
- One accumulating "LCSC2Fusion Library" in the active Fusion Hub project: the first part creates it, later
  parts are merged into it, the same part is not merged twice and a package with an existing name but different
  geometry is renamed first.
- 3D package flow: with the 3D option on, Fusion opens its 3D package editor with the model already in place,
  and the window asks you to check it and press Finish, Save, OK.
- Search results show Basic or Extended, stock, a link to the LCSC product page and, for https links on
  lcsc.com, jlcpcb.com and easyeda.com only, a link to the datasheet.
- Settings in the window: theme (System, Light, Dark) and language (English, Polski), remembered in the window's
  own browser profile and applied without losing results.
- MCP server (`python -m lcsc2fusion.mcp`, standard library only, stdio) with the tools `lcsc_search`,
  `lcsc_part_info`, `lcsc_convert`, `fusion_status`, `fusion_add_part` and `fusion_job`, so that another
  Claude can search, convert and add parts. Project registration in `.mcp.json`, documentation in
  `docs/MCP.md`.
- Local API of the add-in for the MCP server under `/api/local/` with a random bearer token per start,
  announced to the MCP server through `%LOCALAPPDATA%\LCSC2Fusion\api.json`.
- Windows installer for people who do not use Python: a single file `LCSC2Fusion-Setup-<version>.exe` and an
  installer zip with readable scripts. Both install per user into `%APPDATA%\Autodesk\FusionAddins` without
  administrator rights, and `Uninstall-LCSC2Fusion.bat` removes the add-in. A plain add-in zip is published
  for a manual install. The files are built by `scripts/build_release.py`, checked with `SHA256SUMS.txt` and
  are not code signed.
- GitHub Actions: a CI workflow (ruff, mypy, pytest on Linux and Windows) and a release workflow that builds
  and publishes the files when a `v*` tag is pushed. The steps are in `RELEASING.md`.
- README in English and Polish with a user guide, troubleshooting and screenshots.

### Security

- The add-in server listens on `127.0.0.1` only. Every request of the window checks Host, a session cookie
  and Origin, POST requests need JSON and a custom header, there is no CORS and the content security policy
  is `default-src 'self'`.
- The API for the MCP server needs a bearer token (256 bit, new at every start), rejects any request that
  carries an Origin header and is never reachable from the window's session.
- The name of a written `.lbr` file is never taken from data in the downloaded document, so a crafted
  response cannot place a file outside the output folder. A library name must be a plain file name.
- Links from the network are checked again in the page (exact prefix, https only, known hosts) before they
  become `href` attributes, and they open with `rel="noopener noreferrer"`.

[Unreleased]: https://github.com/KAJPER/lcsc2fusion/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/KAJPER/lcsc2fusion/releases/tag/v0.1.0
