<#
.SYNOPSIS
    Removes the LCSC2Fusion add-in from Autodesk Fusion (current user, no admin rights).

.DESCRIPTION
    Deletes <TargetRoot>\LCSC2Fusion, but only when that folder contains LCSC2Fusion.manifest
    (an arbitrary folder is never deleted). Nothing else in TargetRoot is touched. With
    -RemoveData the settings and state folder %LOCALAPPDATA%\LCSC2Fusion is deleted too, after
    a confirmation (no confirmation with -Silent).

    This file is plain text. Read it before you run it.

.PARAMETER TargetRoot
    Folder that holds Fusion add-ins. Default: %APPDATA%\Autodesk\FusionAddins
    (the environment variable LCSC2FUSION_FUSIONADDINS_DIR replaces the default, for tests).

.PARAMETER RemoveData
    Also delete %LOCALAPPDATA%\LCSC2Fusion (state.json, api.json, the window's browser profile).

.PARAMETER Language
    auto (Windows display language), en or pl.

.PARAMETER Silent
    No prompts, no pauses, no message boxes: only text output and the exit code.

.PARAMETER Gui
    No console interaction: the result is shown in a message box.

.NOTES
    Exit codes: 0 removed, 12 invalid TargetRoot or the folder is not this add-in, 13 removal
    failed (stop the add-in in Fusion first), 14 unexpected error, 15 nothing to uninstall
    (counts as success).
#>
[CmdletBinding()]
param(
    [string]$TargetRoot,
    [switch]$RemoveData,
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
        title = 'LCSC2Fusion uninstall'
        fusion_running = 'Autodesk Fusion is running. If files are locked, stop the add-in first: press Shift+S, open the Add-Ins tab, select LCSC2Fusion and press Stop.'
        removing = 'Removing {0}...'
        removed = 'Removed the add-in from {0}'
        nothing = 'Nothing to uninstall: the add-in is not installed in {0}'
        done = 'Done. LCSC2Fusion was uninstalled. If Fusion is running, restart it to make the LCSC2Fusion button disappear.'
        leftover_warning = 'Warning: the add-in folder could not be deleted completely. What is left is in {0}. You can delete that folder.'
        data_prompt = 'Also delete your LCSC2Fusion settings and state in {0}? This removes the saved library id, the list of added parts and the browser profile of the LCSC2Fusion window. Your .lbr files and the libraries in Fusion are not touched.'
        data_answer = 'Delete it? [y/N]'
        data_removed = 'Removed {0}'
        data_kept = 'Your settings and state in {0} were kept.'
        data_none = 'There is no settings folder ({0}), nothing to remove.'
        failed = 'The uninstall failed.'
        exit_code = 'Exit code: {0}'
        err_target_empty = 'The install location is empty.'
        err_target_relative = 'The install location must be a full path (for example C:\Users\you\AppData\Roaming\Autodesk\FusionAddins): {0}'
        err_target_dotdot = 'The install location must not contain ".." segments: {0}'
        err_target_chars = 'The install location contains characters that are not allowed in a path: {0}'
        err_target_file = 'The install location is a file, not a folder: {0}'
        err_target_link = 'The folder {0} is a link (junction or symbolic link). The uninstaller will not delete it. Remove the link yourself if you want it gone.'
        err_target_occupied = '{0} exists but is a file, not a folder.'
        err_not_addin = 'The folder {0} does not contain LCSC2Fusion.manifest, so it is not this add-in. It was left alone.'
        err_data_path = 'The settings location could not be determined from the LOCALAPPDATA variable: {0}'
        err_remove_failed = 'Could not remove the add-in: {0} If Fusion is running, stop the add-in first: press Shift+S, open the Add-Ins tab, select LCSC2Fusion and press Stop. Then run the uninstaller again. The add-in was left in place.'
        err_data_remove_failed = 'Could not remove {0}: {1} Close the LCSC2Fusion window and Fusion, then run the uninstaller again.'
        err_unexpected = 'Unexpected error: {0}'
    }
    pl = @{
        title = 'Odinstalowanie LCSC2Fusion'
        fusion_running = 'Autodesk Fusion jest uruchomiony. Jeśli pliki są zablokowane, najpierw zatrzymaj dodatek: naciśnij Shift+S, otwórz kartę Add-Ins (Dodatki), wybierz LCSC2Fusion i kliknij Stop (Zatrzymaj).'
        removing = 'Usuwanie {0}...'
        removed = 'Usunięto dodatek z {0}'
        nothing = 'Nie ma czego odinstalować: dodatek nie jest zainstalowany w {0}'
        done = 'Gotowe. LCSC2Fusion został odinstalowany. Jeśli Fusion działa, uruchom go ponownie, żeby zniknął przycisk LCSC2Fusion.'
        leftover_warning = 'Uwaga: nie udało się w całości usunąć folderu dodatku. To, co zostało, jest w {0}. Możesz ten folder usunąć.'
        data_prompt = 'Usunąć też ustawienia i stan LCSC2Fusion w {0}? Zniknie zapisany identyfikator biblioteki, lista dodanych części i profil przeglądarki okna LCSC2Fusion. Twoje pliki .lbr i biblioteki we Fusionie zostaną nietknięte.'
        data_answer = 'Usunąć? [t/N]'
        data_removed = 'Usunięto {0}'
        data_kept = 'Twoje ustawienia i stan w {0} zostały zachowane.'
        data_none = 'Nie ma folderu z ustawieniami ({0}), nie ma czego usuwać.'
        failed = 'Odinstalowanie nie powiodło się.'
        exit_code = 'Kod wyjścia: {0}'
        err_target_empty = 'Miejsce instalacji jest puste.'
        err_target_relative = 'Miejsce instalacji musi być pełną ścieżką (na przykład C:\Users\ty\AppData\Roaming\Autodesk\FusionAddins): {0}'
        err_target_dotdot = 'Miejsce instalacji nie może zawierać segmentów "..": {0}'
        err_target_chars = 'Miejsce instalacji zawiera znaki niedozwolone w ścieżce: {0}'
        err_target_file = 'Miejsce instalacji jest plikiem, a nie folderem: {0}'
        err_target_link = 'Folder {0} jest dowiązaniem (junction lub link symboliczny). Program go nie usunie. Jeśli chcesz się go pozbyć, usuń dowiązanie samodzielnie.'
        err_target_occupied = '{0} istnieje, ale jest plikiem, a nie folderem.'
        err_not_addin = 'Folder {0} nie zawiera pliku LCSC2Fusion.manifest, więc nie jest to ten dodatek. Zostawiono go bez zmian.'
        err_data_path = 'Nie udało się ustalić miejsca ustawień ze zmiennej LOCALAPPDATA: {0}'
        err_remove_failed = 'Nie udało się usunąć dodatku: {0} Jeśli Fusion działa, najpierw zatrzymaj dodatek: naciśnij Shift+S, otwórz kartę Add-Ins (Dodatki), wybierz LCSC2Fusion i kliknij Stop (Zatrzymaj). Potem uruchom program ponownie. Dodatek został na miejscu.'
        err_data_remove_failed = 'Nie udało się usunąć {0}: {1} Zamknij okno LCSC2Fusion i Fusion, potem uruchom program ponownie.'
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

# =============================================================================================
# Main
# =============================================================================================

$exitCode = 0
$dialogText = ''
$previousEncoding = $null
$encodingChanged = $false

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

    if (-not $PSBoundParameters.ContainsKey('TargetRoot')) {
        if (-not [string]::IsNullOrEmpty($env:LCSC2FUSION_FUSIONADDINS_DIR)) {
            $TargetRoot = $env:LCSC2FUSION_FUSIONADDINS_DIR
        }
        else {
            if ([string]::IsNullOrEmpty($env:APPDATA)) { Fail 12 'err_target_empty' }
            $TargetRoot = [System.IO.Path]::Combine($env:APPDATA, 'Autodesk\FusionAddins')
        }
    }
    $root = Resolve-TargetRoot $TargetRoot
    $installDir = [System.IO.Path]::Combine($root, $script:AddinName)
    $lines = New-Object 'System.Collections.Generic.List[string]'
    $removedAddin = $false
    $removedData = $false

    if (Test-ReparsePoint $installDir) { Fail 12 'err_target_link' @($installDir) }
    if ([System.IO.File]::Exists($installDir)) { Fail 12 'err_target_occupied' @($installDir) }

    if ([System.IO.Directory]::Exists($installDir)) {
        $manifest = [System.IO.Path]::Combine($installDir, "$($script:AddinName).manifest")
        if (-not [System.IO.File]::Exists($manifest)) { Fail 12 'err_not_addin' @($installDir) }
        if (@(Get-Process -Name 'Fusion360' -ErrorAction SilentlyContinue).Count -gt 0) {
            Write-Line (Get-Text 'fusion_running')
        }
        Write-Line (Get-Text 'removing' @($installDir))
        # Rename first: a locked file makes the rename fail and leaves the add-in whole.
        $suffix = [guid]::NewGuid().ToString('N').Substring(0, 8)
        $aside = [System.IO.Path]::Combine($root, ".$($script:AddinName).removing-$suffix")
        try { [System.IO.Directory]::Move($installDir, $aside) }
        catch { Fail 13 'err_remove_failed' @((Get-ErrorText $_)) }
        try { Remove-Tree $aside }
        catch { Write-Line (Get-Text 'leftover_warning' @($aside)) }
        $removedAddin = $true
        $lines.Add((Get-Text 'removed' @($installDir)))
    }
    else {
        $lines.Add((Get-Text 'nothing' @($installDir)))
    }

    if ($RemoveData) {
        try { $localAppData = Resolve-TargetRoot $env:LOCALAPPDATA }
        catch { Fail 12 'err_data_path' @([string]$env:LOCALAPPDATA) }
        $dataDir = [System.IO.Path]::Combine($localAppData, $script:AddinName)
        if (Test-ReparsePoint $dataDir) { Fail 12 'err_target_link' @($dataDir) }
        if ([System.IO.Directory]::Exists($dataDir)) {
            $confirmed = $true
            if (-not $Silent) {
                $question = Get-Text 'data_prompt' @($dataDir)
                if ($Gui) {
                    $confirmed = Show-Dialog $question (Get-Text 'title') 'question'
                }
                else {
                    Write-Line $question
                    try { $answer = Read-Host (Get-Text 'data_answer') }
                    catch { $answer = '' }   # no input at all means no
                    $confirmed = ($answer -match '^\s*(y|yes|t|tak)\s*$')
                }
            }
            if ($confirmed) {
                try { Remove-Tree $dataDir }
                catch { Fail 13 'err_data_remove_failed' @($dataDir, (Get-ErrorText $_)) }
                $removedData = $true
                $lines.Add((Get-Text 'data_removed' @($dataDir)))
            }
            else {
                $lines.Add((Get-Text 'data_kept' @($dataDir)))
            }
        }
        else {
            $lines.Add((Get-Text 'data_none' @($dataDir)))
        }
    }

    if ($removedAddin) { $lines.Add((Get-Text 'done')) }
    if (-not $removedAddin -and -not $removedData) { $exitCode = 15 }
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
    if ($exitCode -notin 0, 15) { $kind = 'error' }
    try { [void](Show-Dialog $dialogText (Get-Text 'title') $kind) } catch { }
}
exit $exitCode
