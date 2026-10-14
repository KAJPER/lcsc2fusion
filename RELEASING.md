# Releasing LCSC2Fusion

For the maintainer. A release is made by pushing a tag `vX.Y.Z`. GitHub Actions then builds the Windows
installer, runs the tests, and publishes a GitHub release with these files:

| File | What it is |
| --- | --- |
| `LCSC2Fusion-Setup-<version>.exe` | Single file installer made with Windows IExpress. Most users take this one. |
| `LCSC2Fusion-<version>-installer.zip` | The same installer as readable scripts (`Install-LCSC2Fusion.bat`, `Uninstall-LCSC2Fusion.bat`, `install.ps1`, `uninstall.ps1`, `README.txt`, the add-in folder, `payload.sha256`). |
| `LCSC2Fusion-<version>-addin.zip` | The plain add-in folder, for a manual install and for macOS (untested). |
| `SHA256SUMS.txt` | SHA-256 of the three files above. |

The files are made by `scripts/build_release.py`, the notes by `scripts/release_notes.py` (the matching
section of `CHANGELOG.md` plus a fixed footer), the checksum check by `scripts/verify_checksums.py`. The
workflows are `.github/workflows/ci.yml` (every push to main and every pull request) and
`.github/workflows/release.yml` (tags and manual dry runs).

## The three places that hold the version

The tag must be `v` plus the version in all of these, and `python scripts/build_release.py --check-tag vX.Y.Z`
checks it:

1. `pyproject.toml`, `version = "X.Y.Z"`
2. `lcsc2fusion/__init__.py`, `__version__ = "X.Y.Z"`
3. `addin/LCSC2Fusion/LCSC2Fusion.manifest`, `"version": "X.Y.Z"`

A pre-release uses a hyphen suffix in all three places and in the tag, for example `0.2.0-rc1` and
`v0.2.0-rc1`. The workflow marks every version with a hyphen suffix as a pre-release on GitHub, so it is not
shown as "Latest". Try `--check-tag` locally first: the string after the `v` must be identical everywhere.

## Making a release

1. **Main is green.** The Actions tab shows a green CI run for the last commit on main. Or run
   `python -m pytest`, `ruff check .`, `ruff format --check .` and `mypy lcsc2fusion` locally.
2. **Fusion check.** Run the relevant gates of `docs/MANUAL_TEST.md` in real Fusion for what changed. If a gate
   that the README status box and the first paragraph of the version's changelog section call "not yet
   confirmed" has passed, update both texts (and `DECISIONS.md`).
3. **Changelog.** In `CHANGELOG.md` move the entries from `## [Unreleased]` into a new
   `## [X.Y.Z] - YYYY-MM-DD` section (write it for users, the section becomes the release text). Update the
   link block at the bottom: add `[X.Y.Z]: https://github.com/KAJPER/lcsc2fusion/releases/tag/vX.Y.Z` and point
   `[Unreleased]` to `compare/vX.Y.Z...HEAD`. Preview the notes:
   `python scripts/release_notes.py vX.Y.Z`
4. **Version bump.** Change the three places above (see the checklist below).
5. **Local check.**
   ```
   python scripts/build_release.py --check-tag vX.Y.Z
   python -m pytest
   python scripts/build_release.py --out dist/release
   python scripts/verify_checksums.py dist/release --version X.Y.Z
   ```
   The build writes to `dist/release`, which git ignores.
6. **Commit and push.** Push to main, or to a branch and merge it once CI is green.
   ```
   git add CHANGELOG.md pyproject.toml lcsc2fusion/__init__.py addin/LCSC2Fusion/LCSC2Fusion.manifest
   git commit -m "chore: release X.Y.Z"
   git push origin main
   ```
7. **Dry run on GitHub** (recommended, see "Dry run" below) on the pushed commit, then test the
   downloaded installer on a clean Windows user.
8. **Tag and push the tag.** Tag the commit that CI and the dry run have already checked.
   ```
   git tag vX.Y.Z
   git push origin vX.Y.Z
   ```
9. **Watch the run** in the Actions tab, workflow "Release". The job "Build and test" resolves the tag,
   runs `--check-tag`, the whole test suite, the build, the checksum check and the notes. The job
   "Publish the GitHub release" then runs `gh release create` and fails, instead of overwriting, when a release
   for the tag already exists.
10. **Check the result.** The release page must show the four files, the notes and (for a hyphen version) the
    pre-release mark. Download the exe from the page, compare its hash with `SHA256SUMS.txt`, install it on a
    clean Windows user and start Fusion (checklist below).

### Version bump checklist

- [ ] `pyproject.toml` version
- [ ] `lcsc2fusion/__init__.py` `__version__`
- [ ] `addin/LCSC2Fusion/LCSC2Fusion.manifest` version
- [ ] `CHANGELOG.md`: new section with the date, link block updated
- [ ] README status box still true (what is confirmed in Fusion)
- [ ] `python scripts/build_release.py --check-tag vX.Y.Z` exits 0
- [ ] Tests, ruff and mypy are green

## Dry run

Actions, "Release", "Run workflow", pick a branch (or a tag). This is the `workflow_dispatch` trigger. A
manual run does everything except publishing:
it reads the version from the package (the tag is `v` plus that version), runs `--check-tag` (so it also proves
that the three version places agree), the full tests, the build, the checksum check and the notes for the
changelog section of that version (so a missing section fails the dry run, not the real one). The files stay
as workflow artifacts for 30 days: `release-files` and `release-notes`. The `publish` job is skipped, also when
the run was started on a tag. Nothing is written to GitHub Releases and no tag is created.

Test the artifact like a user would, on a clean Windows user account (or a VM) with Fusion installed:

- [ ] Unzip the artifact, run the exe: SmartScreen shows "unknown publisher", "More info", "Run anyway" works.
- [ ] `%APPDATA%\Autodesk\FusionAddins\LCSC2Fusion\LCSC2Fusion.manifest` exists afterwards.
- [ ] Start Fusion: the add-in is listed in Add-Ins and runs on startup; the button opens the window.
- [ ] Run the installer again (upgrade over an existing install) and check that nothing breaks.
- [ ] `Uninstall-LCSC2Fusion.bat` from the installer zip removes the folder; `%LOCALAPPDATA%\LCSC2Fusion` stays.
- [ ] The installer zip route works too (unzip, double click `Install-LCSC2Fusion.bat`).

## When something goes wrong

The publish job is the last step and it only creates the release, so a failure in the build job leaves
nothing public.

- **Build or test failure on a tag run.** Nothing was published. Fix the problem on main, then move the tag
  to the fixed commit. A tag that was never released can be moved: delete it locally and on GitHub, then tag
  and push again.
  ```
  git tag -d vX.Y.Z
  git push origin --delete vX.Y.Z
  git tag vX.Y.Z
  git push origin vX.Y.Z
  ```
  Or fix it with the next patch version and leave the failed tag alone.
- **The publish job failed** (network, a rejected upload). Open the failed run and use "Re-run failed jobs": the
  files come from the artifacts of the build job (kept 30 days), so nothing is rebuilt. If `gh release create`
  had already made a half finished release, delete that release first:
  `gh release delete vX.Y.Z --yes` (this keeps the tag; `--cleanup-tag` would delete the tag as well).
- **"A release for vX.Y.Z already exists".** This is on purpose, the workflow never overwrites a release. Look
  at the release. If it is wrong and nobody can have downloaded it, delete it with `gh release delete` and
  re-run the publish job. Otherwise do not reuse the version: publish a fixed patch version.
- **A wrong tag was pushed** (typo, wrong commit). If no release exists yet, delete the tag as shown above (the
  run may already be going: cancel it in the Actions tab first). If a release exists, treat it as published:
  delete the release only when you are sure nobody used it, and prefer a new version.
- **The tag does not match the version** (`--check-tag` fails). Fix the version places or the tag; the tag
  is `v` plus the string in all three places.
- **Published a bad release.** Edit the release on GitHub and tick "Set as a pre-release" or delete it, and
  publish a new patch version with a changelog entry that says what was wrong.

## One time setup of the repository

- Settings, Actions, General: Actions must be allowed. The default workflow permission can stay on "Read
  repository contents", because `release.yml` asks for `contents: write` in the publish job only.
- Recommended: a tag ruleset (Settings, Rules) for `v*` that restricts who can create and delete tags, and
  a branch protection or ruleset for main that requires the CI checks (`ruff`, `mypy` and the four `pytest`
  jobs) before merging. Without a ruleset anyone with write access can publish a release by pushing a tag.
- A tag can point at any commit, including one that is not on main. The workflow does not check that, so
  tag only commits from main.
- The workflows need no secrets: the built in `GITHUB_TOKEN` is enough.

## About the workflows

- Only first party actions (`actions/checkout`, `actions/setup-python`, `actions/upload-artifact`,
  `actions/download-artifact`) and the GitHub CLI of the runner image are used, no third party release action.
  They are pinned to a major version tag (`@v7`, `@v8`). Pinning to a full commit SHA would be stricter, at the
  price of updating the SHAs by hand or with Dependabot (`package-ecosystem: github-actions`).
- The build runs on `windows-latest` because IExpress, which makes the exe, only exists on Windows. The publish
  job runs on `ubuntu-latest`, has no checkout and runs no code from the repository, and it is the only job
  with `contents: write`.
- A hyphen in the tag after the numbers (`v0.2.0-rc1`) adds `--prerelease`. The publish job runs
  `gh release create` with `--verify-tag`, which refuses a tag that GitHub does not have.
- The workflows were written without being able to run them. Expect to fix small things on the first dry run.

## Signing

The installer and its exe are not code signed, so Windows SmartScreen and some antivirus programs warn about
them (the README explains it to users). That is a known gap, not a bug. A possible future step is a free
signature from the SignPath Foundation, which offers code signing to open source projects; that needs an
application, a signing step in the release workflow and a new look at what the workflow may do. Until then
the checksums and the readable scripts in the installer zip are the way to check what you run.

## How users verify a download

`SHA256SUMS.txt` lists the SHA-256 hash of each file, one `<hash>  <file name>` line per file. In PowerShell,
in the folder with the download:

```
Get-FileHash .\LCSC2Fusion-Setup-<version>.exe -Algorithm SHA256
```

The hash must equal the line for that file. On Linux or macOS, in the same folder, `sha256sum -c SHA256SUMS.txt`
(macOS: `shasum -a 256 -c SHA256SUMS.txt`) checks every listed file; files you did not download are reported
as missing (GNU `sha256sum` also takes `--ignore-missing`).
