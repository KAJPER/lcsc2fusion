/* LCSC2Fusion texts in English and Polish. Plain script (no modules, no network), loaded before
 * http-bridge.js and app.js. The saved choice and the list of languages come from theme-init.js
 * (window.LcscPrefs); this file holds the texts and the code that applies them.
 *
 * Three kinds of text:
 *
 *   1. Interface texts, DICTIONARIES[language][key]. A value is a string with {name} placeholders,
 *      or a plural object {one, few, many, other} that is picked with params.n and Intl.PluralRules
 *      (a missing form falls back to `other`). A key missing in a language falls back to English,
 *      then to the key itself, so a gap never breaks the page.
 *
 *   2. Markup hooks, applied by apply(root):
 *        data-i18n="key"                       the text of the element
 *        data-i18n-attr="aria-label:key;title:key2"   attributes of the element
 *        data-i18n-params='{"id":"C1"}'        {name} values for both of the above (JSON)
 *        data-msg="English text from Python"   a message of the controller, see 3.
 *      The English text in index.html is the default for a reader without script and must equal
 *      DICTIONARIES.en (a test checks it).
 *
 *   3. Messages from Python (status lines, errors, warnings, the saved card "instructions"). The
 *      controller, the flow and the adapter send English sentences and the protocol stays as it is.
 *      The page translates them with MESSAGES, an ordered list; the first entry whose `re`
 *      matches the whole (trimmed) text wins:
 *        { re: /^Found (?<n>\d+) part\(s\) for (?<query>.+)\.$/,
 *          pl: 'Znaleziono {n} części dla {query}.' }
 *      `re` is anchored, has no g or y flag and uses NAMED groups. Per language the entry has a
 *      template: a string with {group}, {group:msg} (the group text is translated as a message
 *      too, for a sentence that is glued to another one), '@key' (use DICTIONARIES[..][key], to
 *      share a text with the interface), or a plural object that is picked with the group `n`.
 *      English is never translated: the original text is shown. A text without an entry is shown
 *      as it came, so an unknown message never breaks anything. tests/test_addin_i18n_messages.py
 *      fails when a message that the Python sources can send has no entry.
 *
 * Texts that quote EasyEDA data (pad names and the like) have no entry on purpose.
 * No dash characters (em dash, en dash, double hyphen) in any text: a test enforces it.
 */
(function () {
  'use strict';

  const FALLBACK = 'en';

  const DICTIONARIES = {
    en: {
      'app.subtitle': 'Add LCSC parts to Fusion Electronics as symbol and footprint',
      'settings.open': 'Settings',
      'settings.theme': 'Theme',
      'settings.theme.system': 'System',
      'settings.theme.light': 'Light',
      'settings.theme.dark': 'Dark',
      'settings.language': 'Language',
      'mock.banner': 'Mock mode: this page is not running inside Fusion. The data below is fake.',
      'search.label': 'LCSC number or search phrase',
      'search.button': 'Search',
      'search.placeholder': 'C2040 or RP2040',
      'search.hint': 'Example: C2040, or words such as "0603 100nF".',
      'options.legend': 'Options',
      'option.with3d': 'Include 3D model (STEP)',
      'option.with3d.hint': 'Saved next to the library. With the option below, Fusion opens it for you.',
      'option.toFusion': 'Add to the Fusion library right away',
      'option.toFusion.hint':
        'Merges the part into your LCSC2Fusion Library. Fusion may stop responding for up to a minute.',
      'status.connecting': 'Connecting to Fusion...',
      'status.level.info': 'Info',
      'status.level.ok': 'OK',
      'status.level.warn': 'Warning',
      'status.level.error': 'Error',
      'status.busy': 'Another request is still running. Please wait for it.',
      'status.typeFirst': 'Type an LCSC number or a search phrase first.',
      'status.requestFailed': 'The request failed.',
      'error.dismiss': 'Dismiss',
      'error.unknown': 'Unknown error.',
      'error.notConnected': 'Not connected to Fusion.',
      'error.cannotReach': 'Could not reach Fusion: {reason}',
      'error.noBridge': 'The Fusion bridge (adsk.fusionSendData) is not available.',
      'idle.title': 'No search yet',
      'idle.text':
        'Results appear here with stock and a link to the LCSC page. Parts you add are listed below them.',
      'results.heading': 'Results',
      'results.headingFor': 'Results for "{query}"',
      'results.searching': 'Searching for "{query}"...',
      'results.count': { one: '{n} part', other: '{n} parts' },
      'results.tableLabel': 'Search results',
      'results.empty.title': 'No parts found',
      'results.empty.text': 'Check the LCSC number, or try fewer words, for example "0603 100nF".',
      'col.lcsc': 'LCSC',
      'col.mpn': 'MPN',
      'col.type': 'Type',
      'col.manufacturer': 'Manufacturer',
      'col.package': 'Package',
      'col.stock': 'Stock',
      'col.description': 'Description',
      'col.datasheet': 'Datasheet',
      'col.action': 'Action',
      'stock.in': 'in stock',
      'stock.out': 'Out of stock',
      'button.add': 'Add to library',
      'button.adding': 'Adding...',
      'button.added': 'Added',
      'button.addLabel': 'Add {part} to library',
      'link.lcscLabel': 'Open {id} on LCSC',
      'link.lcscTitle': 'Open {id} on LCSC (opens in your browser)',
      'link.sheetText': 'Datasheet',
      'link.sheetLabel': 'Open the datasheet of {id}',
      'link.sheetTitle': 'Open the datasheet of {id} (opens in your browser)',
      'added.heading': 'Saved libraries',
      'card.sentToFusion': 'Sent to Fusion',
      'card.saved': 'Saved',
      'card.badge3d': '3D',
      'card.noModelTitle': 'No 3D model.',
      'card.noModelText':
        'A 3D model was requested but none was produced for this part, so only the symbol and footprint were saved.',
      'card.noModelTextReason':
        'A 3D model was requested but none was produced for this part, so only the symbol and footprint were saved. The reason is in the warnings above.',
      'card.howTo': 'How to use it in Fusion',
      'card.openFolder': 'Open folder',
      'button.addToFusion': 'Add to Fusion library',
      'button.addToFusionLabel': 'Add to Fusion library for {id}',
      'button.openSeparate': 'Open as a separate library',
      'button.openSeparateTitle':
        'Opens only this part as its own library in the Fusion Electronics editor',
      'button.sending': 'Sending...',
      'fusion.headIdle': 'Not in the Fusion library yet.',
      'fusion.headSending': 'Sending to Fusion...',
      'fusion.detailSending': 'Fusion may not respond for up to a minute.',
      'fusion.headFail': 'Not added to the Fusion library.',
      'fusion.headFailDetail': 'Not added to the Fusion library:',
      'fusion.headIn': 'In {name}:',
      'fusion.headInDefault': 'In the Fusion library:',
      'fusion.detailPresent': 'already there, not added again.',
      'fusion.detailCreated': 'created the library with this part.',
      'fusion.detailMerged': 'added to the library.',
      'fusion.detailSaved': 'saved.',
      'fusion.threeDStarted':
        '3D package opened: check the model position, then Finish, Save, OK in Fusion.',
      'fusion.threeDFailed': '3D failed: {reason}',
      'fusion.threeDSkipped': '3D skipped: {reason}',
      'fusion.seeWarnings': 'see the warnings below.',
      'fusion.noReport': 'Fusion did not report back. Try again.',
      'model.summary': 'Attach the 3D model by hand',
      'model.step1':
        'Open the library in Fusion Electronics ("Open as a separate library" above) and save it to a project.',
      'model.step2':
        'In the Content Manager open the Footprints tab, right click the footprint and choose "Attach Existing 3D Model".',
      'model.step3': 'Click "Select from my computer..." and pick the placed STEP file:',
      'model.step4': 'Name the package and press OK.',
      'model.step5':
        'If the model sits wrong, edit the package to adjust the alignment. Then use File > Save.',
      'model.placed': 'Placed (use this)',
      'model.raw': 'Raw (as downloaded)',
      'model.note': 'Model data: JLCEDA/EasyEDA Official Library, https://easyeda.com . Personal use only.',
      'footer.text':
        'Data: JLCEDA/EasyEDA Official Library, https://easyeda.com . Personal use; you are responsible for complying with the EasyEDA and LCSC terms.',
      'bridge.lost': 'Lost the connection to Fusion. If Fusion is closed, close this window.',
      'bridge.stopped': 'LCSC2Fusion was stopped in Fusion. You can close this window.',
      'bridge.requestFailed':
        'The request to Fusion failed. Check that Fusion is running, then try again.',
    },

    pl: {
      'app.subtitle': 'Dodawaj części z LCSC do Fusion Electronics jako symbol i footprint',
      'settings.open': 'Ustawienia',
      'settings.theme': 'Motyw',
      'settings.theme.system': 'Systemowy',
      'settings.theme.light': 'Jasny',
      'settings.theme.dark': 'Ciemny',
      'settings.language': 'Język',
      'mock.banner':
        'Tryb demonstracyjny: ta strona nie działa wewnątrz Fusion. Poniższe dane są przykładowe.',
      'search.label': 'Numer LCSC lub fraza wyszukiwania',
      'search.button': 'Szukaj',
      'search.placeholder': 'C2040 lub RP2040',
      'search.hint': 'Przykład: C2040 albo słowa, np. "0603 100nF".',
      'options.legend': 'Opcje',
      'option.with3d': 'Dołącz model 3D (STEP)',
      'option.with3d.hint':
        'Zapisywany obok biblioteki. Z opcją poniżej Fusion otworzy go automatycznie.',
      'option.toFusion': 'Od razu dodaj do biblioteki Fusion',
      'option.toFusion.hint':
        'Dołącza część do Twojej biblioteki LCSC2Fusion Library. Fusion może nie odpowiadać nawet przez minutę.',
      'status.connecting': 'Łączenie z Fusion...',
      'status.level.info': 'Info',
      'status.level.ok': 'OK',
      'status.level.warn': 'Uwaga',
      'status.level.error': 'Błąd',
      'status.busy': 'Inne żądanie jest jeszcze w toku. Poczekaj, aż się zakończy.',
      'status.typeFirst': 'Najpierw wpisz numer LCSC lub frazę wyszukiwania.',
      'status.requestFailed': 'Żądanie nie powiodło się.',
      'error.dismiss': 'Zamknij',
      'error.unknown': 'Nieznany błąd.',
      'error.notConnected': 'Brak połączenia z Fusion.',
      'error.cannotReach': 'Nie udało się połączyć z Fusion: {reason}',
      'error.noBridge': 'Połączenie z Fusion (adsk.fusionSendData) jest niedostępne.',
      'idle.title': 'Brak wyszukiwania',
      'idle.text':
        'Wyniki pojawią się tutaj ze stanem magazynowym i linkiem do strony LCSC. Dodane części zobaczysz pod nimi.',
      'results.heading': 'Wyniki',
      'results.headingFor': 'Wyniki dla "{query}"',
      'results.searching': 'Wyszukiwanie "{query}"...',
      'results.count': {
        one: '{n} część',
        few: '{n} części',
        many: '{n} części',
        other: '{n} części',
      },
      'results.tableLabel': 'Wyniki wyszukiwania',
      'results.empty.title': 'Nie znaleziono części',
      'results.empty.text': 'Sprawdź numer LCSC albo użyj mniejszej liczby słów, np. "0603 100nF".',
      'col.lcsc': 'LCSC',
      'col.mpn': 'MPN',
      'col.type': 'Typ',
      'col.manufacturer': 'Producent',
      'col.package': 'Obudowa',
      'col.stock': 'Stan magazynowy',
      'col.description': 'Opis',
      'col.datasheet': 'Karta katalogowa',
      'col.action': 'Akcja',
      'stock.in': 'w magazynie',
      'stock.out': 'Brak w magazynie',
      'button.add': 'Dodaj do biblioteki',
      'button.adding': 'Dodawanie...',
      'button.added': 'Dodano',
      'button.addLabel': 'Dodaj {part} do biblioteki',
      'link.lcscLabel': 'Otwórz {id} w LCSC',
      'link.lcscTitle': 'Otwórz {id} w LCSC (otworzy się w przeglądarce)',
      'link.sheetText': 'Karta katalogowa',
      'link.sheetLabel': 'Otwórz kartę katalogową {id}',
      'link.sheetTitle': 'Otwórz kartę katalogową {id} (otworzy się w przeglądarce)',
      'added.heading': 'Zapisane biblioteki',
      'card.sentToFusion': 'Wysłano do Fusion',
      'card.saved': 'Zapisano',
      'card.badge3d': '3D',
      'card.noModelTitle': 'Brak modelu 3D.',
      'card.noModelText':
        'Zażądano modelu 3D, ale dla tej części nie powstał żaden, więc zapisano tylko symbol i footprint.',
      'card.noModelTextReason':
        'Zażądano modelu 3D, ale dla tej części nie powstał żaden, więc zapisano tylko symbol i footprint. Powód podano w ostrzeżeniach powyżej.',
      'card.howTo': 'Jak użyć biblioteki we Fusion',
      'card.openFolder': 'Otwórz folder',
      'button.addToFusion': 'Dodaj do biblioteki Fusion',
      'button.addToFusionLabel': 'Dodaj do biblioteki Fusion: {id}',
      'button.openSeparate': 'Otwórz jako osobną bibliotekę',
      'button.openSeparateTitle':
        'Otwiera tylko tę część jako osobną bibliotekę w edytorze Fusion Electronics',
      'button.sending': 'Wysyłanie...',
      'fusion.headIdle': 'Jeszcze nie ma jej w bibliotece Fusion.',
      'fusion.headSending': 'Wysyłanie do Fusion...',
      'fusion.detailSending': 'Fusion może nie odpowiadać nawet przez minutę.',
      'fusion.headFail': 'Nie dodano do biblioteki Fusion.',
      'fusion.headFailDetail': 'Nie dodano do biblioteki Fusion:',
      'fusion.headIn': 'W bibliotece {name}:',
      'fusion.headInDefault': 'W bibliotece Fusion:',
      'fusion.detailPresent': 'już tam jest, nie dodano ponownie.',
      'fusion.detailCreated': 'utworzono bibliotekę z tą częścią.',
      'fusion.detailMerged': 'dodano do biblioteki.',
      'fusion.detailSaved': 'zapisano.',
      'fusion.threeDStarted':
        'Otwarto pakiet 3D: sprawdź położenie modelu, a potem w Fusion kliknij Finish, Save, OK.',
      'fusion.threeDFailed': 'Błąd 3D: {reason}',
      'fusion.threeDSkipped': 'Pominięto 3D: {reason}',
      'fusion.seeWarnings': 'zobacz ostrzeżenia poniżej.',
      'fusion.noReport': 'Fusion nie odpowiedział. Spróbuj ponownie.',
      'model.summary': 'Dołącz model 3D ręcznie',
      'model.step1':
        'Otwórz bibliotekę w Fusion Electronics ("Otwórz jako osobną bibliotekę" powyżej) i zapisz ją w projekcie.',
      'model.step2':
        'W Content Manager otwórz kartę Footprints, kliknij footprint prawym przyciskiem myszy i wybierz "Attach Existing 3D Model".',
      'model.step3': 'Kliknij "Select from my computer..." i wskaż umieszczony plik STEP:',
      'model.step4': 'Nadaj nazwę pakietowi i naciśnij OK.',
      'model.step5':
        'Jeśli model jest źle ustawiony, edytuj pakiet, aby poprawić jego położenie. Potem użyj File > Save.',
      'model.placed': 'Umieszczony (użyj tego)',
      'model.raw': 'Surowy (jak pobrano)',
      'model.note':
        'Dane modelu: JLCEDA/EasyEDA Official Library, https://easyeda.com . Tylko do użytku osobistego.',
      'footer.text':
        'Dane: JLCEDA/EasyEDA Official Library, https://easyeda.com . Do użytku osobistego; odpowiadasz za przestrzeganie warunków EasyEDA i LCSC.',
      'bridge.lost': 'Utracono połączenie z Fusion. Jeśli Fusion jest zamknięty, zamknij to okno.',
      'bridge.stopped': 'LCSC2Fusion został zatrzymany we Fusion. Możesz zamknąć to okno.',
      'bridge.requestFailed':
        'Żądanie do Fusion nie powiodło się. Sprawdź, czy Fusion jest uruchomiony, i spróbuj ponownie.',
    },
  };

  // Messages from Python, see the header comment (3). MESSAGES:BEGIN
  const MESSAGES = [
    // The page itself (bridge and app), not Python.
    {
      re: /^The request to Fusion failed\. Check that Fusion is running, then try again\.$/,
      pl: '@bridge.requestFailed',
    },
    { re: /^Another request is still running\. Please wait for it\.$/, pl: '@status.busy' },
    { re: /^Fusion did not report back\. Try again\.$/, pl: '@fusion.noReport' },

    // controller.py: progress and outcome of a search or an add.
    {
      re: /^Ready\. Libraries are saved to (?<dir>.+)$/,
      pl: 'Gotowe. Biblioteki są zapisywane w {dir}',
    },
    { re: /^Searching for (?<query>.+)\.\.\.$/, pl: 'Wyszukiwanie {query}...' },
    {
      re: /^Downloading (?<id>\S+) and building the library and the 3D model\.\.\.$/,
      pl: 'Pobieranie {id} i budowanie biblioteki oraz modelu 3D...',
    },
    {
      re: /^Downloading (?<id>\S+) and building the library\.\.\.$/,
      pl: 'Pobieranie {id} i budowanie biblioteki...',
    },
    {
      re: /^Found (?<n>\d+) part\(s\) for (?<query>.+)\.$/,
      pl: {
        one: 'Znaleziono {n} część dla {query}.',
        few: 'Znaleziono {n} części dla {query}.',
        many: 'Znaleziono {n} części dla {query}.',
        other: 'Znaleziono {n} części dla {query}.',
      },
    },
    {
      re: /^No parts found for (?<query>.+)\. Try an LCSC number such as C2040 or fewer words\.$/,
      pl: 'Nie znaleziono części dla {query}. Spróbuj podać numer LCSC, np. C2040, albo użyj mniejszej liczby słów.',
    },
    {
      re: /^Saved (?<label>C\d+(?: \(.+?\))?) without a 3D model: none was produced for this part\. See the warnings on the result\.$/,
      pl: 'Zapisano {label} bez modelu 3D: dla tej części nie powstał żaden. Zobacz ostrzeżenia przy wyniku.',
    },
    {
      re: /^Saved (?<label>C\d+(?: \(.+?\))?) with (?<n>\d+) warning\(s\)\.$/,
      pl: {
        one: 'Zapisano {label} z {n} ostrzeżeniem.',
        few: 'Zapisano {label} z {n} ostrzeżeniami.',
        many: 'Zapisano {label} z {n} ostrzeżeniami.',
        other: 'Zapisano {label} z {n} ostrzeżeniami.',
      },
    },
    {
      re: /^Saved (?<label>C\d+(?: \(.+?\))?) and its 3D model to (?<dir>.+)$/,
      pl: 'Zapisano {label} wraz z modelem 3D do folderu {dir}',
    },
    {
      re: /^Saved (?<label>C\d+(?: \(.+?\))?) to (?<path>.+)$/,
      pl: 'Zapisano {label} do pliku {path}',
    },

    // controller.py: the hand-off to the Fusion library cannot start or fails.
    {
      re: /^Another Fusion request is still running\.$/,
      pl: 'Inne żądanie do Fusion jest jeszcze w toku.',
    },
    {
      re: /^(?<id>\S+) was not added in this session\. Press Add to library for it first\.$/,
      pl: 'Część {id} nie została dodana w tej sesji. Najpierw naciśnij "Dodaj do biblioteki" przy tej części.',
    },
    {
      re: /^Refusing to use a file outside the library output directory\.$/,
      pl: 'Odmowa użycia pliku spoza katalogu wyjściowego bibliotek.',
    },
    {
      re: /^Could not start the Fusion request\. See the Text Commands window\.$/,
      pl: 'Nie udało się uruchomić żądania do Fusion. Zajrzyj do okna Text Commands.',
    },
    { re: /^Sending (?<id>\S+) to Fusion\.\.\.$/, pl: 'Wysyłanie {id} do Fusion...' },
    {
      re: /^Adding to the Fusion library is not available in this session\. The library file was saved; use "Open as a separate library" instead\.$/,
      pl: 'Dodawanie do biblioteki Fusion jest niedostępne w tej sesji. Plik biblioteki został zapisany; użyj zamiast tego "Otwórz jako osobną bibliotekę".',
    },
    {
      re: /^Unexpected error\. The details were written to the Fusion Text Commands window\.$/,
      pl: 'Nieoczekiwany błąd. Szczegóły zapisano w oknie Text Commands we Fusion.',
    },

    // controller.py: a request that cannot be served.
    { re: /^Unknown palette action (?<action>.+)\.$/, pl: 'Nieznana akcja okna: {action}.' },
    { re: /^No folder given\.$/, pl: 'Nie podano folderu.' },
    {
      re: /^Refusing to open a folder outside the library output directory\.$/,
      pl: 'Odmowa otwarcia folderu spoza katalogu wyjściowego bibliotek.',
    },
    { re: /^Folder does not exist: (?<path>.+)$/, pl: 'Folder nie istnieje: {path}' },
    {
      re: /^Could not open the folder: (?<exc>.+)$/s,
      pl: 'Nie udało się otworzyć folderu: {exc}',
    },
    { re: /^No library file given\.$/, pl: 'Nie podano pliku biblioteki.' },
    {
      re: /^Refusing to open a file outside the library output directory\.$/,
      pl: 'Odmowa otwarcia pliku spoza katalogu wyjściowego bibliotek.',
    },
    {
      re: /^Library file does not exist: (?<path>.+)$/,
      pl: 'Plik biblioteki nie istnieje: {path}',
    },
    {
      re: /^Malformed request from the palette \(invalid JSON\)\.$/,
      pl: 'Nieprawidłowe żądanie z okna (niepoprawny JSON).',
    },
    {
      re: /^Malformed request from the palette \(expected a JSON object\)\.$/,
      pl: 'Nieprawidłowe żądanie z okna (oczekiwano obiektu JSON).',
    },
    {
      re: /^Enter an LCSC part number \(for example C2040\) or a search phrase\.$/,
      pl: 'Wpisz numer części LCSC (np. C2040) lub frazę wyszukiwania.',
    },
    {
      re: /^The search text is too long \(limit (?<n>\d+) characters\)\.$/,
      pl: {
        one: 'Tekst wyszukiwania jest za długi (limit: {n} znak).',
        few: 'Tekst wyszukiwania jest za długi (limit: {n} znaki).',
        many: 'Tekst wyszukiwania jest za długi (limit: {n} znaków).',
        other: 'Tekst wyszukiwania jest za długi (limit: {n} znaków).',
      },
    },
    {
      re: /^The search text contains unsupported characters\.$/,
      pl: 'Tekst wyszukiwania zawiera nieobsługiwane znaki.',
    },
    {
      re: /^Invalid request: (?<key>\w+) must be true or false\.$/,
      pl: 'Nieprawidłowe żądanie: {key} musi mieć wartość true albo false.',
    },
    {
      re: /^(?<value>.+) is not a valid LCSC part number\. Use the letter C followed by digits, for example C2040\.$/,
      pl: '{value} nie jest poprawnym numerem części LCSC. Użyj litery C i cyfr, np. C2040.',
    },
    { re: /^File system error: (?<exc>.+)$/s, pl: 'Błąd systemu plików: {exc}' },

    // controller.py: what to do in Fusion (the saved card and the open library status).
    {
      re: /^File > Open > Open from my computer, select the \.lbr file, then File > Save into a project so it appears in the Library Manager\.$/,
      pl: 'wybierz File > Open > Open from my computer, wskaż plik .lbr, a potem zapisz go w projekcie przez File > Save, aby pojawił się w Library Manager.',
    },
    {
      re: /^Press "Add to Fusion library" to merge the part into your LCSC2Fusion Library\. To use just this part on its own, press "Open as a separate library", then use File > Save in the editor and pick a project so the library appears in the Library Manager\. If that does not work, do it manually: (?<steps>File > Open > .+)$/,
      pl: 'Naciśnij "Dodaj do biblioteki Fusion", aby dołączyć część do biblioteki LCSC2Fusion Library. Aby użyć tylko tej części osobno, naciśnij "Otwórz jako osobną bibliotekę", a potem w edytorze użyj File > Save i wybierz projekt, aby biblioteka pojawiła się w Library Manager. Jeśli to nie zadziała, zrób to ręcznie: {steps:msg}',
    },
    {
      re: /^Press "Open as a separate library", then use File > Save in the editor and pick a project so the library appears in the Library Manager\. If that does not work, do it manually: (?<steps>File > Open > .+)$/,
      pl: 'Naciśnij "Otwórz jako osobną bibliotekę", a potem w edytorze użyj File > Save i wybierz projekt, aby biblioteka pojawiła się w Library Manager. Jeśli to nie zadziała, zrób to ręcznie: {steps:msg}',
    },
    {
      re: /^Sent (?<name>.+) to Fusion for opening\. In the editor use File > Save and pick a project so it appears in the Library Manager\.$/,
      pl: 'Wysłano {name} do Fusion w celu otwarcia. W edytorze użyj File > Save i wybierz projekt, aby biblioteka pojawiła się w Library Manager.',
    },
    {
      re: /^Fusion did not open (?<name>.+?) \((?<reason>.+)\)\. Open it manually: (?<steps>File > Open > .+)$/s,
      pl: 'Fusion nie otworzył {name} ({reason:msg}). Otwórz go ręcznie: {steps:msg}',
    },
    {
      re: /^Fusion did not open (?<name>.+)\. Open it manually: (?<steps>File > Open > .+)$/,
      pl: 'Fusion nie otworzył {name}. Otwórz go ręcznie: {steps:msg}',
    },

    // fusion_adapter.py: the reasons of an open request that failed (FusionImporter).
    {
      re: /^The library path must be absolute: (?<path>.+)$/,
      pl: 'Ścieżka biblioteki musi być bezwzględna: {path}',
    },
    {
      re: /^The library path contains a double quote: (?<path>.+)$/,
      pl: 'Ścieżka biblioteki zawiera cudzysłów: {path}',
    },
    {
      re: /^The library path contains a single quote: (?<path>.+)$/,
      pl: 'Ścieżka biblioteki zawiera apostrof: {path}',
    },
    {
      re: /^Library file not found: (?<path>.+)$/,
      pl: 'Nie znaleziono pliku biblioteki: {path}',
    },
    {
      re: /^Fusion rejected the open command \((?<exc>.+)\)$/s,
      pl: 'Fusion odrzucił polecenie otwarcia ({exc})',
    },
    {
      re: /^Open command sent to Fusion\.$/,
      pl: 'Polecenie otwarcia zostało wysłane do Fusion.',
    },

    // fusion_library_flow.py: the library flow, in the order it runs.
    {
      re: /^Another library operation is still running\. Wait for it to finish\.$/,
      pl: 'Inna operacja na bibliotece jest jeszcze w toku. Poczekaj, aż się zakończy.',
    },
    {
      re: /^Another library operation is still running\. Wait for it\.$/,
      pl: 'Inna operacja na bibliotece jest jeszcze w toku. Poczekaj na jej zakończenie.',
    },
    {
      re: /^Unexpected error while adding (?<label>.+?): (?<exc>.+)$/s,
      pl: 'Nieoczekiwany błąd podczas dodawania {label}: {exc}',
    },
    {
      re: /^The part library file does not exist: (?<path>.+)$/,
      pl: 'Plik biblioteki części nie istnieje: {path}',
    },
    {
      re: /^The 3D model file does not exist \((?<path>.+)\), so the 3D step is skipped\.$/,
      pl: 'Plik modelu 3D nie istnieje ({path}), więc krok 3D został pominięty.',
    },
    {
      re: /^The LCSC2Fusion Library from the registry no longer exists \(deleted or moved\?\)\. A new one is created; parts added earlier stay in the old document\.$/,
      pl: 'Biblioteka LCSC2Fusion Library zapisana w rejestrze już nie istnieje (usunięta lub przeniesiona?). Zostanie utworzona nowa; części dodane wcześniej zostają w starym dokumencie.',
    },
    {
      re: /^The footprint (?<name>.+) is already in the LCSC2Fusion Library with a different shape, so merging would overwrite it\. Add the part again so that it is converted with a new footprint name\.$/,
      pl: 'Footprint {name} jest już w bibliotece LCSC2Fusion Library, ale ma inny kształt, więc dołączenie części nadpisałoby go. Dodaj część jeszcze raz, aby została przekonwertowana z nową nazwą footprintu.',
    },
    {
      re: /^The device set (?<name>.+) is already in the library \(part (?<other>.+)\)\. Merging replaces it with this part\.$/,
      pl: 'Device set {name} jest już w bibliotece (część {other}). Dołączenie zastąpi istniejący wpis tą częścią.',
    },
    {
      re: /^Creating the LCSC2Fusion Library from (?<label>.+)\.\.\.$/,
      pl: 'Tworzenie biblioteki LCSC2Fusion Library z części {label}...',
    },
    { re: /^Opening the (?<name>.+)\.\.\.$/, pl: 'Otwieranie biblioteki {name}...' },
    {
      re: /^Merging (?<label>.+) into the library\.\.\.$/,
      pl: 'Dołączanie części {label} do biblioteki...',
    },
    { re: /^Saving the library\.\.\.$/, pl: 'Zapisywanie biblioteki...' },
    {
      re: /^Creating the 3D package for (?<label>.+)\.\.\.$/,
      pl: 'Tworzenie pakietu 3D dla {label}...',
    },
    { re: /^Importing the 3D model\.\.\.$/, pl: 'Importowanie modelu 3D...' },

    // fusion_library_flow.py: the result lines. The two 3D sentences are glued to an action
    // sentence, so their entries come first (the action entries would take the whole text).
    {
      re: /^(?<head>.+?) The 3D package editor is open: check the position of the model, then click Finish, Save, OK\.$/,
      pl: '{head:msg} Edytor pakietu 3D jest otwarty: sprawdź położenie modelu, a potem kliknij Finish, Save, OK.',
    },
    {
      re: /^(?<head>.+?) The 3D package could not be created, see the warnings\.$/,
      pl: '{head:msg} Nie udało się utworzyć pakietu 3D, zobacz ostrzeżenia.',
    },
    {
      re: /^(?<label>.+) is already in the LCSC2Fusion Library, so it was not added again\.$/,
      pl: 'Część {label} jest już w bibliotece LCSC2Fusion Library, więc nie została dodana ponownie.',
    },
    {
      re: /^Created the LCSC2Fusion Library with (?<label>C\d+(?: \(.+?\))?)\.$/,
      pl: 'Utworzono bibliotekę LCSC2Fusion Library z częścią {label}.',
    },
    {
      re: /^Added (?<label>.+) to the LCSC2Fusion Library\.$/,
      pl: 'Dodano część {label} do biblioteki LCSC2Fusion Library.',
    },
    {
      re: /^(?<label>.+) was merged into the open library but saving it failed \((?<exc>.+)\)\. Save the library yourself \(File > Save\)\. The part is not recorded, so adding it again merges it again\.$/s,
      pl: 'Część {label} została dołączona do otwartej biblioteki, ale jej zapisanie się nie powiodło ({exc:msg}). Zapisz bibliotekę samodzielnie (File > Save). Część nie jest zapisana w rejestrze, więc ponowne dodanie dołączy ją jeszcze raz.',
    },
    {
      re: /^The part was added, but the registry (?<path>.+) could not be written \((?<exc>.+)\)\. The part will be merged again if you add it again\.$/s,
      pl: 'Część została dodana, ale nie udało się zapisać rejestru {path} ({exc}). Jeśli dodasz ją ponownie, zostanie dołączona jeszcze raz.',
    },

    // fusion_library_flow.py: a Fusion step failed. A reason that ends with a period gives two
    // periods in Python, so each message has an entry for a reason with its period (it is
    // translated as a message) and one for a reason without it.
    {
      re: /^Could not look for the library: (?<exc>.+\.)\.$/s,
      pl: 'Nie udało się wyszukać biblioteki: {exc:msg}',
    },
    {
      re: /^Could not look for the library: (?<exc>.*[^.])\.$/s,
      pl: 'Nie udało się wyszukać biblioteki: {exc:msg}.',
    },
    {
      re: /^Could not create the library: (?<exc>.+\.)\.$/s,
      pl: 'Nie udało się utworzyć biblioteki: {exc:msg}',
    },
    {
      re: /^Could not create the library: (?<exc>.*[^.])\.$/s,
      pl: 'Nie udało się utworzyć biblioteki: {exc:msg}.',
    },
    {
      re: /^Could not open the library: (?<exc>.+\.)\.$/s,
      pl: 'Nie udało się otworzyć biblioteki: {exc:msg}',
    },
    {
      re: /^Could not open the library: (?<exc>.*[^.])\.$/s,
      pl: 'Nie udało się otworzyć biblioteki: {exc:msg}.',
    },
    {
      re: /^Could not merge the part: (?<exc>.+\.)\. The library may be open with unsaved changes: close it without saving before you try again\.$/s,
      pl: 'Nie udało się dołączyć części do biblioteki: {exc:msg} Biblioteka może być otwarta z niezapisanymi zmianami: zamknij ją bez zapisywania, zanim spróbujesz ponownie.',
    },
    {
      re: /^Could not merge the part: (?<exc>.*[^.])\. The library may be open with unsaved changes: close it without saving before you try again\.$/s,
      pl: 'Nie udało się dołączyć części do biblioteki: {exc:msg}. Biblioteka może być otwarta z niezapisanymi zmianami: zamknij ją bez zapisywania, zanim spróbujesz ponownie.',
    },

    // fusion_library_flow.py: a warning from the 3D step (the page drops the leading "3D: ").
    {
      re: /^3D: could not open the library: (?<exc>.+\.)\.$/s,
      pl: '3D: nie udało się otworzyć biblioteki: {exc:msg}',
    },
    {
      re: /^3D: could not open the library: (?<exc>.*[^.])\.$/s,
      pl: '3D: nie udało się otworzyć biblioteki: {exc:msg}.',
    },
    {
      re: /^3D: could not create the 3D package: (?<exc>.+\.)\.$/s,
      pl: '3D: nie udało się utworzyć pakietu 3D: {exc:msg}',
    },
    {
      re: /^3D: could not create the 3D package: (?<exc>.*[^.])\.$/s,
      pl: '3D: nie udało się utworzyć pakietu 3D: {exc:msg}.',
    },
    {
      re: /^3D: could not import (?<file>.+?): (?<exc>.+\.)\. You can import (?<path>.+) by hand\.$/s,
      pl: '3D: nie udało się zaimportować {file}: {exc:msg} Możesz zaimportować {path} ręcznie.',
    },
    {
      re: /^3D: could not import (?<file>.+?): (?<exc>.*[^.])\. You can import (?<path>.+) by hand\.$/s,
      pl: '3D: nie udało się zaimportować {file}: {exc:msg}. Możesz zaimportować {path} ręcznie.',
    },

    // library_registry.py: a registry file that cannot be used, and a part file that is not valid.
    {
      re: /^The library registry (?<path>.+) cannot be read \((?<exc>.+)\); starting with an empty registry\.$/s,
      pl: 'Rejestr biblioteki {path} nie może zostać odczytany ({exc}); zaczynam od pustego rejestru.',
    },
    {
      re: /^The library registry (?<path>.+) is not valid JSON \((?<exc>.+)\); starting with an empty registry\.$/s,
      pl: 'Rejestr biblioteki {path} nie jest poprawnym plikiem JSON ({exc}); zaczynam od pustego rejestru.',
    },
    {
      re: /^The library registry (?<path>.+) does not contain a JSON object; starting with an empty registry\.$/,
      pl: 'Rejestr biblioteki {path} nie zawiera obiektu JSON; zaczynam od pustego rejestru.',
    },
    {
      re: /^The library registry (?<path>.+) has version (?<version>.+), this add-in understands (?<supported>\d+); starting with an empty registry\.$/,
      pl: 'Rejestr biblioteki {path} ma wersję {version}, a ten dodatek obsługuje wersję {supported}; zaczynam od pustego rejestru.',
    },
    {
      re: /^The library registry (?<path>.+) has an unexpected structure; starting with an empty registry\.$/,
      pl: 'Rejestr biblioteki {path} ma nieoczekiwaną strukturę; zaczynam od pustego rejestru.',
    },
    {
      re: /^(?<path>.+): dropped the malformed record for (?<key>.+)$/,
      pl: '{path}: pominięto uszkodzony wpis dla {key}',
    },
    {
      re: /^(?<path>.+): dropped the malformed package entry (?<key>.+)$/,
      pl: '{path}: pominięto uszkodzony wpis pakietu {key}',
    },
    {
      re: /^The part library (?<name>.+) cannot be read: (?<exc>.+)$/s,
      pl: 'Nie można odczytać biblioteki części {name}: {exc}',
    },
    {
      re: /^(?<name>.+) is not an Eagle library \(no <library> element\)$/,
      pl: '{name} nie jest biblioteką Eagle (brak elementu <library>)',
    },
    {
      re: /^(?<name>.+) has no named <deviceset>$/,
      pl: '{name} nie ma nazwanego elementu <deviceset>',
    },
    {
      re: /^(?<name>.+) has a <package> without a name$/,
      pl: '{name} zawiera element <package> bez nazwy',
    },

    // fusion_adapter.py: what a Fusion operation reports (LibraryApiError and the text command
    // checks). They arrive inside the "could not" sentences above.
    {
      re: /^Fusion rejected the rotation matrix for the 3D model\.$/,
      pl: 'Fusion odrzucił macierz obrotu modelu 3D.',
    },
    {
      re: /^Fusion has no project to keep the library in\. Sign in to a hub\.$/,
      pl: 'Fusion nie ma projektu, w którym można przechowywać bibliotekę. Zaloguj się do huba.',
    },
    {
      re: /^Fusion did not return the root folder of the project\.$/,
      pl: 'Fusion nie zwrócił folderu głównego projektu.',
    },
    {
      re: /^Fusion did not open the part library as a new document\.$/,
      pl: 'Fusion nie otworzył biblioteki części jako nowego dokumentu.',
    },
    {
      re: /^The part library did not open as a new document: the active document (?<name>.+) is already saved\.$/,
      pl: 'Biblioteka części nie otworzyła się jako nowy dokument: aktywny dokument {name} jest już zapisany.',
    },
    {
      re: /^Fusion could not save the new library as (?<name>.+)\.$/,
      pl: 'Fusion nie mógł zapisać nowej biblioteki jako {name}.',
    },
    {
      re: /^Fusion saved (?<name>.+) but did not report its file\. The next part finds the library by its name\.$/,
      pl: 'Fusion zapisał {name}, ale nie zwrócił pliku tej biblioteki. Następna część znajdzie bibliotekę po nazwie.',
    },
    {
      re: /^Fusion cannot find the library (?<name>.+) any more\.$/,
      pl: 'Fusion nie może już znaleźć biblioteki {name}.',
    },
    {
      re: /^Fusion could not open the library (?<name>.+)\.$/,
      pl: 'Fusion nie mógł otworzyć biblioteki {name}.',
    },
    {
      re: /^Fusion could not activate the library (?<name>.+)\.$/,
      pl: 'Fusion nie mógł aktywować biblioteki {name}.',
    },
    {
      re: /^The active document (?<name>.+) is not the library, so nothing was merged\.$/,
      pl: 'Aktywny dokument {name} nie jest biblioteką, więc nic nie zostało dołączone.',
    },
    {
      re: /^There is no active document to save\.$/,
      pl: 'Brak aktywnego dokumentu do zapisania.',
    },
    {
      re: /^The 3D package editor did not open\.$/,
      pl: 'Edytor pakietu 3D się nie otworzył.',
    },
    {
      re: /^Fusion could not save the new 3D package design\.$/,
      pl: 'Fusion nie mógł zapisać nowego projektu pakietu 3D.',
    },
    {
      re: /^Fusion could not save (?<name>.+)\.$/,
      pl: 'Fusion nie mógł zapisać {name}.',
    },
    {
      re: /^The STEP file does not exist: (?<path>.+)$/,
      pl: 'Plik STEP nie istnieje: {path}',
    },
    {
      re: /^The active document is not a design, so the STEP file cannot be imported\.$/,
      pl: 'Aktywny dokument nie jest projektem (design), więc nie można zaimportować pliku STEP.',
    },
    {
      re: /^Fusion imported nothing from (?<name>.+)\.$/,
      pl: 'Fusion nic nie zaimportował z {name}.',
    },
    {
      re: /^The imported STEP file did not produce an occurrence\.$/,
      pl: 'Zaimportowany plik STEP nie utworzył wystąpienia (occurrence).',
    },
    {
      re: /^The device set name (?<value>.+) is empty or has unprintable characters\.$/,
      pl: 'Nazwa device set {value} jest pusta albo zawiera znaki niedrukowalne.',
    },
    {
      re: /^The library file name (?<value>.+) is empty or has unprintable characters\.$/,
      pl: 'Nazwa pliku biblioteki {value} jest pusta albo zawiera znaki niedrukowalne.',
    },
    {
      re: /^The device set name (?<value>.+) contains (?<char>.+), which a text command cannot carry\.$/,
      pl: 'Nazwa device set {value} zawiera znak {char}, którego polecenie tekstowe nie obsługuje.',
    },
    {
      re: /^The library file name (?<value>.+) contains (?<char>.+), which a text command cannot carry\.$/,
      pl: 'Nazwa pliku biblioteki {value} zawiera znak {char}, którego polecenie tekstowe nie obsługuje.',
    },

    // service.py: the 3D files of one part (the id comes first), shown in the warnings.
    {
      re: /^(?<label>.+?): no 3D model available$/,
      pl: '{label}: brak dostępnego modelu 3D',
    },
    {
      re: /^(?<label>.+?): 3D model not written: (?<value>.+) is not an LCSC part number$/,
      pl: '{label}: nie zapisano modelu 3D: {value} nie jest numerem części LCSC',
    },
    {
      re: /^(?<label>.+?): 3D model not written: (?<exc>.+)$/s,
      pl: '{label}: nie zapisano modelu 3D: {exc:msg}',
    },

    // client: lookup of a part and of its 3D model on EasyEDA.
    {
      re: /^(?<value>.+) is not a valid LCSC part number\. Expected the letter C followed by digits, for example C2040\.$/,
      pl: '{value} nie jest poprawnym numerem części LCSC. Użyj litery C i cyfr, np. C2040.',
    },
    {
      re: /^EasyEDA has no data for (?<id>\S+) \(HTTP 404\)\.$/,
      pl: 'EasyEDA nie ma danych dla {id} (HTTP 404).',
    },
    {
      re: /^EasyEDA has no symbol\/footprint data for (?<id>\S+)\. The part may not exist or may not have EDA data\.$/,
      pl: 'EasyEDA nie ma danych symbolu ani footprintu dla {id}. Część może nie istnieć albo nie mieć danych EDA.',
    },
    {
      re: /^EasyEDA returned JSON that is not an object\.$/,
      pl: 'EasyEDA zwrócił dane JSON, które nie są obiektem.',
    },
    {
      re: /^3D model (?<id>\S+) not found on EasyEDA \(HTTP 404\)\.$/,
      pl: 'Model 3D {id} nie został znaleziony w EasyEDA (HTTP 404).',
    },
    {
      re: /^EasyEDA returned an unusable 3D model (?<id>\S+): (?<exc>.+)$/s,
      pl: 'EasyEDA zwrócił nieużyteczny model 3D {id}: {exc:msg}',
    },
    {
      re: /^(?<value>.+) is not a valid 3D model id\. Expected 32 lowercase hexadecimal digits\.$/,
      pl: '{value} nie jest poprawnym identyfikatorem modelu 3D. Oczekiwano 32 małych cyfr szesnastkowych.',
    },

    // client: part search on jlcpcb.com.
    {
      re: /^jlcpcb\.com returned JSON that is not an object\.$/,
      pl: 'jlcpcb.com zwrócił dane JSON, które nie są obiektem.',
    },
    {
      re: /^jlcpcb\.com search response has no data \(code (?<code>.+), message (?<message>.+)\)\.$/s,
      pl: 'Odpowiedź wyszukiwarki jlcpcb.com nie zawiera danych (kod {code}, komunikat {message}).',
    },
    {
      re: /^jlcpcb\.com search response has an unexpected componentPageInfo\.$/,
      pl: 'Odpowiedź wyszukiwarki jlcpcb.com zawiera nieoczekiwany element componentPageInfo.',
    },
    {
      re: /^jlcpcb\.com search response has an unexpected result list\.$/,
      pl: 'Odpowiedź wyszukiwarki jlcpcb.com zawiera nieoczekiwaną listę wyników.',
    },

    // client: the network (a block, a timeout, TLS, a bad answer).
    {
      re: /^(?<host>\S+) blocked the request \(HTTP (?<status>\d+)\)\. Such blocks usually clear on their own; try again in about (?<seconds>\d+) s\.$/,
      pl: '{host} zablokował żądanie (HTTP {status}). Takie blokady zwykle ustępują same; spróbuj ponownie za około {seconds} s.',
    },
    {
      re: /^Requests to (?<host>\S+) are paused after it blocked us\. Try again in about (?<seconds>\d+) s\.$/,
      pl: 'Żądania do {host} są wstrzymane, bo serwer nas zablokował. Spróbuj ponownie za około {seconds} s.',
    },
    {
      re: /^(?<host>\S+) returned unexpected HTTP status (?<status>\d+)\.$/,
      pl: '{host} zwrócił nieoczekiwany status HTTP {status}.',
    },
    {
      re: /^(?<host>\S+) returned HTTP (?<status>\d+)$/,
      pl: '{host} zwrócił kod HTTP {status}',
    },
    {
      re: /^(?<host>\S+) returned a response that is not valid JSON: (?<exc>.+)$/s,
      pl: '{host} zwrócił odpowiedź, która nie jest poprawnym JSON: {exc}',
    },
    {
      re: /^(?<host>\S+) sent a corrupt gzip response: (?<exc>.+)$/s,
      pl: '{host} wysłał uszkodzoną odpowiedź gzip: {exc}',
    },
    {
      re: /^Request to (?<host>\S+) timed out after (?<seconds>\S+) s$/,
      pl: 'Upłynął limit czasu żądania do {host} po {seconds} s',
    },
    {
      re: /^TLS error while talking to (?<host>\S+): (?<exc>.+)$/s,
      pl: 'Błąd TLS podczas komunikacji z {host}: {exc}',
    },
    {
      re: /^Cannot reach (?<host>\S+): (?<exc>.+)$/s,
      pl: 'Nie można połączyć się z {host}: {exc}',
    },
    {
      re: /^Connection to (?<host>\S+) failed: (?<exc>.+)$/s,
      pl: 'Połączenie z {host} nie powiodło się: {exc}',
    },
  ];
  // MESSAGES:END

  // The language in use. theme-init.js has already decided (saved choice, else the browser).
  const prefs = window.LcscPrefs;
  let current = prefs && DICTIONARIES[prefs.getLanguage()] ? prefs.getLanguage() : FALLBACK;

  function lookup(key, language) {
    const dictionary = DICTIONARIES[language];
    return dictionary && Object.prototype.hasOwnProperty.call(dictionary, key) ? dictionary[key] : undefined;
  }

  function pluralCategory(language, n) {
    try {
      return new Intl.PluralRules(language).select(n);
    } catch (err) {
      return 'other';
    }
  }

  // Fills {name} in a string, or picks the plural form of an object first. `translateGroup` is set
  // for message templates, where {name:msg} translates the group text as a message.
  function fill(template, params, language, translateGroup) {
    let text = template;
    if (template !== null && typeof template === 'object') {
      const n = Number(params && params.n);
      const form = Number.isFinite(n) ? template[pluralCategory(language, n)] : undefined;
      text = form !== undefined ? form : template.other;
    }
    return String(text).replace(/\{(\w+)(:msg)?\}/g, (whole, name, modifier) => {
      if (!params || params[name] === undefined || params[name] === null) return whole;
      const value = String(params[name]);
      return modifier && translateGroup ? translateGroup(value) : modifier ? whole : value;
    });
  }

  function t(key, params, language) {
    const code = language || current;
    let value = lookup(key, code);
    if (value === undefined) value = lookup(key, FALLBACK);
    return value === undefined ? key : fill(value, params, code, null);
  }

  // Index of the first MESSAGES entry that matches `text`, with the captured groups; else null.
  function findMessage(text) {
    const subject = String(text).trim();
    for (let index = 0; index < MESSAGES.length; index += 1) {
      const found = MESSAGES[index].re.exec(subject);
      if (found) return { index: index, groups: found.groups || {} };
    }
    return null;
  }

  function translateMessage(text, language) {
    const code = language || current;
    const original = text === undefined || text === null ? '' : String(text);
    if (code === FALLBACK) return original;
    const found = findMessage(original);
    if (!found) return original;
    const template = MESSAGES[found.index][code];
    if (template === undefined) return original;
    if (typeof template === 'string' && template.charAt(0) === '@') {
      return t(template.slice(1), found.groups, code);
    }
    return fill(template, found.groups, code, (group) => translateMessage(group, code));
  }

  function getLanguage() {
    return current;
  }

  function languages() {
    return Object.keys(DICTIONARIES);
  }

  // Switches the language of t() and translateMessage(). The caller re-renders (apply, app.js).
  function setLanguage(code) {
    if (!DICTIONARIES[code]) return false;
    current = code;
    if (typeof document !== 'undefined' && document.documentElement) {
      document.documentElement.setAttribute('lang', code);
    }
    return true;
  }

  // Markup hooks -------------------------------------------------------------------------------

  function readParams(node) {
    const raw = node.getAttribute('data-i18n-params');
    if (!raw) return undefined;
    try {
      return JSON.parse(raw);
    } catch (err) {
      return undefined;
    }
  }

  function applyNode(node) {
    const params = readParams(node);
    const key = node.getAttribute('data-i18n');
    if (key) node.textContent = t(key, params);
    const raw = node.getAttribute('data-msg');
    if (raw !== null) node.textContent = translateMessage(raw);
    const attributes = node.getAttribute('data-i18n-attr');
    if (attributes) {
      attributes.split(';').forEach((pair) => {
        const colon = pair.indexOf(':');
        if (colon < 1) return;
        node.setAttribute(pair.slice(0, colon).trim(), t(pair.slice(colon + 1).trim(), params));
      });
    }
  }

  // Translates `root` (default: the document) and everything below it.
  function apply(root) {
    const scope = root || document;
    if (scope.nodeType === 1) applyNode(scope);
    scope.querySelectorAll('[data-i18n], [data-msg], [data-i18n-attr]').forEach(applyNode);
  }

  Object.defineProperty(window, 'LcscI18n', {
    value: Object.freeze({
      FALLBACK: FALLBACK,
      DICTIONARIES: DICTIONARIES,
      MESSAGES: MESSAGES,
      t: t,
      translateMessage: translateMessage,
      findMessage: findMessage,
      getLanguage: getLanguage,
      setLanguage: setLanguage,
      languages: languages,
      apply: apply,
    }),
  });
})();
