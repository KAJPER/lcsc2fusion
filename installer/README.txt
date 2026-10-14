LCSC2Fusion installer for Windows
=================================

ENGLISH

LCSC2Fusion is an open source add-in for Autodesk Fusion (Electronics). It turns LCSC parts
into libraries with symbol, footprint and optional 3D model. This package installs it for your
Windows user. It needs no administrator rights, no Python and no internet connection.

Install
  1. Extract the whole zip file (right click, Extract All). Do not run files from inside the zip.
  2. Double click Install-LCSC2Fusion.bat. A console window shows what happens.
  3. Start Autodesk Fusion. The add-in starts by itself.
     If Fusion is already running: press Shift+S, open the Add-Ins tab, select LCSC2Fusion and
     press Run. If it is already running there, press Stop first, then Run, so the new version
     loads.

Update: do the same with the installer of the new version. The old version is replaced; your
settings and saved state in %LOCALAPPDATA%\LCSC2Fusion stay.

Uninstall
  Double click Uninstall-LCSC2Fusion.bat. It removes the add-in folder only. To delete your
  settings and state as well, open a console in this folder and run
  Uninstall-LCSC2Fusion.bat -RemoveData (it asks first). Your .lbr files are never deleted.

What the installer does
  - checks every add-in file against payload.sha256 (SHA-256) and stops if one differs;
  - copies the add-in to %APPDATA%\Autodesk\FusionAddins\LCSC2Fusion, replacing the old version,
    and puts the old version back if anything fails;
  - touches nothing else: other add-ins, Fusion itself and the registry stay as they are.

Windows warnings
  These files are not signed with a code signing certificate. Windows SmartScreen may say
  "Windows protected your PC" (click More info, Run anyway), and a browser may warn about the
  download. The scripts are plain text: open Install-LCSC2Fusion.bat, install.ps1 and
  uninstall.ps1 in Notepad and read them before you run them. The checksums of the release files
  are in SHA256SUMS.txt on the release page; compare with
  PowerShell: Get-FileHash <file> -Algorithm SHA256
  Antivirus programs are often suspicious of unsigned self extracting programs. If the setup exe
  is blocked, use this zip instead.

Setup exe
  LCSC2Fusion-Setup-<version>.exe does the same in one file: it asks once, unpacks this installer
  into a temporary folder, runs install.ps1, deletes the temporary folder and shows the result in
  a message box. It is built with IExpress, which is part of Windows. The installer output is
  written to %TEMP%\LCSC2Fusion-setup.log.

If it does not work
  - "Could not replace the installed add-in": stop the add-in first (Shift+S, Add-Ins tab,
    LCSC2Fusion, Stop) and run the installer again. The old version was left in place.
  - Scripts are blocked by your organization: use the add-in zip
    (LCSC2Fusion-<version>-addin.zip) and extract it into %APPDATA%\Autodesk\FusionAddins so that
    %APPDATA%\Autodesk\FusionAddins\LCSC2Fusion\LCSC2Fusion.manifest exists.
  - Exit codes of install.ps1: 0 ok, 10 add-in files or manifest missing, 11 checksum problem,
    12 invalid install location, 13 copy or swap failed, 14 unexpected error.

Project: https://github.com/KAJPER/lcsc2fusion (MIT license)


POLSKI

LCSC2Fusion to dodatek open source do Autodesk Fusion (Electronics). Zamienia części z LCSC na
biblioteki z symbolem, footprintem i opcjonalnym modelem 3D. Ta paczka instaluje go dla Twojego
użytkownika Windows. Nie potrzebuje praw administratora, Pythona ani internetu.

Instalacja
  1. Rozpakuj cały plik zip (prawy przycisk, Wyodrębnij wszystko). Nie uruchamiaj plików
     z wnętrza zipa.
  2. Kliknij dwukrotnie Install-LCSC2Fusion.bat. Okno konsoli pokaże, co się dzieje.
  3. Uruchom Autodesk Fusion. Dodatek uruchomi się sam.
     Jeśli Fusion już działa: naciśnij Shift+S, otwórz kartę Add-Ins, wybierz LCSC2Fusion
     i kliknij Run. Jeśli dodatek już tam działa, najpierw kliknij Stop, potem Run, żeby
     załadować nową wersję.

Aktualizacja: zrób to samo z instalatorem nowej wersji. Stara wersja zostanie zastąpiona;
Twoje ustawienia i zapisany stan w %LOCALAPPDATA%\LCSC2Fusion zostają.

Odinstalowanie
  Kliknij dwukrotnie Uninstall-LCSC2Fusion.bat. Usuwa tylko folder dodatku. Żeby usunąć też
  ustawienia i stan, otwórz konsolę w tym folderze i uruchom
  Uninstall-LCSC2Fusion.bat -RemoveData (najpierw zapyta). Twoje pliki .lbr nigdy nie są
  usuwane.

Co robi instalator
  - sprawdza każdy plik dodatku z payload.sha256 (SHA-256) i zatrzymuje się, jeśli któryś
    się różni;
  - kopiuje dodatek do %APPDATA%\Autodesk\FusionAddins\LCSC2Fusion, zastępując starą wersję,
    a w razie błędu przywraca starą wersję;
  - niczego innego nie rusza: inne dodatki, sam Fusion i rejestr zostają bez zmian.

Ostrzeżenia Windows
  Te pliki nie są podpisane certyfikatem. Windows SmartScreen może napisać "System Windows
  ochronił ten komputer" (kliknij Więcej informacji, Uruchom mimo to), a przeglądarka może
  ostrzegać przed pobraniem. Skrypty to zwykły tekst: otwórz Install-LCSC2Fusion.bat,
  install.ps1 i uninstall.ps1 w Notatniku i przeczytaj je przed uruchomieniem. Sumy kontrolne
  plików wydania są w SHA256SUMS.txt na stronie wydania; porównaj w PowerShellu:
  Get-FileHash <plik> -Algorithm SHA256
  Programy antywirusowe często podchodzą nieufnie do niepodpisanych samorozpakowujących się
  programów. Jeśli exe jest blokowany, użyj tego zipa.

Exe instalatora
  LCSC2Fusion-Setup-<wersja>.exe robi to samo w jednym pliku: raz pyta, rozpakowuje ten
  instalator do folderu tymczasowego, uruchamia install.ps1, usuwa folder tymczasowy i pokazuje
  wynik w oknie komunikatu. Jest zbudowany przez IExpress, który jest częścią Windows. Wyjście
  instalatora trafia do %TEMP%\LCSC2Fusion-setup.log.

Gdy coś nie działa
  - "Nie udało się zastąpić zainstalowanego dodatku": najpierw zatrzymaj dodatek (Shift+S, karta
    Add-Ins, LCSC2Fusion, Stop) i uruchom instalator ponownie. Stara wersja została na miejscu.
  - Skrypty są zablokowane przez Twoją organizację: użyj zipa dodatku
    (LCSC2Fusion-<wersja>-addin.zip) i rozpakuj go do %APPDATA%\Autodesk\FusionAddins tak, żeby
    istniał plik %APPDATA%\Autodesk\FusionAddins\LCSC2Fusion\LCSC2Fusion.manifest.
  - Kody wyjścia install.ps1: 0 w porządku, 10 brak plików dodatku lub manifestu, 11 problem
    z sumą kontrolną, 12 niepoprawne miejsce instalacji, 13 kopiowanie lub podmiana nie
    powiodły się, 14 nieoczekiwany błąd.

Projekt: https://github.com/KAJPER/lcsc2fusion (licencja MIT)
