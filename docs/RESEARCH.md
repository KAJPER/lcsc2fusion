# RESEARCH (Etap 0)

Stan na 2026-10-11. Testy na żywo robione 2026-10-10 i 2026-10-11 z jednego IP. Fusion nie był uruchamiany, więc wszystko co dotyczy zachowania Fusiona ma tag źródła:

- [DOC] oficjalna dokumentacja Autodesk
- [LOCAL] odczytane z lokalnej instalacji Fusiona (build 2705.1.30), bez uruchamiania
- [CODE] widziane w działającym kodzie (SnapEDA, EETB, easyeda2kicad itp.)
- [ANEC] posty, fora, README firm trzecich
- [DERIVED] wywnioskowane przeze mnie, wymaga potwierdzenia testem

## 0. Streszczenie

1. Dane EasyEDA da się pobrać nieoficjalnymi endpointami (sprawdzone na żywo). Pojedyncza część to symbol + footprint w jednym JSON-ie.
2. Format .lbr piszemy sami (stdlib `xml.etree`), z pełną tablicą 46 warstw skopiowaną ze struktury pliku zapisanego przez Fusiona. DTD Autodesku nie jest wiarygodną bramką (odrzuca prawdziwe pliki Fusiona), więc potrzebny jest własny linter.
3. Typowane API Fusiona dla Electronics (`adsk.electron`) jest tylko do odczytu i eksportu, w preview. Nie da się nim niczego zaimportować, otworzyć ani zarejestrować. Wszystkie realne integracje (SnapEDA, EETB) jadą na nieudokumentowanych komendach tekstowych `Electron.run` i `Document.newDesignFromLocal`.
4. Zapis biblioteki do huba Fusiona rejestruje ją automatycznie w Library Managerze [DOC]. To najpewniejsza ścieżka rejestracji.
5. Trzy poważne ryzyka: ToS i robots.txt EasyEDA (sekcja 5), AGPL w kodzie referencyjnym easyeda2kicad, brak jakiegokolwiek testu importu we Fusionie (brama w sekcji 7).

## 1. EasyEDA / LCSC

### 1.1 Endpointy (sprawdzone na żywo)

| Cel | Żądanie | Uwagi |
|-|-|-|
| Część po id | `GET https://easyeda.com/api/products/{Cxxxx}/components` | 200 JSON. Parametr `version=` ignorowany. Ten endpoint jest rate limitowany (1.4). |
| Część po uuid | `GET https://easyeda.com/api/components/{uuid}` | Uuid symbolu daje to samo co wyżej, razem z `packageDetail`. Uuid footprintu daje sam footprint. Nie jest limitowany w testach. |
| Lista uuid | `GET /api/products/{Cxxxx}/svgs` | `result[]`: symbol, podjednostki (docType 6), footprint na końcu (docType 4). |
| Batch id na uuid | `POST /api/components/searchByNumbers`, form `numbers=["C2040"]` | Zwraca `uuid` (symbol), `puuid` (footprint), `step` (uuid 3D). Nieznane id są pomijane. Tylko wielkie litery. |
| Metadane LCSC | `GET https://wmsc.lcsc.com/ftps/wm/product/detail?productCode=C2040` | MPN, producent, obudowa, `pdfUrl` (prawdziwy PDF), obrazki. Dla części z danymi EDA bywa `result:null` (np. C1035, C454188), więc nie wolno na tym bramkować. |
| 3D OBJ | `GET https://modules.easyeda.com/3dmodel/{uuid}` | mm. Etap 3. |
| 3D STEP | `GET https://modules.easyeda.com/qAxj6KHrDKw4blvCG8QJPs7Y/{uuid}` | Stały segment bucketu. Etap 3. |

Uuid 3D bierzemy z `searchByNumbers.step` albo z `SVGNODE.attrs.uuid`, nie z `packageDetail.dataStr.head.uuid_3d` (daje 404).

Brak danych EDA: `/components` zwraca HTTP 200 z `{"success":false,"code":404}`, `/svgs` zwraca HTTP 200 z `code:422`. Status HTTP nie wystarcza, trzeba sprawdzać `success`.

### 1.2 Wyszukiwanie frazą

| API | Werdykt |
|-|-|
| JLCPCB `POST https://jlcpcb.com/api/overseas-pcb-order/v1/shoppingCart/smtGood/selectSmtComponentList`, JSON `{"keyword","currentPage","pageSize"}` | Najlepsze. Zwraca `componentCode` (id LCSC), MPN, producenta, obudowę, opis, `dataManualUrl`, typ Basic/Extended, stan. Trzeba odfiltrować wpisy producenta "JLCPCB Assembly" (id `C99xxxxxxxx`), bo nie istnieją w EasyEDA. |
| EasyEDA Std `POST https://easyeda.com/api/components/search`, form `wd=…&type=3&doctype[]=2` | Działa, zwraca uuid od razu. Rozmyte, dokładne trafienie bywa na końcu. Wynik nie zawiera `shape`, potrzebny drugi request. |
| EasyEDA Pro `POST https://pro.easyeda.com/api/eda/product/search` | Działa, uuid w formacie Pro (inne niż Std). Nie używać. |
| LCSC `wmsc.lcsc.com/ftps/wm/search/global` | Nieużywalne, Akamai zwraca 403. |

Reguła: wejście pasujące do `^C\d+$` (po normalizacji do wielkich liter) idzie wprost do pobrania. Reszta to fraza i idzie do wyszukiwarki.

### 1.3 Nagłówki

- easyeda.com (CloudFront) zwraca 403 dla UA `Python-urllib/*`, `python-requests/*` i gołego `Mozilla/5.0`. Opisowy własny UA (`lcsc2fusion/0.1 (+url repo)`) i pusty UA przechodzą. Trzeba więc zawsze ustawiać własny UA.
- wmsc.lcsc.com zwraca 403 dla pustego UA i dla python-requests/urllib.
- modules.easyeda.com i jlcpcb.com: dowolny UA.
- Gzip działa.

### 1.4 Rate limiting

- Tylko `/api/products/{id}/components` był blokowany: CloudFront `403 Request blocked` po około 20 żądaniach w 100 s albo po serii 60 w 8 s. Blokada trwa 2,5 do 5 minut i znika sama. Podczas blokady inne endpointy działały.
- Bez limitu w testach: `/api/components/{uuid}` (80 w 11,6 s), `searchByNumbers` (40), Std search (30), `/svgs` (40), JLCPCB (25), LCSC detail (20).
- Ścieżka domyślna: `searchByNumbers` potem `/api/components/{uuid}`. `/products/{id}/components` tylko jako fallback.
- Klient: współbieżność 1, minimalny odstęp między żądaniami, backoff 5 minut przy 403, cache na dysku z TTL.

### 1.5 Struktura JSON

Top level: `{success, code, result}`. `result`: `uuid`, `title` (MPN), `description`, `docType` 2, `lcsc{id,number}`, `szlcsc`, `tags`, `updateTime`, `dataStr` (symbol), `packageDetail` (footprint), `SMT`, `subparts[]` (tylko multi-unit).

- `dataStr` symbolu: `head{x,y,uuid,puuid,c_para,license}`, `canvas`, `shape[]`, `BBox{x,y,width,height}`.
- `head.c_para` zawiera `pre` (prefiks, np. `U?`), `name`, `package`, `Manufacturer`, `Manufacturer Part`, `Supplier Part`, `JLCPCB Part Class`. Pasywne mają też `Value`.
- `packageDetail.dataStr`: `head{x,y,c_para{package,3DModel,...}}`, `canvas`, `shape[]`, `layers[]`, `BBox`.
- Każdy kształt to jeden string `KIND~f1~f2~…`. Wszystkie wartości to stringi, puste znaczy domyślne. `split("~")` jest bezpieczne. Piny symbolu mają dodatkowo podsekcje `^^`.
- `updateTime` zmienia się codziennie dla części z danymi, więc nie nadaje się na stabilny klucz cache. Klucz to id LCSC plus TTL.

### 1.6 Kształty symbolu (jednostka: 0,254 mm)

| Tag | Pola po tagu |
|-|-|
| `R` | `x~y~rx~ry~w~h~stroke~sw~style~fill~id~lock` (x,y lewy górny róg) |
| `C` | `cx~cy~r~stroke~sw~style~fill~id~lock` |
| `E` | `cx~cy~rx~ry~stroke~sw~style~fill~id~lock` |
| `A` | `svgpath~helperdots~stroke~sw~style~fill~id~lock` |
| `PL` / `PG` | `"x y x y …"~stroke~sw~style~fill~id~lock` |
| `PT` | `svgpath~stroke~sw~style~fill~id~lock` |
| `T` | `type~x~y~rot~color~font~size~weight~style~baseline~role~text~display~anchor~id~lock` |
| `AR` | strzałka, nieudokumentowana, pomijamy |

### 1.7 PIN

```
P~show~3~9~525~230~0~gge461~0^^525~230^^M 525 230 h -10~#880000^^1~511~233.75~0~GPIO7~end~...^^1~524~229.9~0~9~end~...^^0~518~230^^0~M 515 227 L 512 230 L 515 233
```

| Sekcja | Pola |
|-|-|
| 0 | `P~show\|hide~type~spice_no~x~y~rotation~id~locked`. x,y to punkt połączenia (koniec "gorący"). Rotation może być pustym stringiem (0). |
| 1 | kropka: `x~y` |
| 2 | ścieżka `M x y h±L` lub `v±L`. Długość to wartość bezwzględna. Kierunek z ścieżki jest niewiarygodny, kierunek bierzemy z rotation. |
| 3 | nazwa: `show~x~y~rot~text~anchor~font~size~color` |
| 4 | numer, ten sam układ. Pole `[4]` to prawdziwy numer pinu (zdarzają się `A1B12`, `B4A9`) i nadpisuje `spice_no`. |
| 5 | kółko inwersji: `show~cx~cy` |
| 6 | zegar: `show~path` |

Typ elektryczny: 0 undefined, 1 input, 2 output, 3 bidirectional, 4 power (kody z easyeda2kicad, 4 nie zaobserwowane na żywo). Typ bywa pustym stringiem.

Rotacja pinu zaobserwowana na C2040: 0 prawa krawędź (linia idzie w lewo od punktu połączenia), 90 góra (w dół), 180 lewa (w prawo), 270 dół (w górę).

### 1.8 Kształty footprintu (jednostka: 0,254 mm)

| Tag | Pola |
|-|-|
| `PAD` | tabela niżej |
| `TRACK` | `sw~layer~net~"x y x y …"~id~lock` |
| `CIRCLE` | `cx~cy~r~sw~layer~id~lock` |
| `ARC` | `sw~layer~net~svgpath~helper~id~lock` |
| `RECT` | `x~y~w~h~layer~id~lock~sw~fill` (x,y lewy górny róg) |
| `HOLE` | `cx~cy~radius~id~lock` (na żywo to promień, wbrew dokumentacji Std) |
| `VIA` | `cx~cy~diam~net~hole_radius~id~lock` (tylko wg dokumentacji, nie widziane) |
| `TEXT` | `type~cx~cy~sw~rot~mirror~layer~net~size~text~glyphpath~display~id~lock` |
| `SOLIDREGION` | `layer~net~svgpath~solid\|cutout\|npth~id` |
| `SVGNODE` | JSON, tylko model 3D (Etap 3) |
| `COPPERAREA` | tylko wg dokumentacji, nie widziane |

PAD (indeks 0 to tag):

| Idx | Pole | Uwagi |
|-|-|-|
| 1 | shape | `RECT`, `OVAL`, `ELLIPSE`, `POLYGON` |
| 2,3 | środek x,y | |
| 4,5 | szerokość, wysokość | |
| 6 | layer | 1 top SMD, 2 bottom, 11 przelotowy |
| 8 | numer | string, np. `A1B12`, `41` |
| 9 | hole_radius | 0 oznacza SMD. Średnica wiercenia = 2 × promień. |
| 10 | points | bezwzględne wierzchołki, rotacja już wliczona (POLYGON) |
| 11 | rotation | stopnie |
| 13 | hole_length | długość slotu, koniec do końca |
| 14 | slot_outline | oś slotu |
| 15 | plated | `Y` / `N` |
| 17, 18 | nieznane | prawdopodobnie ekspansja maski/pasty, znaczenie niezweryfikowane |

### 1.9 Jednostki, osie, łuki

- 1 jednostka = 10 mil = 0,254 mm, w symbolu i w footprincie. Potwierdzone na C2040 (rozstaw 0,4 mm = 1,5748 j.). Konwersja tylko w parserze, w jednym miejscu.
- Oś Y w dół w obu. Eagle ma Y w górę, więc `y_eagle = -(y - origin_y)` w jednym miejscu.
- Początek footprintu: `head.x`, `head.y`. Początek symbolu: dla multi-unit `head.x/y` pierwszej podczęści, inaczej środek `BBox`, potem przyciągnięcie do siatki 5 j. (1,27 mm), żeby piny wylądowały na siatce.
- Łuki: SVG `M x y A rx ry xrot large sweep x y`. Konwersja punkt końcowy na środek wg W3C SVG implementation notes F.6.5, promienie za małe skalować w górę, zdegenerowane łuki (rx lub ry równe 0) pomijać.
- Znak `curve` w Eagle [DERIVED]: po odwróceniu Y `sweep=1` daje ujemny `curve`, `sweep=0` dodatni. Wartość bezwzględna to kąt wycinka (dla `large=1` uzupełnienie do 360). Do potwierdzenia renderem i testem we Fusionie.
- Znak rotacji padów i tekstu w EasyEDA nie jest udokumentowany [DERIVED]. Nie ma znaczenia dla RECT/OVAL/ELLIPSE przy 0 i 180, ma dla pozostałych kątów i tekstu. Potwierdzić asymetrycznym padem.

### 1.10 Warstwy EasyEDA footprintu

Z żywego `packageDetail.dataStr.layers[]` (C2040):

| ID | Nazwa | ID | Nazwa |
|-|-|-|-|
| 1 | TopLayer | 11 | Multi-Layer |
| 2 | BottomLayer | 12 | Document |
| 3 | TopSilkLayer | 13 | TopAssembly |
| 4 | BottomSilkLayer | 14 | BottomAssembly |
| 5 | TopPasteMaskLayer | 15 | Mechanical |
| 6 | BottomPasteMaskLayer | 19 | 3DModel |
| 7 | TopSolderMaskLayer | 21-52 | Inner1-Inner32 |
| 8 | BottomSolderMaskLayer | 99 | ComponentShapeLayer |
| 9 | Ratlines | 100 | LeadShapeLayer |
| 10 | BoardOutLine | 101 | ComponentMarkingLayer |

### 1.11 Przypadki brzegowe

- Multi-unit (TL072 C6961, TMS320C6678 C527553): główne `dataStr.shape` puste, `BBox` zerowy, jednostki w `result.subparts[]` (każda z własnym `dataStr`, `docType:"6"`, `head.x/y`, `BBox`). Decyzja: osobny symbol i gate na jednostkę (D-007).
- Powtórzone numery padów: ESP32-S3 C2913202 ma 49 padów i 41 pinów symbolu, dziewięć padów ma numer `41` (dzielony pad termiczny). Nazwy padów w Eagle muszą być unikalne [DOC/ANEC], więc `41`, `41_2`, … i `<connect pad="41 41_2 …">` (składnia z listą padów potwierdzona w pliku zapisanym przez Fusiona, `pad="15 17"`).
- USB-C C165948: numery nienumeryczne (`A1B12`), pady POLYGON (4), sloty OVAL przelotowe (4), HOLE (2).
- Części bez 3D (C476261) i bez danych EDA (23 z 36 losowych id).
- Id są case sensitive, tylko wielkie litery.
- Nigdy nie widziano części z samym symbolem lub samym footprintem, ale trzeba chronić się przed brakiem `packageDetail` (własny wyjątek, nie `KeyError`).
- Nazwa komponentu: sanityzacja, obcinać od `(` lub `[`, usuwać spacje i `;`.

### 1.12 Zestaw referencyjny (wszystkie id pobrane i zweryfikowane na żywo)

| Rola | LCSC | MPN | Obudowa | Piny / pady |
|-|-|-|-|-|
| rezystor 0603 | C25804 | 0603WAF1002T5E | 0603 | 2 / 2 |
| kondensator | C14663 | CC0603KRX7R9BB104 | 0603 (4 łuki) | 2 / 2 |
| dioda | C81598 | 1N4148W | SOD-123 | 2 / 2 |
| dioda | C8678 | SS34 | SMA | 2 / 2 |
| SOT-23 | C20526 | MMBT3904 | SOT-23 | 3 / 3 |
| SOT-223 z tabem | C6186 | AMS1117-3.3 | SOT-223 | 4 / 4 |
| SOIC-8 | C7593 | NE555DR | SOIC-8 | 8 / 8 |
| multi-unit | C6961 | TL072CDT | SO-8 | 5+3 / 8 |
| QFN z thermal padem | C2040 | RP2040 | LQFN-56 | 57 / 57 |
| SOIC-8-EP | C454188 | MP1584EN | SOIC-8-EP | 9 / 9 |
| VSON z thermal padem | C15483 | TPS63020DSJR | VSON-14-EP | 15 / 15 |
| TQFP | C14877 | ATMEGA328P-AU | TQFP-32 | 32 / 32 |
| LQFP | C8734 | STM32F103C8T6 | LQFP-48 | 48 / 48 |
| pin header | C2895846 | PZ254V-11-40P | THT 1x40 | 40 / 40 |
| DIP THT | C46749 | NE555P | DIP-8 | 8 / 8 |
| USB-C | C165948 | TYPE-C-31-M-12 | SMD 16P | 16 / 16 |
| ESP32-S3 moduł | C2913202 | ESP32-S3-WROOM-1-N16R8 | SMD 25,5x18 | 41 / 49 |
| kwarc | C12674 | X49SM8MSD2SC | HC-49S | 2 / 2 |
| kwarc 4-pad | C13738 | X322516MLB4SI | 3225-4P | 4 / 4 |
| cewka | C1035 | SDFL1608S100KTF | 0603 | 2 / 2 |
| cewka mocy | C476261 | SRN6045-100M | 6x6 | 2 / 2 |
| LED | C2286 | KT-0603R | 0603 | 2 / 2 |
| przycisk | C318884 | TS-1187A-B-A-B | SMD-4P | 4 / 4 |
| stress test | C1019365, C527553 | PIC32MZ, TMS320C6678 | BGA | 177 / 841 pady |

## 2. Format Eagle .lbr

### 2.1 Struktura

Źródło: `eagle.dtd` 9.6.2 (CC BY-ND 3.0, nie redystrybuować, także wersji poprawionej). Współrzędne w mm, Y w górę, booleany `yes`/`no`, maksymalnie około 6 miejsc po przecinku, bez notacji wykładniczej, tekst z escapowaniem XML.

Kolejność w `<library>`: `description?`, `packages?`, `packages3d?`, `symbols?`, `devicesets?`. `<layers>` jest wymagane w `<drawing>` (może być puste). `deviceset` wymaga `gates` i `devices`.

Atrybuty (R = wymagane):

| Element | Atrybuty |
|-|-|
| `layer` | number R, name R, color R, fill R, visible=yes, active=yes |
| `package` | name R |
| `wire` | x1 y1 x2 y2 width layer R, curve=0, cap=round |
| `circle` | x y radius width layer R (width=0 to wypełniony dysk) |
| `rectangle` | x1 y1 x2 y2 layer R, rot=R0 |
| `polygon` | width layer R, vertex x y R curve=0 |
| `text` | x y size layer R, font=proportional, ratio=8, rot=R0, align=bottom-left |
| `hole` | x y drill R |
| `pad` | name x y drill R, diameter=0 (liczone z DRC, ustawiać jawnie), shape=round (square, octagon, long, offset), rot, stop, thermals, first |
| `smd` | name x y dx dy layer R (1 lub 16), roundness=0..100, rot, stop=yes, thermals=yes, cream=yes |
| `pin` | name x y R, visible=both (off, pad, pin), length=long (point, short, middle), direction=io (nc, in, out, oc, pwr, pas, hiz, sup), function (dot, clk, dotclk), swaplevel=0, rot=R0 |
| `deviceset` | name R, prefix, uservalue=no |
| `gate` | name symbol x y R, addlevel=next, swaplevel=0 |
| `device` | name (może być ""), package |
| `connect` | gate pin pad R, route=all (`pad` może być listą rozdzieloną spacją) |
| `technology` | name R (zawsze emitować `name=""`) |
| `attribute` | name R, value, constant=no |

Składnia rotacji: `[S][M]R<deg>`. Dla padów i pinów używamy tylko `R<deg>` (bez `S`/`M`). Piny symbolu: tylko 0, 90, 180, 270.

Pin: `x,y` to punkt połączenia (zewnętrzny koniec), `rot` to kierunek, w którym linia pinu rozciąga się od tego punktu w stronę korpusu [DOC + potwierdzone w prawdziwych plikach]. R0 daje +x (pin na lewej krawędzi korpusu). Długości: point 0, short 2,54, middle 5,08, long 7,62 mm.

Łuk: `curve` to kąt od (x1,y1) do (x2,y2), dodatni przeciwnie do ruchu wskazówek zegara (Y w górę), zakres -359,9 do 359,9. Promień = chord / (2 sin(|curve|/2)). Pełne okręgi przez `<circle>`.

### 2.2 Warstwy

Deklarujemy pełny zestaw 46 warstw (taki zapisuje Fusion i SnapEDA). Brak dowodu, że pusta lub częściowa lista psuje import, ale brak też dowodu, że działa, więc nie ryzykujemy. Format wiersza: `numer nazwa kolor fill visible`, wszystkie `active="yes"`.

```
1 Top 4 1 yes | 16 Bottom 1 1 yes | 17 Pads 2 1 yes | 18 Vias 2 1 yes | 19 Unrouted 6 1 yes
20 Dimension 15 1 yes | 21 tPlace 7 1 yes | 22 bPlace 7 1 yes | 23 tOrigins 15 1 yes | 24 bOrigins 15 1 yes
25 tNames 7 1 yes | 26 bNames 7 1 yes | 27 tValues 7 1 yes | 28 bValues 7 1 yes
29 tStop 7 3 yes | 30 bStop 7 6 no | 31 tCream 7 4 yes | 32 bCream 7 5 no
33 tFinish 6 3 no | 34 bFinish 6 6 no | 35 tGlue 7 4 no | 36 bGlue 7 5 no | 37 tTest 7 1 no | 38 bTest 7 1 no
39 tKeepout 4 11 yes | 40 bKeepout 1 11 yes | 41 tRestrict 4 10 yes | 42 bRestrict 1 10 yes | 43 vRestrict 2 10 yes
44 Drills 7 1 no | 45 Holes 7 1 no | 46 Milling 3 1 no | 47 Measures 7 1 no | 48 Document 7 1 yes | 49 Reference 7 1 yes
51 tDocu 7 1 yes | 52 bDocu 7 1 yes | 90 Modules 5 1 yes
91 Nets 2 1 yes | 92 Busses 1 1 yes | 93 Pins 2 1 yes | 94 Symbols 4 1 yes | 95 Names 7 1 yes | 96 Values 7 1 yes
97 Info 7 1 yes | 98 Guide 6 1 yes
```

Ostrzeżenie [ANEC]: Adom twierdzi, że nowsze buildy zapisują atrybuty warstw `blackcolor`/`whitecolor`/`coloredcolor`. Nie ma tego w DTD ani w żadnej próbce. Dlatego brama z sekcji 7 zaczyna się od eksportu małej biblioteki z samego Fusiona (UI albo `createEagleLbrExportOptions`) i porównania nagłówka i warstw.

Użycie warstw:

| Cel | Warstwa |
|-|-|
| obrys silkscreen footprintu | 21 |
| tekst `>NAME` w footprincie | 25 |
| tekst `>VALUE` w footprincie | 27 |
| courtyard | 39 (konwencja SparkFun, SnapEDA) |
| obrys dokumentacyjny, korpus | 51 |
| pady SMD | `smd layer="1"` (top) lub 16 (bottom) |
| pady przelotowe | `<pad>` (bez atrybutu layer) |
| maska i pasta | nie rysujemy, generowane z `stop` i `cream` (plus reguły DRC) |
| korpus symbolu | 94 |
| `>NAME` / `>VALUE` w symbolu | 95 / 96 |
| frezowanie slotów | 46 |

### 2.3 Mapowanie warstw EasyEDA na Eagle (propozycja, jedna stała tabela w kodzie)

| EasyEDA | Eagle |
|-|-|
| 1 / 2 | 1 / 16 |
| 3 / 4 | 21 / 22 |
| 5 / 6 / 7 / 8 | pomijane (maska i pasta z `stop`/`cream`) |
| 10 BoardOutLine | 20 |
| 12 / 13 / 15 | 51 |
| 14 | 52 |
| 99 ComponentShape | 51 |
| 100 LeadShape | 51 |
| 101 ComponentMarking | 21 |
| 11 Multi-Layer | pad przelotowy |

Courtyard na 39 generujemy sami z bbox padów i korpusu plus margines, bo EasyEDA nie ma osobnej warstwy courtyard.

### 2.4 Reguły konwersji [DERIVED, każdą potwierdzić renderem i testem we Fusionie]

| Przypadek | Reguła |
|-|-|
| pad SMD RECT | `smd`, roundness 0 |
| pad SMD OVAL lub ELLIPSE | `smd`, roundness 100 (ELLIPSE: dx=dy) |
| pad przelotowy ELLIPSE | `pad shape="round"` |
| pad przelotowy RECT | `pad shape="square"` (stratne dla nieprostokątnych, ostrzeżenie) |
| pad przelotowy OVAL | `pad shape="long"` (proporcje 2:1 z DRC, nie do ustawienia w bibliotece) |
| pad POLYGON | aproksymacja prostokątem z bbox (znane ograniczenie) |
| thermal pad | jeden duży `smd` z `stop="no" cream="no"` plus prostokąty na 29 i 31 (tak robi plik DRV8833 zapisany przez Fusiona) |
| HOLE (niepokryty) | `<hole drill=2r>` |
| slot | `drill` = szerokość slotu, kontur na warstwie 46 |
| powtórzony numer pada | `41`, `41_2`, …, jedna lista w `connect` |
| rotacja pinu | `eagle_rot = (180 + ee_rot) % 360` (spójne z obserwacją na C2040 po odwróceniu Y: EE 90 daje R270, EE 270 daje R90) |
| długość pinu | 0 to point, do 2,54 to short, do 5,08 to middle, powyżej long |
| typ pinu | 1 in, 2 out, 3 io, 4 pwr, 0 lub pusty pas |
| nazwy pinów | bez spacji i `;`, duplikaty `GND@1`, `GND@2` |
| roundness prostokąta zaokrąglonego | promień = roundness% × (krótszy bok / 2), definicja Autodesku znana tylko dla 0 i 100 |
| ekspansja maski/pasty per pad | niereprezentowalna, tylko `stop`/`cream` |

Znane pułapki z cudzych projektów: pole `function="none"` na `<pin>` odrzuca loader Eagle mimo że DTD je dopuszcza (pomijać atrybut) [ANEC, stark9000/easyeda2eagle]. Ten sam projekt raportuje odwrócenie rotacji pinu o 180, zgodne z naszą regułą.

### 2.5 Konwencje biblioteki

- Nazwy (package, symbol, deviceset, pad, pin) wielkimi literami, unikalne bez względu na wielkość liter, bez spacji i `;`.
- Pady nazywamy prawdziwym numerem (`1`, `A1`, `EP`).
- Tekst: `font="vector"`, ratio 12 do 20, rozmiar 0,6 do 1,0 mm w footprincie, 1,778 w symbolu.
- Piny na siatce 2,54 mm, `length="short"`, `visible="pad"` lub `both`. `swaplevel="1"` tylko dla wymiennych (rezystor).
- Deviceset: prefix z `c_para.pre` bez `?`, `uservalue="yes"` dla pasywnych. Zawsze `<technologies><technology name=""/></technologies>`.
- Atrybuty w `<technology>`: `MPN`, `MANUFACTURER`, `LCSC_PART`, `DATASHEET`, plus atrybucja źródła (sekcja 5). Nazwy atrybutów bez spacji, bez `-` na początku.
- Wielkość polygonu: co najmniej 3 wierzchołki, szerokość nie mniejsza niż 0,01 (nie ma w żadnej prawdziwej bibliotece szerokości 0).
- 3D: referencja w .lbr to identyfikatory zarządzane przez Fusiona (`urn:adsk.eagle:package:...` lub `wip_urn`), nie da się ich zmyślić. 3D zostawiamy do Etapu 3 i dołączamy w Fusionie.

### 2.6 Własny linter (zamiast walidacji DTD)

DTD 9.6.2 odrzuca prawdziwe pliki (wymaga `name` w `<library>`, nie zna `locally_modified` ani `<note minversion>`), więc nie jest bramką. Własny linter sprawdza:

1. brak duplikatów nazw (package, symbol, deviceset, device, pad, pin), bez względu na wielkość liter
2. każde `connect` wskazuje istniejący gate, pin i pad
3. brak spacji i `;` w nazwach padów i pinów
4. wartości enum w dozwolonych zbiorach (`direction`, `shape`, `align`, `visible`, `length`)
5. `rot` pasuje do `^R\d+(\.\d+)?$`, piny symbolu tylko 0/90/180/270
6. polygony mają ≥ 3 wierzchołki, szerokości > 0
7. każda użyta warstwa jest zadeklarowana w `<layers>`
8. kolejność elementów w `<library>`, obecność `gates` i `devices`
9. poprawny XML i escapowanie

Liczba pinów symbolu równa liczbie unikalnych padów po rozwinięciu list w `connect` (wymaganie z CLAUDE.md, osobny test geometrii).

Python: nie używamy żadnej biblioteki do .lbr (Swoop 2021, PyEAGLE 2015, eaglepy 2014 są martwe). Własne dataclasses plus `xml.etree.ElementTree`. Nagłówek XML i DOCTYPE doklejamy ręcznie, bo ElementTree nie ma DOCTYPE.

## 3. Fusion

### 3.1 Struktura add-inu

- Wbudowany Python: 3.14 [DOC: release notes, build 2606.1.22] i [LOCAL]. Nie dostarczać `.pyc` (bytecode 3.12 nie ładuje się w 3.14). Projekt ma 3.11+, więc core pisać na stdlib, bez składni specyficznej dla 3.12+.
- `Lib/site-packages` w Pythonie Fusiona jest praktycznie pusty [LOCAL] (brak `requests`, `certifi`). Autodesk zaleca vendorowanie modułów w folderze add-inu [DOC]. Decyzja: runtime tylko stdlib (`urllib.request`), D-002.
- `urllib` z własnym UA pobrał dane z easyeda.com wewnątrz interpretera Fusiona na Windows [LOCAL]. macOS: brak danych, możliwa potrzeba CA bundle (nie testowane).
- Foldery add-inów [DOC][LOCAL]: `%APPDATA%\Autodesk\Autodesk Fusion 360\API\AddIns`, `%APPDATA%\Autodesk\FusionAddins` (dla aplikacji spoza App Store, folder bez `.bundle`, istnieje lokalnie), `%APPDATA%\Autodesk\ApplicationPlugins` (App Store, `.bundle` z `PackageContents.xml`, tu siedzi SnapEDA.bundle). Dla open source cel to `FusionAddins` albo zielony plus w Scripts and Add-Ins (Shift+S).
- Manifest: nazwa folderu, `.py` i `.manifest` identyczne. Pola: `autodeskProduct` ("Fusion" lub "Fusion360", oba w użyciu), `type:"addin"`, `id`, `author`, `description`, `version`, `runOnStartup`, `supportedOS:"windows|mac"`, `editEnabled`, `iconFilename`. Punkty wejścia `run(context)` i `stop(context)` (w `stop` sprzątać paletę, komendy, handlery).
- Wątki [DOC]: Python działa w głównym wątku UI, długie HTTP blokuje Fusiona. Wzorzec: wątek roboczy, wynik przez `app.fireCustomEvent(id, json)`, obsługa na wątku głównym. API Fusiona nie wolno wołać z wątku roboczego.
- Logi: `app.log(msg)` (okno Text Commands), `LogTypes.FileLogType` do pliku. Logi Fusiona: `%LOCALAPPDATA%\Autodesk\Autodesk Fusion 360\<userId>\logs\AppLogFile*.log` [LOCAL].

### 3.2 Paleta HTML/JS

- `ui.palettes.add(id, name, htmlFileURL, isVisible, showCloseButton, isResizable, width, height, useNewWebBrowser)` [DOC]. Przeglądarka to teraz zawsze Qt, parametr `useNewWebBrowser` nieużywany.
- JS do add-inu: `adsk.fusionSendData(action, dataString)` zwraca Promise. Add-in odbiera w `palette.incomingFromHTML` (`html_args.action`, `.data`, odpowiedź przez `html_args.returnData`).
- Add-in do JS: `palette.sendInfoToHTML(action, data)`. Strona musi mieć `window.fusionJavaScriptHandler = {handle(action, data){...; return "OK"}}`. Pusty zwrot liczy się jako błąd.
- `fetch`, CORS i `localStorage` w palecie: nieudokumentowane. Cały ruch sieciowy idzie przez Pythona.
- Wzorzec do skopiowania: zainstalowany szablon `Python/DefaultAddIn/files/commands/paletteShow/entry.py` [LOCAL] i oficjalny PaletteSample [DOC].

### 3.3 Co API realnie umożliwia dla Electronics

- `adsk.electron` istnieje od maja 2026 (v.2703.1.11), preview, tylko odczyt. Typy: `EcadDesign`, `Schematic`, `Board`, `Library`. Jedyny kanał wyjściowy to `exportManager.createEagleBrdExportOptions/SchExportOptions/LbrExportOptions(path)` plus `execute()`, produkuje EAGLE 9.6.2 [DOC]. Nie ma importu, otwierania, rejestracji ani wstawiania do schematu [DOC][LOCAL: `electron.py` ma 138 klas, żadnej metody importu]. Dokumentacja zabrania dystrybucji programów opartych o funkcje preview.
- `adsk.core.DocumentTypes` zna tylko design Fusiona. `ImportManager` nie obsługuje EAGLE/LBR.
- Komendy tekstowe (`app.executeTextCommand`) nie są udokumentowane przez Autodesk. Stan wiedzy [CODE/ANEC]:
  - `Document.newDesignFromLocal "<ścieżka>.lbr"` otwiera .lbr w edytorze bibliotek Electronics (Adom, lipiec 2026). `Document.Open` na .lbr nie działa.
  - `Electron.run <polecenie EAGLE>` działa tylko w edytorach Schematic, Board i Library. Błędy nie wracają, widać je tylko jako toasty i w logu. Polecenia otwierające dialogi blokują wątek główny.
  - SnapEDA 2.2.1 (kwiecień 2025, lokalnie zainstalowany) robi: `Electron.run "Use '<src.lbr>'"`, `Electron.run 'Copy "<NAME>.dev@<lib>"'`, `Electron.run 'Use "-<lib>"'`, potem `document.save(...)`. Czyli scala device z lokalnego .lbr do otwartej biblioteki.
  - EETB (MIT) uruchamia `ELECTRON.RUN SCRIPT` i `RUN`. Jego dokumentacja: komend tekstowych Electronics nie wolno uruchamiać w handlerach `commandExecute` (ryzyko crasha), są bezpieczne w handlerze `incomingFromHTML` palety.
  - Ścieżki ze spacjami trzeba cytować.
- Polecenia EAGLE `SCRIPT`, `RUN`, `OPEN`, `USE`, `COPY`, `ADD dev@/ścieżka/lib.lbr` są udokumentowane [DOC] jako polecenia, ale uruchamianie ich programowo przez `Electron.run` jest tylko [CODE].
- Lokalizacje: Preferences > Electronics > Directory ustawia ścieżki bibliotek, skryptów, ULP [DOC], lokalnie `UseDefault="1"` [LOCAL]. Nie znaleziono żadnego obserwowanego, auto ładowanego folderu lokalnych bibliotek. Zarządzany cache bibliotek (`%LOCALAPPDATA%\Autodesk\Autodesk Fusion 360\Electron\<id>\lbr\...`) nie jest do zapisu przez użytkownika.
- Rejestracja: zapis biblioteki ją uploaduje do huba i "dodaje do Library Managera i aktywuje automatycznie" [DOC]. Release 2705.1.11 naprawił przypadki, gdy po uploadzie biblioteka nie trafiała do Library Managera ani Place Panelu. Konta osobiste mają limit 10 edytowalnych dokumentów [ANEC], więc jedna narastająca biblioteka jest lepsza niż jedna na część.

### 3.4 Przepływ "Dodaj do biblioteki" (rekomendacja)

Ranking według tego, ile jest potwierdzone:

| Wariant | Kroki | Pewność |
|-|-|-|
| C: ręczny (fallback, zawsze działa) | add-in zapisuje .lbr, otwiera folder, pokazuje instrukcję: File > Open > Open from my computer, potem File > Save do projektu, albo Data Panel > Upload | [DOC] |
| A: otwarcie w edytorze | add-in zapisuje .lbr (ścieżka bez spacji), woła `executeTextCommand('Document.newDesignFromLocal "C:/.../C2040.lbr"')` z handlera palety, użytkownik robi File > Save, biblioteka rejestruje się sama | komenda [ANEC], reszta [DOC] |
| B: narastająca biblioteka | użytkownik ma otwartą docelową bibliotekę, add-in robi `Use` / `Copy` / `Use "-lib"` jak SnapEDA, potem save | [CODE], wymaga testu na buildzie 2705 |

Rekomendacja na v1: C jako gwarantowany plus A jako przycisk, B po potwierdzeniu A. Weryfikacja kontekstu przed `Electron.run`: `executeTextCommand("DebugCommands.ActiveWorkspace")`. Wszystko po stronie Fusiona opakowane cienkim interfejsem, żeby dało się podmienić wariant.

### 3.5 Testowalność

- Oficjalne stuby leżą lokalnie: `%APPDATA%\Autodesk\Autodesk Fusion 360\API\Python\defs\adsk\` (`core.py`, `fusion.py`, `electron.py`, ...) [LOCAL, potwierdzone]. Importują się w zwykłym CPythonie, klasy bazowe handlerów są prawdziwe, `Application.get()` zwraca atrapę, `executeTextCommand` zwraca `''`. Licencja stubów (repo FusionAPIReference) to CC BY-NC-SA, więc nie kopiować ich do repo, tylko wskazać ścieżkę zmienną środowiskową.
- Zwykły `MagicMock` jako `adsk.core` psuje dziedziczenie po klasach handlerów. Użyć stubów albo własnego fałszywego modułu z prawdziwymi klasami bazowymi.
- Na PyPI jest tylko `adsk` 2.0.12157 z 2022 (bez `adsk.electron`). Nie używać.
- Wniosek architektoniczny: cały core i logika add-inu (parsowanie zdarzeń palety, budowa komend) w czystym Pythonie, adapter `adsk` cienki.

### 3.6 Dystrybucja

Zip z folderem o nazwie add-inu, rozpakowany do `%APPDATA%\Autodesk\FusionAddins\` albo dodany zielonym plusem. Nowy add-in może startować wyłączony. Windows może blokować pobrany zip (Unblock). Nie znaleziono wymogu podpisywania add-inów w czystym Pythonie. App Store to osobna ścieżka (`.bundle` + `PackageContents.xml`), na później.

## 4. Prior art

| Projekt | Licencja | Co robi |
|-|-|-|
| uPesy/easyeda2kicad.py | AGPL-3.0, 1690 gwiazdek | Referencyjny klient i parser EasyEDA. Nie kopiujemy kodu (D-001). |
| TousstNicolas/JLC2KiCad_lib | MIT | Alternatywny parser, przydatny jako źródło testowych id. |
| nschurando/easyeda2eagle | AGPL-3.0 | LCSC na .lbr plus STEP, pomija łuki. Oparty o easyeda2kicad. |
| stark9000/easyeda2eagle | brak licencji | LCSC na .lbr, README opisuje pułapki loadera Eagle. Wendoruje kod AGPL. |
| CircuitSetup/EasyEDA-to-Fusion360-Eagle | MIT | Konwersja całych projektów, własny emiter .lbr. |
| makerspace-darmstadt/fusion-lcsc-library-manager | brak | Łączy części LCSC w jeden .lbr, autor sam pisze, że nie zwalidowany we Fusionie. |
| SnapEDA.bundle (lokalny) | komercyjny | Wzorzec integracji: add-in plus zewnętrzna aplikacja, `Electron.run`, protokół `fusion360://`. |
| DataBitTech/Fusion-EEToolBox | MIT | Add-in w przestrzeni Electronics z paletami, dobry wzorzec struktury. |

Nie znaleziono istniejącego add-inu Fusiona z paletą wyszukiwania EasyEDA, który dodaje części bezpośrednio. Nisza jest wolna, ale kilka CLI robi pół drogi.

## 5. Ryzyka

1. Prawne i ToS (najważniejsze):
   - Regulamin EasyEDA §VII (sprawdzony dosłownie 2026-10-11, https://easyeda.com/legal): "you shall not, directly or through any third party, use any robot, spider, scraper, crawler, script or other automated means to access, scrape, download, copy, index or mirror any Content ... in a bulk or systematic manner" oraz zakaz użycia tak zdobytej treści do text and data mining, uczenia maszynowego i trenowania modeli AI. Zakaz dotyczy trybu masowego i systematycznego. Pojedyncze, inicjowane przez użytkownika pobranie jednej części z cache i rate limitem leży poza dosłownym brzmieniem, ale to jest interpretacja, nie gwarancja.
   - §VII dozwala pobieranie "for personal, informational, and noncommercial purposes only" i zabrania "create derivative works from" oraz redystrybucji. §V mówi o "internal business or personal purposes". Te sformułowania są niespójne.
   - robots.txt: easyeda.com ma `Disallow: /api/`, wmsc.lcsc.com ma `Disallow: /`, jlcpcb.com ma `Disallow: /api`. Wszystkie nasze endpointy (w tym wyszukiwarka JLCPCB) są pod tymi zakazami.
   - Licencja osadzona w każdym `dataStr.head.license` (dosłownie, wersja angielska): prawa autorskie do oficjalnej biblioteki (parametry, symbole, footprinty, modele 3D) należą do JLCEDA/EasyEDA. Zakres: "Any AI tool". Wymóg: przy cytowaniu, wywoływaniu, kopiowaniu, wyprowadzaniu lub dystrybucji treści trzeba wyraźnie wskazać źródło "JLCEDA/EasyEDA Official Library" wraz z linkami https://lceda.cn/ i https://easyeda.com i nie wolno tej informacji usuwać ani ukrywać. Komercyjne użycie dozwolone "when used in JLCEDA/EasyEDA", czyli użycie wygenerowanego .lbr w komercyjnym projekcie we Fusionie nie jest tą licencją wprost objęte.
   - Warunki LCSC nie zostały odczytane (strona zwróciła stronę główną).
   - Działania: atrybucja w opisie każdej biblioteki, atrybucji części i w README, zachowanie `license` w fixture'ach, rate limit i cache, opisowy UA z adresem repo, README z jasnym zastrzeżeniem (narzędzie osobiste, użytkownik odpowiada za zgodność z warunkami, brak redystrybucji danych), brak wbudowanych danych EasyEDA w paczce add-inu. Decyzja o fixture'ach w repo jest poniżej w sekcji 8. To nie jest porada prawna, przed publiczną dystrybucją potrzebna jest ludzka lektura warunków.
2. Licencja kodu referencyjnego: easyeda2kicad to AGPL-3.0. Pisanie parsera w stylu "clean room" z formatu i żywych JSON-ów (D-001). Zwiększa to koszt, ale pozwala wybrać licencję projektu (rekomendacja MIT).
3. Niestabilne, nieoficjalne endpointy. Zmiany WAF (blokada UA, limity per ścieżka) mogą pojawić się bez ostrzeżenia. Retry, backoff, cache, czytelne wyjątki.
4. Import do Fusiona: nikt nie zwalidował wygenerowanego .lbr na buildzie 2705. Komendy tekstowe są nieudokumentowane i mogą się zmienić między buildami. Błędy `Electron.run` są ciche.
5. Niepotwierdzone reguły konwersji (znak `curve`, rotacja pinów i padów, roundness, tekst). Każda z nich ma test geometrii plus render, ale ostateczny dowód to oko użytkownika we Fusionie.
6. macOS: SSL w Pythonie Fusiona, `fetch`, `localStorage` w palecie. Brak danych.
7. Limit 10 edytowalnych dokumentów na kontach osobistych [ANEC]. Preferować jedną narastającą bibliotekę.
8. Pady POLYGON, sloty, nieprostokątne pady przelotowe, ekspansja maski per pad: stratne lub niereprezentowalne. Ostrzeżenia w wyniku konwersji.

## 6. Proponowana architektura

```
lcsc2fusion/
  model.py          frozen dataclasses: Part, Symbol, Pin, Package, Pad, Hole, Wire, Arc, Circle, Rect, Polygon, Text, Gate
  constants.py      jednostki, tabela warstw EasyEDA na Eagle, tablica 46 warstw Eagle, mapowanie typów pinów
  errors.py         wyjątki: NetworkError, RateLimitedError, PartNotFoundError, ParseError, ConversionError
  client/           http.py (urllib, UA, gzip, timeout), cache.py (dysk, TTL), ratelimit.py, retry.py, easyeda.py (fetch), search.py (JLCPCB + id)
  parser/           shapes.py (tokenizacja ~ i ^^), symbol.py, footprint.py, svgpath.py (łuki), units.py (JEDYNE miejsce konwersji jednostek i osi Y)
  lbr_writer/       writer.py (model na ElementTree), layers.py, lint.py
  cli/              __main__.py (argparse, bez zależności)
  render.py         footprint na SVG/PNG (matplotlib, zależność tylko dev)
addin/LCSC2Fusion/  .manifest, LCSC2Fusion.py (run/stop), palette/ (HTML, JS), fusion_adapter.py (jedyny import adsk), controller.py (czysta logika zdarzeń palety)
scripts/build_addin.py   kopiuje lcsc2fusion/ do paczki add-inu (core jest vendorowany, bo Fusion nie ma pip)
tests/              fixtures/ (JSON), golden/ (.lbr), test_parser, test_writer, test_geometry, test_client (offline, mock transportu), test_addin_controller (stuby adsk po ścieżce z env ADSK_DEFS)
docs/               RESEARCH.md, MANUAL_TEST.md
```

Zasady przekrojowe:

- Runtime tylko stdlib. Dev: pytest, ruff, mypy, matplotlib.
- Konwersja jednostek i osi Y wyłącznie w `parser/units.py`, model zawsze w mm, Y w górę.
- Tabela warstw w `constants.py`, żadnych magicznych liczb.
- Wyjątki własne, bez gołego `except`.
- Core nie importuje `adsk`. `fusion_adapter.py` implementuje interfejs `LibraryImporter` z metodą `open_library(path)`, żeby wariant A/B/C był podmienialny.
- Python 3.11 do 3.14 (Fusion 3.14, lokalnie 3.13).

## 7. Plan Etapu 1 i brama weryfikacyjna

Brama 0 (zanim powstanie parser, wymaga Twojej ręki we Fusionie):

1. Ręcznie napisany minimalny rezystor 0603 .lbr (szablon z pełną tablicą 46 warstw, sekcja 2.2).
2. Ty importujesz go (File > Open > Open from my computer, potem Save) i potwierdzasz, że symbol, footprint i device są widoczne w Place Panelu.
3. Dodatkowo prosisz Fusiona o eksport dowolnej małej biblioteki do .lbr i wklejasz mi plik, żebym porównał nagłówek i atrybuty warstw z naszym szablonem (ryzyko `blackcolor`/`whitecolor`).

Dalej, w kolejności:

| Krok | Zawartość | Równoległość |
|-|-|-|
| 1.0 | `pyproject.toml`, venv, ruff, mypy, pytest, `model.py`, `constants.py`, `errors.py` | sekwencyjnie, to baza |
| 1.1 | `client/` z testami offline | równolegle |
| 1.2 | `parser/symbol.py` + `units.py` + `svgpath.py` | równolegle |
| 1.3 | `parser/footprint.py` | równolegle |
| 1.4 | `lbr_writer/` + `lint.py` + testy struktury | równolegle |
| 1.5 | `render.py` (SVG/PNG footprintu), oględziny z podglądem EasyEDA | po 1.3 |
| 1.6 | CLI, fixture'y dla zestawu referencyjnego, golden files, testy geometrii | po 1.2 do 1.4 |
| 1.7 | README, DECISIONS, lista ograniczeń; pytest, ruff, mypy zielone | koniec etapu |

Commit po każdym kroku w formacie `type: short description`.

Etap 2 (po zielonym Etapie 1): szkielet add-inu, paleta, `controller.py` z testami na stubach, `docs/MANUAL_TEST.md` (krótka lista kontrolna), wariant C plus A.

## 8. Otwarte pytania do Ciebie

1. Fixture'y w repo: realne odpowiedzi EasyEDA są wymagane przez testy, ale to treść objęta prawem autorskim EasyEDA, a repo ma być publiczne. Opcje: (a) commitować przycięte realne fixture'y z nienaruszoną licencją i atrybucją, (b) trzymać realne fixture'y lokalnie (gitignore) i commitować tylko własnoręcznie napisane, syntetyczne w tym samym formacie. Rekomendacja: (b) dla repo publicznego, z testami przeciwko realnym lokalnie. Do decyzji.
2. Licencja projektu: MIT przy parserze clean room (rekomendacja), albo AGPL-3.0 przy pochodnym od easyeda2kicad.
3. Czy wykonasz Bramę 0 we Fusionie przed rozbudową parsera (zgodnie z CLAUDE.md).

## 9. Addendum z implementacji (2026-10-11)

Co implementacja potwierdziła lub skorygowała względem sekcji 1 do 8:

- Znak rotacji (poprawka do 1.9): dodatni kąt w EasyEDA to obrót przeciwnie do ruchu wskazówek
  zegara na ekranie, więc po odwróceniu osi Y kąt pozostaje bez zmian. Dowód: kolejność wierzchołków
  w polu `points` dla 50 z 50 padów RECT obróconych o 90 lub 270 stopni w C2040. Kąty ukośne nie
  występują w realnych danych (D-015).
- Rotacja pinów symbolu `angle = (ee_rotation + 180) % 360` potwierdzona porównaniem renderu z
  podglądem EasyEDA dla C2040.
- Porównanie wizualne (render z naszego modelu kontra obraz PNG z EasyEDA) dla C2040 (QFN z padem
  termicznym), C165948 (USB-C z padami wielokątnymi, slotami i otworami), C2913202 (moduł z
  dziewięcioma padami o numerze 41) i symbolu RP2040: liczba i położenie padów, kształt silkscreenu,
  strony i kolejność pinów zgadzają się. Różnica kosmetyczna: warstwa ComponentMarkingLayer (101)
  jest u nas na silkscreenie (21), w EasyEDA rysowana na biało.
- `wmsc.lcsc.com/ftps/wm/product/detail` odpowiada 200 także dla naszego opisowego User-Agenta
  (w badaniach 403 dostawały UA pythonowe). Używane tylko jako best effort do adresu datasheetu.
- Wyszukiwanie po numerze LCSC bez danych EasyEDA kończy się `PartNotFoundError`, a nie pustą listą.
- Pozycje pinów leżą na siatce 1,27 mm, nie 2,54 mm (D-016).
- Wyjście zaokrąglamy do 0,1 mikrona (`MM_DECIMALS = 4`), żeby usunąć szum typu 0.850011.
- Atrybuty technologii `PACKAGE` i `VALUE` kolidują z wbudowanymi atrybutami Eagle, więc zapisujemy
  `LCSC_PACKAGE` i `LCSC_VALUE`.
- Pady niemetalizowane (`plated = N`) nie występują w 24 realnych częściach, a Eagle ich nie ma. Są
  zapisywane jako zwykłe pady metalizowane.
- Open library: otwarcie .lbr uruchamiamy tylko z handlera `incomingFromHTML` palety (przycisk),
  nigdy automatycznie ze zdarzenia niestandardowego.
- Brama 0 przeszła (D-017). Eksport z Fusiona ma nagłówek 9.7.0 i warstwy w nowym stylu
  (`blackcolor`, `whitecolor`, `coloredcolor`, `*highlight`), ale starszy zapis (`color`, `fill`)
  też jest przyjmowany.
- Natywne sloty (D-018): `<pad shape="slot" drill slotLength rot>`, opcjonalnie `diameter` i
  `solderMask`. To poprawka do sekcji 2.4 (slot jako wiercenie plus frezowanie).
- Znak `curve` potwierdzony na realnych danych Fusiona (24 z 24 łuków).

## 10. Addendum: modele 3D (2026-10-12)

Dwa niezależne researche (dane EasyEDA i workflow Fusiona) oraz plan Etapu 3 (D-020):

- Uuid modelu to `SVGNODE.attrs.uuid` w footprincie (to samo co `step` z `searchByNumbers`). Pole
  `head.uuid_3d` jest inne i nie służy do pobierania. STEP: `https://modules.easyeda.com/qAxj6KHrDKw4blvCG8QJPs7Y/{uuid}`,
  OBJ: `https://modules.easyeda.com/3dmodel/{uuid}`. 23 z 24 realnych części ma model (brak: C476261).
- STEP z EasyEDA: AP214 z SolidWorks 2020, milimetry, jedna PRODUCT, bez drzewa montażu, wiele brył,
  kolory na ścianę, 1 do 3 MB, nazwy z bajtami GBK, CRLF. Bbox z `VERTEX_POINT` zgadza się z OBJ do 0,01 mm,
  bbox ze wszystkich `CARTESIAN_POINT` jest błędny.
- Transformacja do układu paczki (potwierdzona na C25804, C2040, C165948, C46749): obrót modelu wokół
  środka jego bbox o `c_rotation[2]` (dodatni przeciwnie do zegara), potem przesunięcie do
  `(c_origin - head) * 0,254 mm` (Y odwrócone) i podniesienie tak, by najniższy punkt leżał na
  `z * 0,254 mm`. Zwykłe dodanie `z` jest błędne. `c_origin` jest nieaktualne w C1035, C14663, C6186,
  C20526, wtedy bierzemy środek konturu SVGNODE.
- Fusion: model jest osobnym projektem w Hubie, w `.lbr` tylko referencja (`<package3d type="model"
  urn="" wip_urn="urn:adsk.wipprod:fs.file:vf.XXX?version=N" locally_modified="yes">` i
  `<package3dinstance package3d_urn=...>` w device). Oficjalne kroki ręczne: Content Manager >
  Footprints > prawy przycisk na footprincie > Attach Existing 3D Model > Select from my computer.
  `Electron.Create3DPackage "<lbr>"` istnieje w zainstalowanym Fusionie 2705.1.30 i wymaga aktywnego
  dokumentu biblioteki, ale nie importuje STEP. SnapEDA następnie importuje STEP przez API i obraca
  model o 90 stopni wokół X (stąd wariant Y-up w Bramie 3).
- Licencja w JSON-ie: "3D models" są wprost własnością JLCEDA/EasyEDA, użycie komercyjne tylko w EasyEDA,
  obowiązek atrybucji. Dlatego `--3d` jest opt-in, a pliki STEP nie trafiają do repo.
