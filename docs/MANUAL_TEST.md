# Test ręczny we Fusion Electronics

Fusion nie da się uruchomić z poziomu repo, więc to są jedyne kroki, które robisz sam. Zacznij od
Bramy 0. Dopóki ona nie przejdzie, reszta nie ma sensu. Przy każdym błędzie wklej do rozmowy
dokładny komunikat i zawartość okna Text Commands (wciśnij `S`, wpisz "Text Commands").

Przygotowanie (raz):

```
.venv\Scripts\activate
python scripts/make_manual_test_assets.py     # docs/manual_test/synthetic_all.lbr + out/manual_test/*.png
python scripts/build_addin.py                 # dist/LCSC2Fusion i dist/LCSC2Fusion.zip
```

## Brama 0: czy Fusion przyjmuje nasz .lbr (5 minut)

Status: PRZESZŁA (2026-10-12). Fusion otworzył `gate0_resistor.lbr`, w Content Managerze są device
`0603WAF1002T5E` (2 piny), pakiet `0603` (2 pady z automatyczną maską i pastą) oraz symbol z dwoma
pinami. Nasz starszy format warstw (`color`, `fill`) i nagłówek 9.6.2 są akceptowane.

Plik: `docs/gate0_resistor.lbr` (jeden rezystor 0603 zbudowany przez nasz writer).

- [ ] File > Open > Open from my computer, wskaż plik. Alternatywa: Data Panel > Upload.
- [ ] Otwiera się edytor biblioteki bez okna z błędem lub ostrzeżeniem.
- [ ] Widać device `0603WAF1002T5E` z jednym symbolem (prostokąt, 2 piny) i pakietem `0603` (2 pady).
- [ ] File > Save do projektu. Biblioteka pojawia się w Library Manager (Place Component).
- [ ] Umieść część w schemacie. Pojawia się `R1`, wartość edytowalna.

Dodatkowo, żeby wykluczyć ryzyko z badań (nowsze buildy mogą zapisywać inne atrybuty warstw):
wyeksportuj dowolną małą bibliotekę z Fusiona do .lbr i wklej mi plik albo jego pierwsze 60 linii.

## Brama 1: reguły konwersji, które są tylko wywnioskowane (15 minut)

Plik: `docs/manual_test/synthetic_all.lbr` (18 części syntetycznych). Rendery do porównania leżą w
`out/manual_test/<id>_footprint.png` i `<id>_symbol_<n>.png`. Importuj jak w Bramie 0.

- [ ] Import bez błędów, w bibliotece jest 18 deviceset (SYN-...).
- [ ] `C9000002` (kondensator): cztery łuki silkscreen w rogach wygięte w tę samą stronę co w renderze.
- [ ] `C9000015` (padstress): pady obrócone o 30, 45 i 15 stopni leżą tak samo jak w renderze
  (kierunek obrotu, nie tylko położenie środka).
- [ ] `C9000009` (USB-C): cztery sloty przelotowe, pady wielokątne jako prostokąty, dwa otwory
  niemetalizowane. W `synthetic_all.lbr` sloty są natywne (`shape="slot"`, nagłówek 9.7.0, tak jak
  zapisuje je sam Fusion). Zaimportuj osobno `slots_native.lbr` i `slots_milling.lbr` (ta sama
  część, sloty jako otwór plus frezowanie na warstwie 46) i napisz, które się otwierają oraz czy
  długość slotu zgadza się z renderem (`slotLength` to długość całkowita).
- [ ] `C9000010` (moduł): cztery pady dla pinu 25 (`25`, `25_2`, `25_3`, `25_4`). W edytorze device
  w oknie Connect pin 25 ma przypisane wszystkie cztery.
- [ ] `C9000014` (wzmacniacz): trzy gate'y (dwa wzmacniacze i zasilanie), każdy z własnym symbolem.
- [ ] `C9000006` (QFN z padem termicznym): duży pad centralny jest przypisany do pinu 17.
- [ ] `C9000005` (SOIC-8): pady owalne wyglądają jak w renderze (zaokrąglone końce).
- [ ] `C9000016`, `C9000017`, `C9000018` (pudełka 3D): sam footprint jak w renderze (modele 3D
  sprawdza Brama 3).
- [ ] `C9000008` (listwa): pady przelotowe, pierwszy kwadratowy, reszta okrągłe.
- [ ] Piny symboli: końce połączeniowe są na zewnątrz symbolu, nazwy i numery czytelne, kropki
  inwersji i znaczniki zegara tam, gdzie render je pokazuje (`C9000005`).
- [ ] Odnotuj, czy Fusion zgłasza ostrzeżenia ERC "off grid" dla pinów (D-016) i dla których części.

## Brama 3: modele 3D

Status: PRZESZŁA (2026-10-12). `<id>.placed.step` leży poprawnie na footprincie dla C25804, C2040 i
C165948. Wariant Y-up kładł model na boku, a surowy STEP był obrócony o 90 stopni (D-021).

Powtórzenie dla innej części:

```
python -m lcsc2fusion <id> --3d -o out
```

- [ ] Otwórz `out\<id>.lbr` (File > Open > Open from my computer) i zapisz do projektu.
- [ ] Content Manager > Footprints > prawy przycisk na footprincie > **Attach Existing 3D Model** >
  **Select from my computer...** > `out\<id>.placed.step` > nazwa pakietu > OK.
- [ ] Model leży na padach, stoi prosto (nie na boku), jest obrócony tak jak footprint (pin 1,
  znaczniki) i stoi na płytce, nie pod nią ani nad nią.
- [ ] Add-in: zaznacz "Include 3D model (STEP)", dodaj część, karta wyniku pokazuje pięć kroków i
  nazwy plików, a pliki STEP leżą obok `.lbr`.

Jeśli model jest przesunięty lub obrócony, wklej mi: id części, zrzut oraz zawartość `<id>.3d.json`
(pole `placement`, w tym `placement_source`).

## Brama 4: add-in w osobnym oknie (D-022)

Brama 2 (paleta w Fusionie) jest zastąpiona tą. Paleta znikała przy każdej zmianie środowiska.

Instalacja: rozpakuj `dist\LCSC2Fusion.zip` do `%APPDATA%\Autodesk\FusionAddins\` (nadpisz stary folder),
w Fusionie Shift+S > Add-Ins > `LCSC2Fusion` > Run. Zaznacz "Run on Startup".

- [ ] Po Run otwiera się osobne okno (Edge lub Chrome bez paska adresu) z formularzem wyszukiwania i
  statusem "Ready. Libraries are saved to ...". Brak banera rozłączenia.
- [ ] W oknie Text Commands (klawisz `S`, "Text Commands") są linie `button: ... -> added|exists|missing`.
  Wklej je do rozmowy, zwłaszcza te z `missing` lub `failed` i linie `relatedWorkspaces`.
- [ ] Przycisk LCSC2Fusion jest w Utilities > ADD-INS w Design, Schematic, Board i Library
  (zapisz, w których go brakuje).
- [ ] Wyszukaj `C2040`: jeden wynik (RP2040). Wyszukaj `100nF 0603`: lista z kolumnami.
- [ ] "Add to library" przy RP2040: status Saved, plik w `%USERPROFILE%\LCSC2Fusion\libraries\C2040.lbr`.
- [ ] "Open as a separate library" (dawniej "Open in Fusion Electronics"): biblioteka otwiera się w edytorze, a OKNO ZOSTAJE otwarte i dalej
  działa (to jest cała zmiana). Jeśli komenda nie zadziała, okno pokazuje kroki ręczne. Zapisz, co się stało.
- [ ] Przełącz się między Design, Schematic, Board i Library: okno nie znika, przycisk jest dostępny.
- [ ] Kliknij przycisk LCSC2Fusion, gdy okno już jest: istniejące okno wychodzi na wierzch, nie powstaje
  drugie. Zamknij okno krzyżykiem i kliknij przycisk: otwiera się nowe.
- [ ] Zaznacz "Include 3D model (STEP)", dodaj `C25804`: pliki STEP leżą obok `.lbr`, karta pokazuje kroki.
- [ ] Stop add-inu (Shift+S > Stop): okno zamyka się samo.
- [ ] Zamknij i uruchom ponownie Fusion (z "Run on Startup"): przyciski są od razu, okno otwiera się
  dopiero po kliknięciu.
- [ ] Brak błędów w Text Commands i w `%LOCALAPPDATA%\Autodesk\Autodesk Fusion 360\<userId>\logs\AppLogFile*.log`
  (szukaj `LCSC2Fusion`).

Wynik zapiszemy jako D-023: który mechanizm otwierania biblioteki zostaje, które przyciski działają i czy
usuwamy tryb palety (`LCSC2FUSION_UI=palette`).

## Brama 5: zbiorcza biblioteka i automatyczne 3D (D-024)

Instalacja: nowa wersja z `dist\LCSC2Fusion.zip` jest już rozpakowana w `%APPDATA%\Autodesk\FusionAddins\`.
W Fusionie: Shift+S, Add-Ins, `LCSC2Fusion`, Stop, potem Run. Praca Fusiona przy dodawaniu części trwa do około
minuty (okno pokazuje postęp), więc w tym czasie nie klikaj w Fusionie.

- [ ] Okno ma własną ikonę (niebieski układ scalony) w pasku tytułu i na pasku zadań. Jeśli na pasku zadań
  nadal jest globus, napisz (zrobimy skrót z własną ikoną).
- [ ] Część 1 (np. `C25804`, bez 3D, pole "Add to the Fusion library right away" zaznaczone): w oknie kolejne
  statusy, a w Fusionie powstaje i otwiera się dokument "LCSC2Fusion Library", zapisany w Hubie w katalogu
  głównym aktywnego projektu. Napisz, w którym projekcie.
- [ ] Część 2 (np. `C2040`): wchodzi do TEJ SAMEJ biblioteki (nie powstaje druga), w Library Manager są oba
  komponenty z footprintami, wersja pliku w Data Panel rośnie.
- [ ] Ta sama część jeszcze raz: komunikat "already there", żadnych okien dialogowych.
- [ ] Dwie części z tym samym pakietem (np. dwa różne rezystory 0603): bez dialogu nadpisywania. Jeśli dialog
  się pojawi, zrób zrzut i napisz, co wybrałeś.
- [ ] Z 3D (zaznacz "Include 3D model (STEP)"): po dodaniu Fusion otwiera edytor pakietu 3D z modelem. Model leży
  na padach, stoi prosto i jest zwrócony tak jak footprint (to samo kryterium co w Bramie 3). Potem Finish, Save, OK
  i File > Save biblioteki. Jeśli model jest obrócony lub przesunięty, napisz jak (zrzut).
- [ ] Przycisk "Add to Fusion library" przy karcie części działa, gdy pole automatyczne jest odznaczone.
- [ ] Zamknij Fusion i uruchom ponownie: kolejna część dodaje się do tej samej biblioteki (id zapamiętane).

Co wkleić przy błędzie: statusy z okna, linie `LCSC2Fusion` z najnowszego `AppLogFile*.log` (zwłaszcza `merge`,
`create_3d_package`, `import_step`) i zawartość `%LOCALAPPDATA%\LCSC2Fusion\state.json`. Wynik zapiszemy jako D-026.

## Co wkleić, jeśli coś nie działa

- Dokładny tekst błędu lub zrzut ekranu okna.
- Zawartość Text Commands.
- Ostatnie 100 linii najnowszego `%LOCALAPPDATA%\Autodesk\Autodesk Fusion 360\<userId>\logs\AppLogFile*.log`.
- Wersję Fusiona (Help > About) i system.
- Nazwę pliku .lbr, który się nie otworzył. Mogę go zlintować i zdiffować z plikiem wyeksportowanym
  przez Fusiona.

## Znane ryzyka, które ta lista ma rozstrzygnąć

Poniższe reguły są wywnioskowane z danych i dokumentacji, nie potwierdzone we Fusionie
(RESEARCH.md 2.4): kierunek obrotu padów (znak `curve` łuków jest już potwierdzony na danych z
Fusiona), `roundness="100"` dla owali,
obrót pionowych owali przelotowych, prostokąt zastępujący pad wielokątny, semantyka `slotLength`, lista padów w `connect`, atrybuty technologii. Komenda `Document.newDesignFromLocal`
jest nieudokumentowana i może nie działać na Twoim buildzie.
