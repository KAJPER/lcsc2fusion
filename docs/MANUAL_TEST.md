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

## Brama 2: add-in

Instalacja: rozpakuj `dist/LCSC2Fusion.zip` do `%APPDATA%\Autodesk\FusionAddins\` tak, żeby istniał
plik `%APPDATA%\Autodesk\FusionAddins\LCSC2Fusion\LCSC2Fusion.manifest`. W Fusion: Shift+S, zakładka
Add-Ins, uruchom `LCSC2Fusion` (opcjonalnie zaznacz "Run on Startup").

- [ ] Add-in startuje bez okna z błędem. W Utilities > Add-Ins jest przycisk LCSC2Fusion i otwiera paletę.
- [ ] Wpisz `C2040` i Search: lista ma jeden wynik (RP2040, obudowa QFN-56).
- [ ] Wpisz `100nF 0603` i Search: lista wyników z kolumnami id, MPN, producent, obudowa, typ.
- [ ] "Add to library" przy RP2040: status "ok", w `%USERPROFILE%\LCSC2Fusion\libraries\C2040.lbr`
  powstaje plik, w palecie są przyciski "Open in Fusion Electronics" i "Open folder".
- [ ] "Open in Fusion Electronics": biblioteka otwiera się w edytorze. Jeśli nie, paleta pokazuje kroki
  ręczne (File > Open > Open from my computer, potem File > Save). Zapisz, które z dwóch zaszło.
- [ ] Po zapisie do projektu biblioteka jest w Library Manager, a RP2040 da się umieścić w schemacie.
- [ ] Porównaj footprint RP2040 w Fusionie z `python -m lcsc2fusion C2040 --render -o out`
  (plik `out/C2040_footprint.png`) i z podglądem w EasyEDA: 56 padów na obwodzie plus pad 57 w środku.
- [ ] Błędny numer `C1` lub `C0`: czytelny komunikat, add-in dalej działa.
- [ ] Odłącz sieć i wyszukaj: czytelny komunikat o błędzie sieci, bez zawieszenia Fusiona.
- [ ] Zatrzymaj i uruchom add-in ponownie: palety nie ma dwa razy, brak błędów w Text Commands.

## Brama 3: modele 3D (około 20 minut)

Przygotowanie: `python scripts/make_3d_test_pack.py` (domyślnie C25804, C2040 i C165948 do `out\3d_test`).
Dla każdej części powstają `<id>.lbr`, `<id>.step` (surowy z EasyEDA), `<id>.placed.step` (ten sam model
przeniesiony do układu paczki: mm, Z w górę, początek w początku footprintu, spód bryły na płytce),
`<id>.placed_yup.step` (wariant obrócony o 90 stopni wokół X, tylko do tego testu) i `<id>.3d.json`.

Dla każdej części po kolei (zacznij od rezystora 0603, bo jego bryła jest asymetryczna i widać orientację):

1. Otwórz `<id>.lbr` (File > Open > Open from my computer) i zapisz do projektu (File > Save).
2. Content Manager > Footprints > prawy przycisk na footprincie > **Attach Existing 3D Model** >
   **Select from my computer...** > wybierz plik STEP > nazwij pakiet > OK.
3. Obejrzyj model nad footprintem i zapisz w tabeli niżej: czy leży na padach (położenie), czy stoi
   prosto i nie leży na boku (oś), czy kierunek obrotu się zgadza (znaczniki, pin 1), czy stoi na
   płytce (wysokość), czy import pokazał błąd lub trwał dziwnie długo.
4. Powtórz dla pozostałych wariantów: `.placed.step`, `.placed_yup.step`, `.step`. Usuń poprzedni
   pakiet 3D (albo użyj nowej nazwy pakietu), żeby nie mylić wyników.

| Część | Wariant STEP | Położenie XY | Oś (leży/stoi) | Obrót | Wysokość | Błąd importu |
|-|-|-|-|-|-|-|
| C25804 | placed | | | | | |
| C25804 | placed_yup | | | | | |
| C25804 | surowy | | | | | |
| C2040 | placed | | | | | |
| C165948 | placed | | | | | |

Co z tego wyniknie: który wariant zostaje (D-021), czy trzeba skorygować oś w `step.py`, i czy warto
robić automatyzację w stylu SnapEDA (D-020, punkt "odrzucone na teraz"). Add-in: zaznacz "Include 3D
model (STEP)", dodaj część i sprawdź, że karta wyniku pokazuje pięć kroków oraz nazwy plików, a pliki
STEP leżą obok `.lbr`.

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
