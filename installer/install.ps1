<#
.SYNOPSIS
    Installs the LCSC2Fusion add-in for Autodesk Fusion (current user, no admin rights, no network).

.DESCRIPTION
    Checks the add-in files next to this script against payload.sha256, then replaces
    <TargetRoot>\LCSC2Fusion with them. The swap is atomic: the files are copied to a staging
    folder, the old folder is renamed aside, the staging folder is renamed into place, and
    everything is rolled back when a step fails. Nothing else in TargetRoot is touched, and
    %LOCALAPPDATA%\LCSC2Fusion (your settings and state) is never touched.

    This file is plain text. Read it before you run it.

.PARAMETER TargetRoot
    Folder that holds Fusion add-ins. Default: %APPDATA%\Autodesk\FusionAddins
    (the environment variable LCSC2FUSION_FUSIONADDINS_DIR replaces the default, for tests).

.PARAMETER SourceDir
    Folder with the LCSC2Fusion folder and payload.sha256. Default: the folder of this script.

.PARAMETER Language
    auto (Windows display language), en or pl.

.PARAMETER Silent
    No prompts, no pauses, no message boxes: only text output and the exit code.

.PARAMETER Gui
    No console interaction: the result is shown in a message box.

.NOTES
    Exit codes: 0 installed, 10 add-in files or manifest missing, 11 checksum list missing or
    a file does not match it or an unlisted file exists, 12 invalid TargetRoot, 13 copy or swap
    failed (the previous version is left in place), 14 unexpected error.
#>
[CmdletBinding()]
param(
    [string]$TargetRoot,
    [string]$SourceDir,
    [string]$Language = 'auto',
    [switch]$Silent,
    [switch]$Gui
)

Set-StrictMode -Version 2
$ErrorActionPreference = 'Stop'

$script:AddinName = 'LCSC2Fusion'
$script:Lang = 'en'

# Texts. Placeholders are {0}, {1}. Keep one entry per line, in both languages, with the same keys.
$script:Messages = @{
    en = @{
        title = 'LCSC2Fusion setup'
        checking = 'Checking the installer files...'
        payload_ok = 'The installer files are complete and unchanged ({0} files, add-in version {1}).'
        target_is = 'Install location: {0}'
        fusion_running = 'Autodesk Fusion is running. Installing works anyway; the new version loads when the add-in is restarted.'
        fusion_not_running = 'Autodesk Fusion is not running.'
        no_autodesk = 'Note: the folder {0} does not exist, so Autodesk Fusion may not be installed for this user. The add-in is installed anyway.'
        copying = 'Copying {0} files...'
        replaced_old = 'The previous version was replaced.'
        verified = 'The installed files were checked against the checksum list.'
        leftover_warning = 'Warning: the previous version could not be deleted completely. It was left in {0}. You can delete that folder.'
        done = 'Done. LCSC2Fusion {0} is installed in {1}'
        data_kept = 'Your settings and downloaded libraries (in {0}) were not touched.'
        next_start = 'Next step: start Autodesk Fusion. The add-in starts by itself.'
        next_running = 'Fusion is already running: press Shift+S, open the Add-Ins tab, select LCSC2Fusion and press Run. If the add-in is already running, press Stop first and then Run, so that the new version loads.'
        failed = 'The installation failed.'
        rolled_back = 'The previous version was restored.'
        exit_code = 'Exit code: {0}'
        err_target_empty = 'The install location is empty.'
        err_target_relative = 'The install location must be a full path (for example C:\Users\you\AppData\Roaming\Autodesk\FusionAddins): {0}'
        err_target_dotdot = 'The install location must not contain ".." segments: {0}'
        err_target_chars = 'The install location contains characters that are not allowed in a path: {0}'
        err_target_file = 'The install location is a file, not a folder: {0}'
        err_target_link = 'The folder {0} is a link (junction or symbolic link). The installer will not replace it. Delete the link or install somewhere else.'
        err_target_occupied = '{0} exists but is a file, not a folder.'
        err_target_self = 'The installer is running from inside the install location ({0}). Copy the installer folder somewhere else (for example the Desktop) and run it again.'
        err_payload_missing = 'The add-in files were not found next to the installer ({0}). Extract the whole zip file first and run the installer from the extracted folder.'
        err_manifest_bad = 'The add-in manifest is missing or is not a valid add-in manifest: {0}'
        err_hash_list_missing = 'The checksum list is missing or unreadable: {0}'
        err_hash_list_bad = 'The checksum list is damaged (line {0}).'
        err_hash_mismatch = 'The installer files are damaged or were changed: {0}. Download the installer again.'
        err_unlisted_file = 'The installer contains a file that is not on the checksum list: {0}. Download the installer again.'
        err_payload_link = 'The installer folder contains a link, which is not allowed: {0}'
        err_swap_failed = 'Could not replace the installed add-in: {0} If Fusion is running, stop the add-in first: press Shift+S, open the Add-Ins tab, select LCSC2Fusion and press Stop. Then run the installer again. The previous version was left in place.'
        err_copy_failed = 'Could not copy the add-in files: {0} The previous version was left in place.'
        err_rollback_failed = 'The installation failed and the previous version could not be restored automatically. It is in the folder {0}. To get it back, delete {1} if it exists and rename the first folder to LCSC2Fusion. Details: {2}'
        err_unexpected = 'Unexpected error: {0}'
    }
    pl = @{
        title = 'Instalator LCSC2Fusion'
        checking = 'Sprawdzanie plików instalatora...'
        payload_ok = 'Pliki instalatora są kompletne i niezmienione ({0} plików, wersja dodatku {1}).'
        target_is = 'Miejsce instalacji: {0}'
        fusion_running = 'Autodesk Fusion jest uruchomiony. Instalacja i tak zadziała; nowa wersja załaduje się po ponownym uruchomieniu dodatku.'
        fusion_not_running = 'Autodesk Fusion nie jest uruchomiony.'
        no_autodesk = 'Uwaga: folder {0} nie istnieje, więc Autodesk Fusion może nie być zainstalowany dla tego użytkownika. Dodatek został zainstalowany mimo to.'
        copying = 'Kopiowanie plików: {0}...'
        replaced_old = 'Poprzednia wersja została zastąpiona.'
        verified = 'Zainstalowane pliki sprawdzono z listą sum kontrolnych.'
        leftover_warning = 'Uwaga: nie udało się w całości usunąć poprzedniej wersji. Została w folderze {0}. Możesz ten folder usunąć.'
        done = 'Gotowe. LCSC2Fusion {0} jest zainstalowany w {1}'
        data_kept = 'Twoje ustawienia i pobrane biblioteki (w {0}) nie zostały naruszone.'
        next_start = 'Następny krok: uruchom Autodesk Fusion. Dodatek uruchomi się sam.'
        next_running = 'Fusion już działa: naciśnij Shift+S, otwórz kartę Add-Ins (Dodatki), wybierz LCSC2Fusion i kliknij Run (Uruchom). Jeśli dodatek już działa, najpierw kliknij Stop (Zatrzymaj), a potem Run, żeby załadować nową wersję.'
        failed = 'Instalacja nie powiodła się.'
        rolled_back = 'Przywrócono poprzednią wersję.'
        exit_code = 'Kod wyjścia: {0}'
        err_target_empty = 'Miejsce instalacji jest puste.'
        err_target_relative = 'Miejsce instalacji musi być pełną ścieżką (na przykład C:\Users\ty\AppData\Roaming\Autodesk\FusionAddins): {0}'
        err_target_dotdot = 'Miejsce instalacji nie może zawierać segmentów "..": {0}'
        err_target_chars = 'Miejsce instalacji zawiera znaki niedozwolone w ścieżce: {0}'
        err_target_file = 'Miejsce instalacji jest plikiem, a nie folderem: {0}'
        err_target_link = 'Folder {0} jest dowiązaniem (junction lub link symboliczny). Instalator go nie zastąpi. Usuń dowiązanie albo zainstaluj gdzie indziej.'
        err_target_occupied = '{0} istnieje, ale jest plikiem, a nie folderem.'
        err_target_self = 'Instalator działa z wnętrza miejsca instalacji ({0}). Skopiuj folder instalatora gdzie indziej (na przykład na Pulpit) i uruchom go ponownie.'
        err_payload_missing = 'Nie znaleziono plików dodatku obok instalatora ({0}). Najpierw rozpakuj cały plik zip i uruchom instalator z rozpakowanego folderu.'
        err_manifest_bad = 'Brakuje manifestu dodatku albo nie jest to poprawny manifest dodatku: {0}'
        err_hash_list_missing = 'Brakuje listy sum kontrolnych albo nie da się jej odczytać: {0}'
        err_hash_list_bad = 'Lista sum kontrolnych jest uszkodzona (wiersz {0}).'
        err_hash_mismatch = 'Pliki instalatora są uszkodzone lub zostały zmienione: {0}. Pobierz instalator ponownie.'
        err_unlisted_file = 'Instalator zawiera plik, którego nie ma na liście sum kontrolnych: {0}. Pobierz instalator ponownie.'
        err_payload_link = 'Folder instalatora zawiera dowiązanie, a to jest niedozwolone: {0}'
        err_swap_failed = 'Nie udało się zastąpić zainstalowanego dodatku: {0} Jeśli Fusion działa, najpierw zatrzymaj dodatek: naciśnij Shift+S, otwórz kartę Add-Ins (Dodatki), wybierz LCSC2Fusion i kliknij Stop (Zatrzymaj). Potem uruchom instalator ponownie. Poprzednia wersja została na miejscu.'
        err_copy_failed = 'Nie udało się skopiować plików dodatku: {0} Poprzednia wersja została na miejscu.'
        err_rollback_failed = 'Instalacja nie powiodła się i nie udało się automatycznie przywrócić poprzedniej wersji. Jest w folderze {0}. Aby ją odzyskać, usuń {1}, jeśli istnieje, i zmień nazwę pierwszego folderu na LCSC2Fusion. Szczegóły: {2}'
        err_unexpected = 'Nieoczekiwany błąd: {0}'
    }
}

#region shared
# This whole region is the same in install.ps1 and uninstall.ps1 (a test compares the two copies).

function Get-Text {
    param([string]$Key, [object[]]$FormatArgs = @())
    $template = $null
    foreach ($language in @($script:Lang, 'en')) {
        $table = $script:Messages[$language]
        if ($table.ContainsKey($Key)) { $template = [string]$table[$Key]; break }
    }
    if ($null -eq $template) { return $Key }
    if ($FormatArgs.Count -eq 0) { return $template }
    return [string]::Format($template, [object[]]$FormatArgs)
}

function Fail {
    param([int]$Code, [string]$Key, [object[]]$FormatArgs = @())
    $failure = New-Object System.Exception -ArgumentList (Get-Text $Key $FormatArgs)
    $failure.Data['ExitCode'] = $Code
    throw $failure
}

function Get-ErrorText {
    param($ErrorRecord)
    $inner = $ErrorRecord.Exception
    while ($null -ne $inner.InnerException) { $inner = $inner.InnerException }
    return $inner.Message
}

function Resolve-Language {
    param([string]$Requested)
    $value = ''
    if ($Requested) { $value = $Requested.Trim().ToLowerInvariant() }
    if ($value -eq 'en' -or $value -eq 'pl') { return $value }
    if ([System.Globalization.CultureInfo]::CurrentUICulture.TwoLetterISOLanguageName -eq 'pl') { return 'pl' }
    return 'en'
}

function Write-Line {
    param([string]$Text)
    Write-Host $Text
}

function Show-Dialog {
    # Returns $true for OK/Yes. LCSC2FUSION_TEST_GUI=yes|no is a test hook: no window, the text
    # goes to the output and the value is the answer.
    param([string]$Text, [string]$Title, [string]$Kind)
    $hook = $env:LCSC2FUSION_TEST_GUI
    if ($hook) {
        Write-Host ('[dialog:{0}] {1}: {2}' -f $Kind, $Title, ($Text -replace '\r?\n', ' | '))
        return ($hook -eq 'yes')
    }
    Add-Type -AssemblyName System.Windows.Forms
    $buttons = [System.Windows.Forms.MessageBoxButtons]::OK
    $icon = [System.Windows.Forms.MessageBoxIcon]::Information
    if ($Kind -eq 'error') { $icon = [System.Windows.Forms.MessageBoxIcon]::Error }
    if ($Kind -eq 'question') {
        $buttons = [System.Windows.Forms.MessageBoxButtons]::YesNo
        $icon = [System.Windows.Forms.MessageBoxIcon]::Question
    }
    # A hidden top most owner keeps the box in front when the process has no visible window.
    $owner = New-Object System.Windows.Forms.Form
    $owner.TopMost = $true
    try {
        $answer = [System.Windows.Forms.MessageBox]::Show($owner, $Text, $Title, $buttons, $icon)
    }
    finally {
        $owner.Dispose()
    }
    return ($answer -eq [System.Windows.Forms.DialogResult]::OK -or $answer -eq [System.Windows.Forms.DialogResult]::Yes)
}

function Test-ReparsePoint {
    param([string]$Path)
    try {
        $attributes = [int][System.IO.File]::GetAttributes($Path)
    }
    catch [System.IO.IOException] { return $false }
    catch [System.UnauthorizedAccessException] { return $false }
    return (($attributes -band [int][System.IO.FileAttributes]::ReparsePoint) -ne 0)
}

function Resolve-TargetRoot {
    # Returns the full path of the folder that holds the add-ins, or exits with code 12.
    param([string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) { Fail 12 'err_target_empty' }
    $raw = $Path.Trim()
    if ($raw.IndexOfAny([System.IO.Path]::GetInvalidPathChars()) -ge 0 -or $raw.IndexOfAny([char[]]@('*', '?')) -ge 0) {
        Fail 12 'err_target_chars' @($raw)
    }
    $isDrivePath = $raw -match '^[A-Za-z]:[\\/]'
    $isUncPath = $raw -match '^\\\\[^\\/]+[\\/][^\\/]+'
    if (-not ($isDrivePath -or $isUncPath)) { Fail 12 'err_target_relative' @($raw) }
    if ($raw.IndexOf(':', 2) -ge 0) { Fail 12 'err_target_chars' @($raw) }
    if (@($raw -split '[\\/]') -contains '..') { Fail 12 'err_target_dotdot' @($raw) }
    $full = [System.IO.Path]::GetFullPath($raw)
    if ($full.Length -gt 3) { $full = $full.TrimEnd('\') }
    if ([System.IO.File]::Exists($full)) { Fail 12 'err_target_file' @($full) }
    return $full
}

function Remove-Tree {
    param([string]$Path)
    if (-not [System.IO.Directory]::Exists($Path)) { return }
    try {
        [System.IO.Directory]::Delete($Path, $true)
    }
    catch {
        # Read only files stop the delete: clear the attribute and try once more.
        foreach ($file in [System.IO.Directory]::GetFiles($Path, '*', [System.IO.SearchOption]::AllDirectories)) {
            [System.IO.File]::SetAttributes($file, [System.IO.FileAttributes]::Normal)
        }
        [System.IO.Directory]::Delete($Path, $true)
    }
}
#endregion shared

function Get-FileSha256 {
    param([string]$Path)
    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    try {
        $stream = [System.IO.File]::OpenRead($Path)
        try { $bytes = $algorithm.ComputeHash($stream) }
        finally { $stream.Dispose() }
    }
    finally { $algorithm.Dispose() }
    return ([System.BitConverter]::ToString($bytes)).Replace('-', '').ToLowerInvariant()
}

function Get-TreeFiles {
    # Relative paths (forward slashes) of all files below $Root. Links are refused (exit 11).
    param([string]$Root)
    $found = New-Object 'System.Collections.Generic.List[string]'
    $pending = New-Object 'System.Collections.Generic.Stack[string]'
    $pending.Push($Root)
    while ($pending.Count -gt 0) {
        $directory = $pending.Pop()
        foreach ($sub in [System.IO.Directory]::GetDirectories($directory)) {
            if (Test-ReparsePoint $sub) { Fail 11 'err_payload_link' @($sub) }
            $pending.Push($sub)
        }
        foreach ($file in [System.IO.Directory]::GetFiles($directory)) {
            if (Test-ReparsePoint $file) { Fail 11 'err_payload_link' @($file) }
            $found.Add($file.Substring($Root.Length + 1).Replace('\', '/'))
        }
    }
    return , $found
}

function Read-HashList {
    # payload.sha256: one line per file, "<sha256 hex>  <relative path with forward slashes>".
    param([string]$Path)
    if (-not [System.IO.File]::Exists($Path)) { Fail 11 'err_hash_list_missing' @($Path) }
    try { $text = [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8) }
    catch { Fail 11 'err_hash_list_missing' @($Path) }
    $map = New-Object 'System.Collections.Generic.Dictionary[string,string]' -ArgumentList ([System.StringComparer]::OrdinalIgnoreCase)
    $number = 0
    foreach ($line in ($text -split '\r?\n')) {
        $number++
        if ($line.Length -eq 0) { continue }
        if ($line -match '^([0-9A-Fa-f]{64})  (\S.*)$') {
            $hash = $Matches[1].ToLowerInvariant()
            $relative = $Matches[2]
        }
        else { Fail 11 'err_hash_list_bad' @($number) }
        $segments = @($relative -split '/')
        if ($relative.Contains('\') -or $relative.Contains(':') -or $segments -contains '' -or $segments -contains '.' -or $segments -contains '..') {
            Fail 11 'err_hash_list_bad' @($number)
        }
        if ($map.ContainsKey($relative)) { Fail 11 'err_hash_list_bad' @($number) }
        $map.Add($relative, $hash)
    }
    if ($map.Count -eq 0) { Fail 11 'err_hash_list_bad' @(1) }
    return , $map
}

function Test-FilesAgainstList {
    # Every listed file must exist with the listed hash, and no other file may exist below $Root.
    param([string]$Root, $HashList)
    $actual = Get-TreeFiles $Root
    $unlisted = @($actual | Where-Object { -not $HashList.ContainsKey($_) })
    if ($unlisted.Count -gt 0) { Fail 11 'err_unlisted_file' @($unlisted[0]) }
    foreach ($relative in $HashList.Keys) {
        $path = [System.IO.Path]::Combine($Root, $relative.Replace('/', '\'))
        if (-not [System.IO.File]::Exists($path)) { Fail 11 'err_hash_mismatch' @($relative) }
        if ((Get-FileSha256 $path) -ne $HashList[$relative]) { Fail 11 'err_hash_mismatch' @($relative) }
    }
}

function Test-Payload {
    # Exit 10: add-in folder or manifest missing. Exit 11: checksum problems.
    param([string]$Source)
    $payloadDir = [System.IO.Path]::GetFullPath([System.IO.Path]::Combine($Source, $script:AddinName))
    $manifestPath = [System.IO.Path]::Combine($payloadDir, "$($script:AddinName).manifest")
    if (-not [System.IO.File]::Exists($manifestPath)) {
        if (-not [System.IO.Directory]::Exists($payloadDir)) { Fail 10 'err_payload_missing' @($payloadDir) }
        Fail 10 'err_manifest_bad' @($manifestPath)
    }
    # ConvertFrom-Json of Windows PowerShell 5.1 rejects the empty key in "description": {"": ...}.
    $manifest = $null
    try {
        Add-Type -AssemblyName System.Web.Extensions
        $serializer = New-Object System.Web.Script.Serialization.JavaScriptSerializer
        $manifest = $serializer.DeserializeObject([System.IO.File]::ReadAllText($manifestPath, [System.Text.Encoding]::UTF8))
    }
    catch { Fail 10 'err_manifest_bad' @($manifestPath) }
    if ($manifest -isnot [System.Collections.IDictionary] -or -not $manifest.ContainsKey('type') -or $manifest['type'] -ne 'addin') {
        Fail 10 'err_manifest_bad' @($manifestPath)
    }
    $version = '?'
    if ($manifest.ContainsKey('version')) { $version = [string]$manifest['version'] }

    $hashList = Read-HashList ([System.IO.Path]::Combine($Source, 'payload.sha256'))
    Test-FilesAgainstList $payloadDir $hashList

    $files = New-Object 'System.Collections.Generic.List[string]'
    foreach ($relative in $hashList.Keys) { $files.Add($relative) }
    $files.Sort([System.StringComparer]::Ordinal)
    return [pscustomobject]@{ PayloadDir = $payloadDir; Files = $files; HashList = $hashList; Version = $version }
}

# =============================================================================================
# Main
# =============================================================================================

$exitCode = 0
$dialogText = ''
$previousEncoding = $null
$encodingChanged = $false
$staging = $null
$aside = $null
$installDir = $null
$movedAside = $false
$swapped = $false

try {
    if ([Console]::IsOutputRedirected) {
        # Redirected output: write UTF-8 so that Polish letters survive. Restored at the end.
        try {
            $previousEncoding = [Console]::OutputEncoding
            [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding -ArgumentList $false
            $encodingChanged = $true
        }
        catch { $encodingChanged = $false }
    }
    $script:Lang = Resolve-Language $Language

    # Where to install.
    $isDefaultTarget = $false
    if (-not $PSBoundParameters.ContainsKey('TargetRoot')) {
        if (-not [string]::IsNullOrEmpty($env:LCSC2FUSION_FUSIONADDINS_DIR)) {
            $TargetRoot = $env:LCSC2FUSION_FUSIONADDINS_DIR
        }
        else {
            if ([string]::IsNullOrEmpty($env:APPDATA)) { Fail 12 'err_target_empty' }
            $TargetRoot = [System.IO.Path]::Combine($env:APPDATA, 'Autodesk\FusionAddins')
            $isDefaultTarget = $true
        }
    }
    $root = Resolve-TargetRoot $TargetRoot

    # Where the files come from.
    if (-not $PSBoundParameters.ContainsKey('SourceDir') -or [string]::IsNullOrWhiteSpace($SourceDir)) {
        $SourceDir = $PSScriptRoot
        if ([string]::IsNullOrEmpty($SourceDir)) { $SourceDir = (Get-Location).Path }
    }
    $source = [System.IO.Path]::GetFullPath($SourceDir).TrimEnd('\')

    Write-Line (Get-Text 'checking')
    $payload = Test-Payload $source
    Write-Line (Get-Text 'payload_ok' @($payload.Files.Count, $payload.Version))

    # The install folder: not a link, not a file, and not the folder the installer runs from.
    $installDir = [System.IO.Path]::Combine($root, $script:AddinName)
    $sourceWithSeparator = $source.TrimEnd('\') + '\'
    if ($payload.PayloadDir -ieq $installDir -or $sourceWithSeparator.StartsWith($installDir + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
        Fail 12 'err_target_self' @($installDir)
    }
    if (Test-ReparsePoint $installDir) { Fail 12 'err_target_link' @($installDir) }
    if ([System.IO.File]::Exists($installDir)) { Fail 12 'err_target_occupied' @($installDir) }
    Write-Line (Get-Text 'target_is' @($installDir))
    $autodeskDir = [System.IO.Path]::GetDirectoryName($root)
    if ($isDefaultTarget -and $autodeskDir -and -not [System.IO.Directory]::Exists($autodeskDir)) {
        Write-Line (Get-Text 'no_autodesk' @($autodeskDir))
    }
    if (@(Get-Process -Name 'Fusion360' -ErrorAction SilentlyContinue).Count -gt 0) {
        $fusionRunning = $true
        Write-Line (Get-Text 'fusion_running')
    }
    else {
        $fusionRunning = $false
        Write-Line (Get-Text 'fusion_not_running')
    }
    [void][System.IO.Directory]::CreateDirectory($root)

    # Install: stage, check, rename the old one aside, rename the new one in, check again.
    $suffix = [guid]::NewGuid().ToString('N').Substring(0, 8)
    $staging = [System.IO.Path]::Combine($root, ".$($script:AddinName).new-$suffix")
    $aside = [System.IO.Path]::Combine($root, ".$($script:AddinName).old-$suffix")
    $fault = $env:LCSC2FUSION_TEST_FAULT   # test hook: copy-midway, after-aside, after-swap

    Write-Line (Get-Text 'copying' @($payload.Files.Count))
    try {
        [void][System.IO.Directory]::CreateDirectory($staging)
        $copied = 0
        foreach ($relative in $payload.Files) {
            $windowsRelative = $relative.Replace('/', '\')
            $from = [System.IO.Path]::Combine($payload.PayloadDir, $windowsRelative)
            $to = [System.IO.Path]::Combine($staging, $windowsRelative)
            [void][System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($to))
            [System.IO.File]::Copy($from, $to, $false)
            try { Unblock-File -LiteralPath $to } catch { }   # drop the "downloaded from the internet" mark
            $copied++
            if ($fault -eq 'copy-midway' -and $copied -ge ($payload.Files.Count / 2)) { throw 'injected fault: copy-midway' }
        }
        Test-FilesAgainstList $staging $payload.HashList
    }
    catch {
        $reason = Get-ErrorText $_
        try { Remove-Tree $staging } catch { }
        $staging = $null
        Fail 13 'err_copy_failed' @($reason)
    }

    try {
        if ([System.IO.Directory]::Exists($installDir)) {
            [System.IO.Directory]::Move($installDir, $aside)
            $movedAside = $true
        }
        if ($fault -eq 'after-aside') { throw 'injected fault: after-aside' }
        [System.IO.Directory]::Move($staging, $installDir)
        $staging = $null
        $swapped = $true
        if ($fault -eq 'after-swap') { throw 'injected fault: after-swap' }
        Test-FilesAgainstList $installDir $payload.HashList
    }
    catch {
        $reason = Get-ErrorText $_
        # Roll back: remove what was put in place, bring the old folder back, drop the staging folder.
        try {
            if ($swapped) { Remove-Tree $installDir; $swapped = $false }
            if ($movedAside) {
                [System.IO.Directory]::Move($aside, $installDir)
                $movedAside = $false
                Write-Line (Get-Text 'rolled_back')
            }
        }
        catch { Fail 13 'err_rollback_failed' @($aside, $installDir, (Get-ErrorText $_)) }
        if ($null -ne $staging) { try { Remove-Tree $staging } catch { }; $staging = $null }
        Fail 13 'err_swap_failed' @($reason)
    }
    Write-Line (Get-Text 'verified')

    if ($movedAside) {
        Write-Line (Get-Text 'replaced_old')
        try { Remove-Tree $aside }
        catch { Write-Line (Get-Text 'leftover_warning' @($aside)) }
        $movedAside = $false
    }

    $dataDir = [System.IO.Path]::Combine([string]$env:LOCALAPPDATA, $script:AddinName)
    $lines = New-Object 'System.Collections.Generic.List[string]'
    $lines.Add((Get-Text 'done' @($payload.Version, $installDir)))
    if (-not [string]::IsNullOrEmpty($env:LOCALAPPDATA)) { $lines.Add((Get-Text 'data_kept' @($dataDir))) }
    $lines.Add((Get-Text 'next_start'))
    if ($fusionRunning) { $lines.Add((Get-Text 'next_running')) }
    foreach ($line in $lines) { Write-Line $line }
    $dialogText = ($lines -join "`r`n`r`n")
}
catch {
    $failure = $_.Exception
    if ($failure.Data.Contains('ExitCode')) {
        $exitCode = [int]$failure.Data['ExitCode']
        $message = $failure.Message
    }
    else {
        $exitCode = 14
        $message = Get-Text 'err_unexpected' @((Get-ErrorText $_))
    }
    # A failure after the cleanup above must not leave the staging folder behind.
    if ($null -ne $staging) { try { Remove-Tree $staging } catch { } }
    Write-Line (Get-Text 'failed')
    Write-Line $message
    Write-Line (Get-Text 'exit_code' @($exitCode))
    $dialogText = (Get-Text 'failed') + "`r`n`r`n" + $message
}
finally {
    if ($encodingChanged) {
        try { [Console]::OutputEncoding = $previousEncoding } catch { }
    }
}

if ($Gui -and -not $Silent) {
    $kind = 'info'
    if ($exitCode -ne 0) { $kind = 'error' }
    try { [void](Show-Dialog $dialogText (Get-Text 'title') $kind) } catch { }
}
exit $exitCode
