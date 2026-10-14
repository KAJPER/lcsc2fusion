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
section of `CHANGELOG.md` plus a fixed footer), the checksum check by `scripts/verify_checksums.py`. The optional
code signing (done by the owner on his own PC, not by CI) uses `scripts/sign_release.py`, see "Code signing"
below. The workflows are `.github/workflows/ci.yml` (every push to main and every pull request) and
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
11. **Optional: sign.** The owner replaces the four files of the release with signed ones, built on his PC from
    the tag. See "Code signing" below. The release is complete and usable without this step.

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

## Code signing

Out of the box the release is unsigned, so Windows SmartScreen shows "Unknown publisher" for the setup exe.
This section is how the owner signs the files with Authenticode so that Windows shows a publisher name instead.
Signing is optional and local: the workflows stay as they are (they build, test and publish the unsigned
release), and afterwards the owner replaces the four assets of the published release with the signed ones,
built on his own PC.

### Why local

A new public trust code signing certificate has to keep its private key on a hardware token or in a cloud
service, so there is normally no PFX file to download. A cloud certificate such as Certum's (SimplySign) is
unlocked with a one time code from a phone app, which cannot run unattended in GitHub Actions. That is why
signing happens on the owner's PC, with the certificate taken from the Windows certificate store while the
vendor's app is logged in. An ordinary software certificate in a PFX file works too (see the options).

What gets signed:

- `LCSC2Fusion-Setup-<version>.exe`: signed. This is the file where Windows shows the publisher.
- `install.ps1` and `uninstall.ps1` inside the installer zip: signed before they are zipped, so that
  `Get-AuthenticodeSignature` can check them after the zip is extracted.
- The `.bat` and `.cmd` files, `README.txt`, `payload.sha256`, `SHA256SUMS.txt`: not signed, Authenticode does not
  apply to them.
- Both zip files as such: not signed, a zip cannot carry a signature. `SHA256SUMS.txt` covers them.

### Which certificate to buy

The name Windows displays is the subject of the certificate. To see "KAJPA Kacper Popko" the certificate has to be
issued to a registered business with exactly that name (for example a sole proprietorship in CEIDG). A certificate
issued to a private person shows the person's name. The brand alone cannot be shown.

Facts below were gathered on 2026-10-14. They are notes, not promises: prices, stock and rules change, so check
the shop and the vendors before you buy. Sources: https://shop.certum.eu/code-signing.html ,
https://shop.certum.eu/open-source-code-signing-on-simplysign.html ,
https://learn.microsoft.com/azure/trusted-signing/quickstart , https://signpath.org/ .

| Option | Name Windows shows | Price on 2026-10-14 | Notes |
| --- | --- | --- | --- |
| Certum Open Source Code Signing in the Cloud | "Open Source Developer" plus the person's name | from EUR 49 | For open source projects. Out of stock when checked. Limit of 5000 signatures per month. Key in the cloud (SimplySign). |
| Certum Standard Code Signing in the Cloud | The name of the registered business | from EUR 209 | Needs a registered business to show a business name. Key in the cloud (SimplySign). |
| Certum EV Code Signing in the Cloud | The name of the registered business | from EUR 379 | Stricter vetting, more expensive, not needed to show a name. |
| Certum card sets (physical token) | As the cloud variants | Open Source EUR 69, Standard EUR 169, EV EUR 359 | Cheaper, but needs the card reader and its driver. |
| SignPath Foundation | "SignPath Foundation", not the owner | Free for open source | Needs a published code signing policy page and automated CI builds, a third party action and a token in CI. |
| Azure Trusted Signing (Artifact Signing) | The verified organization | Not checked | Public trust only for organizations in the USA, Canada, the EU and the UK, and for individuals only in the USA and Canada. A Polish individual cannot use it, a Polish registered business may. |

Certificates issued after 2026-02-27 are valid for at most 459 days, so plan a renewal about once a year.

Recommendation, in one question: does a registered business called exactly "KAJPA Kacper Popko" exist?

- If yes, buy Certum Standard Code Signing in the Cloud (from EUR 209) for that business. Windows then shows
  "KAJPA Kacper Popko".
- If no, buy Certum Open Source Code Signing in the Cloud (from EUR 49). Windows shows "Open Source Developer"
  and the owner's name: honest, a real identity, and a fraction of the price. If it is out of stock, the Open
  Source card set (EUR 69) or waiting are the options. Do not register a business only for the display name.
- EV is not needed for a name. Do not assume it removes SmartScreen warnings, Microsoft's rules for that change.
- SignPath Foundation is the free route if the owner can live with "SignPath Foundation" as the publisher. It would
  need a new look at the workflows (they use first party actions only and have no secrets).
- Azure Trusted Signing is out for a private person in Poland. It could be an unattended CI option later for a
  registered business.

### One time setup (Certum cloud certificate)

1. Buy the certificate in the Certum shop and pass their identity verification (and for the Open Source product,
   the open source check). Follow their instructions, it can take days.
2. Install SimplySign Desktop on the PC and the SimplySign app on the phone. Log in to Desktop with the one time
   code from the phone. While you are logged in, the certificate appears in the Windows certificate store as a
   virtual smart card.
3. Find the thumbprint (works only while SimplySign is logged in):
   ```
   Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert | Format-List Subject, Thumbprint, NotAfter
   ```
4. Optional: install the Windows SDK signing tools. The script looks for `signtool.exe` on PATH and under
   `Windows Kits\10\bin\*\x64`, and falls back to the PowerShell cmdlet `Set-AuthenticodeSignature` when it is
   not there.

### Routine for every release

1. Push the tag as usual. CI publishes the unsigned release (steps 8 to 10 above).
2. On the owner's PC, build from a clean checkout of the tag (the signature says "this is the release"; the
   build warns when the tree is dirty or HEAD is not the tag):
   ```
   git fetch --tags
   git switch --detach vX.Y.Z
   git status
   ```
3. Log in to SimplySign Desktop, then build, check and upload (replace `<T>` with the thumbprint):
   ```
   python scripts/build_release.py --sign --sign-thumbprint <T> --out dist/release-signed
   python scripts/sign_release.py verify dist/release-signed --expect-thumbprint <T>
   python scripts/sign_release.py upload vX.Y.Z dist/release-signed --dry-run
   python scripts/sign_release.py upload vX.Y.Z dist/release-signed --yes
   ```
4. Open the release page, download the exe and check it as a user would (below).

What the build does with `--sign`: it finds the certificate (and stops when there is none, several, one without the
Code Signing purpose, without a usable private key or outside its validity period), signs copies of
`install.ps1` and `uninstall.ps1`, zips them, builds the exe from that zip, signs the exe, checks every signature
right after it was made (signer, SHA-256, timestamp, status) and writes `SHA256SUMS.txt` last, over the final files.
Any failure fails the build and nothing is written to the output folder. Signing is never done outside the
temporary build folder, and the files in `installer/` are never touched.

What `upload` does: it runs `verify` and `scripts/verify_checksums.py`, requires the release to exist and not be a
draft, requires the tag to match the version in the file names, prints the plan with the exact `gh` command, and
only with `--yes` runs `gh release upload <tag> <files> --clobber`. It never creates or deletes a release or a
tag. All four assets are replaced, also the add-in zip and `SHA256SUMS.txt`, because the zips are not
byte identical to the ones CI built and the checksum list must describe the files on the page. `gh` on the PC must be
logged in to an account that can write to the repository (`gh auth status`), otherwise the upload fails and
nothing changes.

Choosing the certificate: `--sign-thumbprint HEX` (the SHA-1 thumbprint from the store), or `--sign-subject TEXT`
(the whole subject, or exactly its CN or O value, for example `--sign-subject "KAJPA Kacper Popko"`). Zero or
several matching certificates is an error. `--sign-pfx PATH` takes an ordinary software certificate; its password is
read from the environment variable `LCSC2FUSION_SIGN_PFX_PASSWORD` only, loaded in memory and never imported into a
store, and the tool refuses a PFX inside the repository folder. A PFX is always signed through PowerShell because
signtool would need the password on its command line.

Timestamps: the digest is SHA-256 and the timestamp is RFC 3161. The default server is
`http://timestamp.digicert.com` (free, no account, very common, independent of the certificate's CA) and the
fallbacks are `http://timestamp.sectigo.com`, `http://time.certum.pl` and
`http://timestamp.globalsign.com/tsa/r6advanced1`, two rounds each. Give your own with `--timestamp-url URL`
(repeat it for fallbacks). A timestamp keeps the signature valid after the certificate expires. On 2026-10-14, with a
throwaway certificate, signtool got a timestamp from all four default servers. The PowerShell cmdlet (used for a PFX
and as the fallback when signtool is missing) speaks an older timestamp protocol and got one from DigiCert, Sectigo
and Certum but not from the GlobalSign URL, which is why signtool is preferred. `--no-timestamp` is for tests only
and prints a warning: the signature dies with the certificate.

Test options that never belong in a real release: `--no-timestamp`, `--sign-allow-untrusted-root` (accepts a self
signed certificate, whose signature Windows never calls Valid), and for `verify` `--allow-untrusted-root` and
`--allow-no-timestamp`. `--sign-backend signtool` or `powershell` forces one tool.

### How users verify a signed file

In PowerShell, in the folder with the download:

```
Get-AuthenticodeSignature .\LCSC2Fusion-Setup-<version>.exe | Format-List Status, SignerCertificate, TimeStamperCertificate
```

`Status` must be `Valid` and the signer must be the publisher you expect. In Explorer: right click the file, Properties,
Digital Signatures, Details ("This digital signature is OK"). `Get-FileHash` against `SHA256SUMS.txt` still works as
described below. A file changed after signing shows `HashMismatch`.

### When the certificate is renewed

A renewed certificate has a new thumbprint (and maybe a new validity period), nothing in the repository pins a
thumbprint, so use the new one in the commands above. Releases signed with the old certificate stay valid after it
expires because they carry a timestamp. Change the thumbprint in your own notes and in `--expect-thumbprint`.

### What is not solved

- SmartScreen builds its opinion of a file from its reputation. A signature gives the file a name, not a good
  reputation, and building one takes time and downloads. Expect warnings for a while even with a valid signature.
- Antivirus heuristics may still dislike a self extracting exe that starts `cmd` and PowerShell. The zip with plain
  scripts stays the fallback.
- macOS: Authenticode does not exist there. The add-in zip for macOS is not signed or notarized.
- The cloud certificate only works while SimplySign is logged in, and it needs the phone: signing cannot be
  automated in CI. Certum's Open Source product is limited to 5000 signatures per month; one release uses three.
- The certificate selection, signing with signtool, signing with PowerShell and with a PFX, the checks and the
  upload logic are tested with a throwaway self signed certificate and a fake `gh`. A real CA chain (status `Valid`),
  the timestamp servers in the automated tests, and a smart card or cloud certificate were not tested. The first real
  signing needs a look at the output, at `verify` and at the downloaded file.

### Secrets

Never put a PFX file, its password, the OTP seed behind the codes in the phone app, or SimplySign credentials into
the repository, a pull request, a log, a chat, or GitHub secrets. If signing in CI is ever wanted, use a protected
environment with required reviewers and a signing service built for unattended use, never a personal login. The tool
reads the PFX password from the environment only, never puts it on a command line, never prints it and removes it from
anything a child process says. Tests create a self signed certificate in `Cert:\CurrentUser\My` only and delete it
again, and nothing in this repository installs a certificate into a trusted root, trusted publisher or machine store.

## How users verify a download

`SHA256SUMS.txt` lists the SHA-256 hash of each file, one `<hash>  <file name>` line per file. In PowerShell,
in the folder with the download:

```
Get-FileHash .\LCSC2Fusion-Setup-<version>.exe -Algorithm SHA256
```

The hash must equal the line for that file. On Linux or macOS, in the same folder, `sha256sum -c SHA256SUMS.txt`
(macOS: `shasum -a 256 -c SHA256SUMS.txt`) checks every listed file; files you did not download are reported
as missing (GNU `sha256sum` also takes `--ignore-missing`).
