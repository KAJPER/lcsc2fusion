# DECISIONS

Format: kontekst, wybór, powód. Status: ACCEPTED (przyjęte do planu), PENDING (czeka na decyzję użytkownika).

## D-001 Parser clean room, bez kodu z easyeda2kicad, licencja MIT (ACCEPTED)

- Kontekst: easyeda2kicad (referencyjny parser) jest na AGPL-3.0. Projekty easyeda2eagle kopiują go prawie bez zmian i dziedziczą AGPL.
- Wybór: parser piszemy od zera na podstawie formatu, dokumentacji EasyEDA i żywych JSON-ów. easyeda2kicad służy tylko do porównania zachowania.
- Powód: pozwala wybrać licencję MIT dla całego projektu (zaakceptowane przez użytkownika). Koszt: więcej własnej pracy przy łukach, polygonach i pinach.

## D-002 Runtime tylko ze stdlib (ACCEPTED)

- Kontekst: Python Fusiona (3.14) nie ma `requests`, `certifi`, `lxml` w site-packages. Vendorowanie zależności komplikuje dystrybucję.
- Wybór: HTTP przez `urllib.request`, XML przez `xml.etree.ElementTree`, CLI przez `argparse`, model przez `dataclasses`. Zależności tylko dev: pytest, ruff, mypy, matplotlib.
- Powód: zero zależności runtime, add-in to zwykły folder z kodem.

## D-003 Pobieranie i wyszukiwanie (ACCEPTED)

- Kontekst: `/api/products/{id}/components` jest rate limitowany (CloudFront 403 po około 20 żądaniach w 100 s), inne endpointy nie. LCSC global search jest zablokowany dla klientów nie-przeglądarkowych.
- Wybór: id `^C\d+$` idzie przez `searchByNumbers` a potem `/api/components/{uuid}`, `/products/{id}/components` jako fallback. Fraza idzie do wyszukiwarki JLCPCB (wpisy "JLCPCB Assembly" odfiltrowane), EasyEDA Std search jako zapasowa. Własny UA, współbieżność 1, cache na dysku z TTL, backoff 5 minut po 403.
- Powód: omija znany limit, najmniej requestów, najstabilniejszy format wyników.

## D-004 Własny zapis .lbr i własny linter zamiast walidacji DTD (ACCEPTED)

- Kontekst: `eagle.dtd` 9.6.2 odrzuca prawdziwe pliki zapisane przez Fusiona. Licencja DTD (CC BY-ND) zabrania dystrybucji zmienionej wersji. Biblioteki Pythona do .lbr są martwe.
- Wybór: własne dataclasses plus `xml.etree`, nagłówek XML i DOCTYPE doklejane ręcznie, linter z listą reguł w RESEARCH.md 2.6.
- Powód: kontrola nad formatem i brak zależności.

## D-005 Nagłówek `version="9.6.2"` i pełna tablica 46 warstw (ACCEPTED, do weryfikacji Bramą 0)

- Kontekst: taki zestaw zapisuje Fusion i SnapEDA. Brak dowodu, że pusta lub częściowa lista działa.
- Wybór: zawsze deklarujemy 46 warstw, atrybuty bez `blackcolor` itp. do czasu porównania z plikiem wyeksportowanym z aktualnego Fusiona.
- Powód: najmniejsze ryzyko odrzucenia przy imporcie.

## D-006 Przepływ "Dodaj do biblioteki": C gwarantowany plus A (ACCEPTED, nieprzetestowane)

- Kontekst: API Electronics jest tylko do odczytu. Jedyny udokumentowany import to ręczne File > Open lub Data Panel > Upload. `Document.newDesignFromLocal` i `Electron.run` działają w cudzych add-inach, ale nie są udokumentowane.
- Wybór: v1 zapisuje .lbr i pokazuje instrukcję (C), a przycisk "Otwórz w Fusion Electronics" woła `Document.newDesignFromLocal` (A) z fallbackiem do C. Wariant B (scalanie do jednej biblioteki przez `Use`/`Copy`) po potwierdzeniu A.
- Powód: C zawsze działa, A daje wygodę bez opierania się na nieudokumentowanych funkcjach jako jedynej ścieżce. Logika Fusiona za interfejsem `LibraryImporter`.

## D-007 Symbole multi-unit jako osobne gate'y (ACCEPTED)

- Kontekst: części typu TL072 mają jednostki w `result.subparts[]`. Spłaszczenie wymagałoby własnego rozmieszczenia pinów.
- Wybór: jeden `symbol` i jeden `gate` na jednostkę, `connect` per gate.
- Powód: to naturalny model Eagle, prostszy niż spłaszczanie.

## D-008 Powtórzone numery padów (ACCEPTED)

- Kontekst: ESP32-S3-WROOM ma 9 padów o numerze 41. Nazwy padów w Eagle muszą być unikalne.
- Wybór: `41`, `41_2`, … i jedna lista `pad="41 41_2 …"` w `connect`.
- Powód: składnia listy potwierdzona w pliku zapisanym przez Fusiona. Pin count symbolu liczymy względem unikalnych numerów.

## D-009 Core jako kopia w paczce add-inu (ACCEPTED)

- Kontekst: Fusion nie ma pip, add-in to samodzielny folder.
- Wybór: `scripts/build_addin.py` kopiuje `lcsc2fusion/` do folderu add-inu. W repo nie ma duplikatu kodu.
- Powód: jedno źródło prawdy, zero zależności.

## D-010 Testy add-inu na oficjalnych stubach adsk po ścieżce z env (ACCEPTED)

- Kontekst: stuby leżą w `%APPDATA%\Autodesk\Autodesk Fusion 360\API\Python\defs`, licencja CC BY-NC-SA zabrania kopiowania do repo. `MagicMock` psuje dziedziczenie po handlerach.
- Wybór: zmienna `ADSK_DEFS` wskazuje stuby, testy add-inu pomijane (skip) gdy jej brak. Logika palety siedzi w `controller.py` bez `adsk`, więc większość testów nie potrzebuje stubów.
- Powód: zgodność licencyjna plus realne klasy bazowe.

## D-011 Courtyard generowany, nie importowany (ACCEPTED)

- Kontekst: EasyEDA nie ma warstwy courtyard, ma `ComponentShapeLayer` (99) jako kształt korpusu.
- Wybór: 99 i 100 idą na tDocu (51), a courtyard na tKeepout (39) liczymy z bbox padów i korpusu plus margines.
- Powód: poprawna semantyka warstw w Eagle.

## D-012 Atrybucja źródła w każdym .lbr (ACCEPTED)

- Kontekst: licencja w `dataStr.head.license` wymaga wskazania źródła "JLCEDA/EasyEDA Official Library" z linkami https://lceda.cn/ i https://easyeda.com i zakazuje jej usuwania.
- Wybór: tekst atrybucji w `<description>` biblioteki, w opisie deviceset i w atrybucie technologii. Pole `license` zachowywane w fixture'ach.
- Powód: spełnienie warunku licencji, zero kosztu.

## D-013 Fixture'y: syntetyczne w repo, realne tylko lokalnie (ACCEPTED)

- Kontekst: testy offline wymagają JSON-ów w formacie EasyEDA, repo ma być publiczne, treść jest chroniona prawem autorskim EasyEDA (RESEARCH.md sekcja 5).
- Wybór: do repo trafiają tylko syntetyczne fixture'y generowane przez `tests/fixtures/synthetic/generate.py` w formacie EasyEDA, razem z niezależnie policzonymi wartościami oczekiwanymi. Realne odpowiedzi leżą w `tests/fixtures/real/` (gitignore), a testy oznaczone `real` pomijają się, gdy katalogu brak.
- Powód: nie redystrybuujemy treści chronionej prawem autorskim EasyEDA, a testy geometrii dalej porównują wynik ze źródłem (z wartościami policzonymi z parametrów generatora).

## D-014 Thermal pad jako zwykły pad SMD w v1 (ACCEPTED)

- Kontekst: plik DRV8833 zapisany przez Fusiona obsługuje thermal pad przez `stop="no" cream="no"` plus ręczne prostokąty na warstwach 29 i 31. Wykrywanie, który pad jest termiczny, jest heurystyką.
- Wybór: w v1 każdy pad SMD (także duży pad centralny) zapisujemy jako zwykły `smd`. Maska i pasta wynikają z reguł DRC Fusiona.
- Powód: poprawność geometrii i prostota. Optymalizacja pasty pod pad termiczny to zadanie na później, zapisane w znanych ograniczeniach.

## D-015 Znak rotacji EasyEDA: dodatni to przeciwnie do ruchu wskazówek zegara (ACCEPTED, częściowo zweryfikowane)

- Kontekst: znak rotacji padów i tekstu nie jest udokumentowany. Wstępne założenie (dodatni zgodnie z zegarem) okazało się błędne.
- Wybór: `ee_rotation_to_ccw(d) = d % 360`. Dowód: w C2040 dla wszystkich 50 padów RECT obróconych o 90 lub 270 stopni kolejność wierzchołków w polu `points` zgadza się z obrotem przeciwnym do zegara, a z zegarem w żadnym. Rotacja pinów symbolu: `angle = (ee_rotation + 180) % 360`.
- Powód: dane źródłowe. Ryzyko: realne dane zawierają tylko kąty 0, 90, 180, 270, więc kąty ukośne są wnioskowane. Test syntetyczny z padem 30 stopni dokumentuje przyjętą konwencję, a ostateczne potwierdzenie to oko we Fusionie.

## D-016 Siatka pinów symbolu nie jest wymuszana (ACCEPTED, znane ograniczenie)

- Kontekst: pozycje pinów w EasyEDA leżą na siatce 1,27 mm (5 jednostek), a Eagle domyślnie pracuje na 2,54 mm. Tylko część symboli ma wszystkie piny na siatce 2,54 mm. Przyciągnięcie pojedynczych pinów groziłoby kolizjami (rozstaw 2,54 mm) i zniekształceniem symbolu.
- Wybór: pozycje pinów zachowujemy dokładnie ze źródła, początek symbolu to środek BBox przyciągnięty do siatki 5 jednostek.
- Powód: wierność geometrii. Konsekwencja: we Fusionie mogą pojawić się ostrzeżenia ERC "off grid" dla części symboli, a podłączanie przewodów wymaga mniejszej siatki. Do zmierzenia w MANUAL_TEST i ewentualnej optymalizacji fazy początku układu.

## D-017 Brama 0 przeszła, format zgodny z eksportem Fusiona (ACCEPTED, 2026-10-12)

- Kontekst: użytkownik zaimportował `docs/gate0_resistor.lbr` do Fusiona (zrzuty: Content Manager z device, footprint z padami, maską i pastą, symbol z pinami) i dostarczył eksport z Fusiona `Connector_USB.lbr` (nagłówek 9.7.0, 62 warstwy w nowym stylu `blackcolor`, `whitecolor`, `coloredcolor`, sekcje `atts` i `packages3d`).
- Wniosek: Fusion akceptuje nasz nagłówek 9.6.2 i starszy zapis warstw (`color`, `fill`), więc zostajemy przy nim jako zweryfikowanym. Na eksporcie Fusiona potwierdzono też znak `curve` (24 z 24 łuków spójne z konwencją "dodatni = przeciwnie do zegara") i listy padów w `connect` (`pad="SH1 SH2"`).
- Linter został poprawiony tak, żeby przepuszczał ten plik bez uwag (nowy styl warstw, `atts`, `shape="slot"`). Plik jest cudzą treścią, więc leży lokalnie w `docs/test_library/` (gitignore).

## D-018 Sloty jako natywne pady `shape="slot"` (ACCEPTED, do potwierdzenia w Bramie 1)

- Kontekst: Fusion 9.7 zapisuje sloty natywnie (`<pad drill="0.7" slotLength="1.2" shape="slot" rot="R90">`), zamiast frezowania na warstwie 46, które było naszym obejściem.
- Wybór: domyślnie `native_slots=True`. Biblioteka ze slotami dostaje nagłówek 9.7.0, bez slotów zostaje 9.6.2. `native_slots=False` daje stare frezowanie. `drill` to szerokość slotu, `slotLength` długość całkowita (zgodnie z EasyEDA `hole_length`), `rot` to kierunek osi długiej.
- Powód: to dokładnie forma zapisywana przez Fusiona. Ryzyko: semantyka `slotLength` (całkowita czy między środkami) nie jest udokumentowana. Brama 1 importuje obie wersje osobno.

## D-019 Auto-dopasowanie korpusu symbolu do nazw pinów (ACCEPTED, kalibracja do potwierdzenia)

- Kontekst: w Bramie 1 nazwy pinów rysowane przez Eagle wewnątrz korpusu nakładały się (np. SOIC-8, USB-C, QFN). Źródłowe korpusy EasyEDA są dobrane do jego drobnej czcionki. Eagle zaczyna nazwę pinu około 2,3 mm w głąb korpusu, a znak ma około 1,35 mm szerokości (pomiar na zrzucie użytkownika, korpus 10,16 mm i piny 2,54 mm dają skalę).
- Wybór: `parser/symbol_fit.py` powiększa prostokątny korpus (tylko w górę, w krokach 2,54 mm, piny jadą razem z krawędzią), gdy nazwy przeciwległych pinów w tym samym wierszu lub kolumnie się nie mieszczą. Pomija symbole o 2 pinach lub mniej (writer ukrywa im nazwy) i korpusy nieprostokątne. Stała kalibracji są na górze modułu. `parse_part(fit_symbols=False)` wyłącza to dla testów wierności względem źródła.
- Powód: czytelność we Fusionie bez zmiany pozycji pinów wzdłuż krawędzi. Z 24 realnych części zmieniają się 3 (kryształ C13738, USB-C C165948, NE555DR C7593). D-016 zostaje prawdziwe, bo wzrost jest w krokach siatki 2,54 mm.

## D-020 Modele 3D: opt-in, STEP z wypieczoną transformacją, ręczne podłączenie (ACCEPTED, 2026-10-12)

- Kontekst: model 3D nie jest częścią `.lbr`. Fusion trzyma go jako osobny projekt w Hubie, a `.lbr` wskazuje na niego przez `wip_urn` utworzony przez samego Fusiona (potwierdzone w 214 tagach z lokalnego cache Fusiona), więc nie da się go wygenerować offline. Model z EasyEDA nie jest wycentrowany na początku układu paczki, a `c_origin` bywa nieaktualne (4 z 23 części).
- Wybór: flaga `--3d` (domyślnie wyłączona) pobiera STEP spod uuid z `SVGNODE.attrs.uuid` i zapisuje obok `.lbr`: `<id>.step` (surowy), `<id>.placed.step` (ten sam model po transformacji do układu paczki) i `<id>.3d.json` (placement, sumy kontrolne, atrybucja). Transformację `q_xy = Rz(rz) * (p_xy - c) + (ox, oy)`, `q_z = p_z - zmin + zb` wypiekamy własnym kodem w plikach STEP (`step.py`, tylko 3D `CARTESIAN_POINT` i `DIRECTION`, reszta bajtów bez zmian), bo plik nie ma drzewa montażu. Podłączenie do footprintu robi użytkownik oficjalną ścieżką (Content Manager > Footprints > Attach Existing 3D Model), add-in pokazuje kroki.
- Powód: to jedyna ścieżka w pełni potwierdzona dokumentacją. Opt-in, bo to dodatkowe pobranie treści chronionej licencją (licencja w JSON-ie wprost wymienia "3D models"), z obowiązkiem atrybucji i bez redystrybucji.
- Odrzucone na teraz: zapis `package3d`/`wip_urn` do `.lbr` (niewykonalny offline) i automatyzacja `Electron.Create3DPackage` w stylu SnapEDA (nieudokumentowana, niepotwierdzona synchroniczność i kontekst wywołania). Wrócimy do niej po Bramie 3.
- Wynik Bramy 3 (który wariant osi i początku Fusion przyjmuje) zapiszemy jako D-021.

## D-021 Brama 3 przeszła: `placed.step` (Z w górę) jest poprawny (ACCEPTED, 2026-10-12)

- Kontekst: dla C25804 (rezystor 0603, `rz = 90`), C2040 (QFN-56) i C165948 (USB-C, `rz = 180`, `z = -0,85 mm`, przesunięcie -1,1 mm) użytkownik podłączył do footprintu we Fusionie trzy warianty STEP przez Attach Existing 3D Model.
- Wynik: `<id>.placed.step` leży poprawnie na footprincie dla wszystkich trzech części (położenie, obrót, wysokość). Wariant `placed_yup` (obrócony o 90 stopni wokół X) kładzie model na boku. Surowy STEP jest obrócony o 90 stopni względem footprintu (C25804).
- Wniosek: Fusion przyjmuje model w układzie "Z w górę, mm, początek w początku footprintu, spód na płytce", bez konwersji osi. Wzór transformacji z D-020 (obrót `rz` wokół środka bbox, przesunięcie `c_origin`, podniesienie do `z`) jest potwierdzony we Fusionie, także znak rotacji i reguła `zb - zmin` dla modeli z `z != 0`.
- Zmiany: wariant Y-up i skrypt paczki testowej usunięte (`rotate_x`, `scripts/make_3d_test_pack.py`), bo służyły tylko do tego eksperymentu. Zwykłe `--3d` wystarcza. Automatyzacja w stylu SnapEDA (D-020, "odrzucone na teraz") zostaje opcją na życzenie.

## D-022 UI add-inu jako osobne okno przeglądarki z lokalnym serwerem (ACCEPTED, 2026-10-12, do potwierdzenia w Bramie 4)

- Kontekst: Fusion usuwa wszystkie palety API przy każdej zmianie środowiska (dokumentacja Palettes_UM i log: po `Document.newDesignFromLocal` wpis `the palette does not exist`), a add-in dalej działa. Przycisk był tylko w Design, więc w Electronics nie dało się go otworzyć ponownie. SnapEDA rozwiązuje to osobnym procesem Electron (111 MB) i `fusion360://`.
- Wybór: serwer HTTP w add-inie (stdlib, 127.0.0.1, port efemeryczny, token startowy zamieniany na ciasteczko HttpOnly SameSite=Strict, sprawdzanie Host/Origin/CSRF, CSP) oraz okno Edge lub Chrome w trybie `--app` z własnym profilem (fallback: domyślna przeglądarka). Strona to to samo `index.html` i `app.js`, przez `http-bridge.js` (POST `/api/act`, SSE `/api/events`). Wywołania `adsk` zawsze na głównym wątku przez własne zdarzenie niestandardowe (`MainThreadDispatcher`). Przycisk we wszystkich środowiskach Electronics (`TARGETS`), `runOnStartup: true`. `controller.py` bez zmian.
- Powód: brak zależności i instalacji, zgodne z regułą "tylko stdlib", okno niezależne od cyklu życia palet. Prototyp i testy: odmowy bezpieczeństwa, SSE z odtwarzaniem, `--app` w Edge i Chrome, zamykanie okna z serwera.
- Znaleziska z implementacji: zmienna środowiskowa `__COMPAT_LAYER` powoduje relaunch Edge i utratę uchwytu procesu, więc launcher ją usuwa; serwer nie odtwarza zdarzeń dla świeżego połączenia, więc strona czeka na otwarcie strumienia przed pierwszym żądaniem i po awarii strumienia wysyła `ready` ponownie.
- Ryzyka do rozstrzygnięcia w Bramie 4: komenda tekstowa `Document.newDesignFromLocal` w handlerze zdarzenia niestandardowego (plan awaryjny: `os.startfile("fusion360://host/?command=open&file=...")`), identyfikatory paneli przycisków (zwłaszcza `PCBDesignEnvironement`), późno ładowane środowiska. Tryb `LCSC2FUSION_UI=palette` jest tymczasowy i zostanie usunięty razem z D-023 (wynik Bramy 4).

## D-024 Zbiorcza biblioteka w Hubie i automatyczne 3D jak SnapEDA (ACCEPTED, 2026-10-13, do potwierdzenia w Bramie 5)

- Kontekst: użytkownik nie chce osobnego `.lbr` per część, tylko jedną bibliotekę w Fusionie i model 3D od razu ustawiony w edytorze pakietu do zatwierdzenia. Zainstalowany SnapEDA robi dokładnie to na tym buildzie (jego "SnapEDA Library" ma 57 wersji w Hubie).
- Wybór: biblioteka "LCSC2Fusion Library" w katalogu głównym aktywnego projektu Hubu. Pierwsza część: `Document.newDesignFromLocal` plus `saveAs`. Kolejne: `documents.open`, `Electron.run "Use '<part.lbr>'"`, `Copy "<DEVICESET>.dev@<plik>"`, `Use "-<plik>"`, `document.save`. 3D: `Electron.Create3DPackage "<part.lbr>"`, `saveAs`, import `placed.step` przez `importToTarget2`, obrót jak SnapEDA ((x,y,z) na (x,-z,y)), a użytkownik tylko sprawdza ustawienie i klika Finish, Save, OK. Nigdy nie zamykamy aktywnego dokumentu użytkownika.
- Rejestr `%LOCALAPPDATA%\LCSC2Fusion\state.json` (id biblioteki, dodane części, odciski pakietów) chroni przed duplikatami i przed kolizją nazw pakietów (ta sama nazwa, inna geometria dostaje sufiks z odcisku przed zapisem `.lbr`). Pliki `.lbr` per część zostają w `~/LCSC2Fusion/libraries/` jako źródło scalania.
- Praca idzie na głównym wątku Fusiona w tle (`MainThreadDispatcher.post`), bo trwa około 45 s, a żądanie HTTP ma 30 s. Postęp i wynik wracają komunikatami `status` i `fusion_done`.
- Ryzyka do rozstrzygnięcia w Bramie 5: sekwencja `saveAs`, `importToTarget2` i komend tekstowych ze zdarzenia niestandardowego (SnapEDA używa `openedFromURL`), zachowanie `COPY` przy istniejących nazwach (możliwy blokujący dialog), dopasowanie modelu po obrocie SnapEDA dla naszego `placed.step`, rozszerzenie `flbr` pliku biblioteki.

## D-025 Własna ikona okna (ACCEPTED, 2026-10-13)

- Kontekst: okno Edge w trybie `--app` miało domyślny globus, bo serwer nie podawał favikony (204).
- Wybór: ikona z projektu przycisku w Fusionie (niebieski kwadrat, biały układ scalony, złote nóżki), generowana skryptem `scripts/make_icons.py` (Pillow, narzędzie deweloperskie) do `palette/favicon.ico` (16 do 256 px, małe rozmiary rysowane ręcznie na siatce pikseli), `icon-192.png`, `icon-512.png` i `manifest.webmanifest`, oraz te same PNG dla przycisku w Fusionie. Serwer podaje je z tymi samymi kontrolami (Host, ciasteczko, Origin), link do manifestu ma `crossorigin="use-credentials"`, bo inaczej przeglądarka nie wyśle ciasteczka.
- Niepewne: czy pasek zadań Windows weźmie ikonę z manifestu (sprawdza użytkownik, plan awaryjny: skrót `.lnk` z własną ikoną).

## D-026 Brama 5, pierwszy przebieg: co działa, poprawka obrotu, błąd zapisu do obserwacji (2026-10-13)

- Potwierdzone w logu Fusiona 2705 (część C25804): `find_library` bez wyniku, `Document.newDesignFromLocal` (4,2 s), `saveAs` jako "LCSC2Fusion Library" (rozszerzenie pliku `flbr`, wersja v1 w Hubie po około 7 s, biblioteka zarejestrowana w Library Managerze), `Electron.Create3DPackage` ("Command is executed successfully."), `saveAs` pakietu 3D ("R0603 v1", 20 s), import STEP przez `importToTarget2` (około 53 s, Fusion zgłosił "Hang detected", ale się nie zawiesił), wpis w `state.json`. Całość z handlera zdarzenia niestandardowego, więc to ryzyko z D-024 jest zdjęte dla `saveAs`, `Create3DPackage` i `importToTarget2`.
- Błąd: model leżał na boku. Przyczyna: obrót jak u SnapEDA ((x,y,z) na (x,-z,y)) kompensuje ICH pliki STEP, a nasz `placed.step` ma już Z w górę i początek w początku footprintu (ręczne podłączenie w Bramie 3 działało bez obrotu). Poprawka: `STEP_IMPORT_TURN_RAD = 0.0`, import zostawiamy bez transformacji (obrót SnapEDA zostaje jako nazwana stała `SNAPEDA_TURN_RAD`).
- Do obserwacji: modalny błąd Fusiona "Can't rename ... .flbr.~~~ to ... .flbr.lbr, Error code: 5" (odmowa dostępu) pojawił się raz, około 70 s po pierwszym zapisie nowej biblioteki, w trakcie importu STEP (po zdarzeniach "WipContentAvailable" dla biblioteki). Wygląda na wyścig Fusiona między odświeżeniem lokalnej kopii świeżo wysłanej biblioteki a otwartym dokumentem Electronics. Nie występował w żadnym wcześniejszym logu (SnapEDA). Przepływ po nim kontynuował, a użytkownik zamknął okno błędu. Sprawdzamy, czy wraca przy kolejnych częściach (biblioteka już istnieje) i przy ponownym 3D dla tej samej części.
- Import STEP trwa około 53 s dla 1 MB pliku (rezystor 0603, 12 tysięcy encji). To czas Fusiona, nie naszego kodu.

## D-027 Brama 5 przeszła (2026-10-13)

- Użytkownik potwierdził po poprawce obrotu (D-026), że przepływ działa: zbiorcza "LCSC2Fusion Library" w Hubie, dodawanie kolejnych części do tej samej biblioteki i pakiet 3D otwierany z modelem do zatwierdzenia. Decyzje D-022, D-024 i D-025 są więc zweryfikowane we Fusionie (D-024 i D-025 poza dwiema obserwacjami niżej).
- Nie sprawdzone osobno i zostają do obserwacji: czy ikona okna pojawia się też na pasku zadań Windows (plan awaryjny: skrót `.lnk` z własną ikoną), czy sporadyczny błąd "Can't rename ... Error code: 5" z D-026 wraca przy kolejnych częściach.
- Tryb `LCSC2FUSION_UI=palette` (D-022) jest już zbędny i można go usunąć przy najbliższym porządkowaniu.

## D-028 Nowy wygląd okna i link do LCSC (ACCEPTED, 2026-10-13)

- Kontekst: okno dostało pierwszą wersję stylu bez spójnego systemu, a użytkownik chciał od razu przejść z wyniku do strony części.
- Wybór: styl "spokojny panel narzędzia" (jeden akcent w odcieniu ikony #1f5ea8, chłodne szarości, systemowa czcionka, jedna skala promieni 4/6/8 px i odstępów co 4 px, bez gradientów i cieni, ciemny i jasny motyw sprawdzone pod WCAG AA testem). Stany: szkielet wierszy podczas szukania, pusty wynik, błąd w ramce, stan zajętości. Ikon nie rysujemy ręcznie.
- Link: kontroler dodaje do wyniku `lcsc_url` z `lcsc2fusion.links.lcsc_product_url` (jedno miejsce, wyjątek dla niekanonicznego id). Strona i tak sprawdza adres (dokładny prefiks, `.html`, id zgodne z wierszem), buduje `<a>` samym DOM-em z `target=_blank` i `rel="noopener noreferrer"`. Link do karty danych tylko dla https i hostów lcsc.com, jlcpcb.com, easyeda.com (z poddomenami).
- Powód: dane pochodzą z sieci, więc każdy adres jest walidowany po stronie strony, mimo że kontroler już go zbudował.

## D-029 Ustawienia: motyw i język (ACCEPTED, 2026-10-13)

- Wybór: panel Ustawienia w oknie. Motyw: System, Jasny, Ciemny (`data-theme` na `<html>`, brak atrybutu to System). Język: English i Polski, domyślnie z `navigator.language`, zapisany wybór wygrywa.
- Zapis w `localStorage` okna (własny profil przeglądarki add-inu), klucze `lcsc2fusion.theme` i `lcsc2fusion.lang`, każdy dostęp w try/catch. Dotyka ich tylko `theme-init.js`, ładowany synchronicznie w `<head>` (CSP zabrania skryptów inline), dzięki czemu nie ma mignięcia złego motywu.
- Ciemne tokeny stoją w `style.css` dwa razy (media query i `data-theme=dark`) z testem zgodności, a nie w `light-dark()`, bo ta funkcja wymaga Chrome 123, a przeglądarka osadzona we Fuzionie bywa starsza.
- Protokół Pythona zostaje po angielsku. Teksty z kontrolera tłumaczy strona tabelą wzorców regex (141 wzorców w `i18n.js`), nieznany tekst przechodzi po angielsku. Test dryfu zbiera komunikaty z kodu i uruchomionego kontrolera i wymaga, żeby każdy miał wzorzec.
- Nowy język: słownik plus wzorce plus wpis na liście języków (opis w addin/README.md).

## D-030 Lokalne API add-inu dla MCP (ACCEPTED, 2026-10-13)

- Kontekst: inny Claude ma móc dodać część do Fusiona przez działający add-in.
- Wybór: trasy `/api/local/` na tym samym serwerze co okno: `GET ping`, `POST add`, `GET job/<n>`. Uwierzytelnienie tokenem Bearer (256 bit, nowy przy każdym starcie), ciasteczko okna tu nie działa, a token nie działa na trasach okna. Kolejność kontroli: Host, brak nagłówka Origin (przeglądarka zawsze go wysyła przy żądaniach między originami), token, metoda, wymagania POST. Nieznana trasa lokalna daje 404 dopiero po uwierzytelnieniu. Nieudane logowania logowane z limitem 20 na minutę, bez blokady, token nigdy w logu.
- Odkrywanie: add-in zapisuje `api.json` (url, token, pid) obok `state.json` po starcie serwera, a usuwa przy zatrzymaniu tylko wtedy, gdy plik nadal ma jego token. Wspólny moduł `lcsc2fusion/discovery.py`.
- Zadania: `JobTracker` obserwuje komunikaty kontrolera przez `TeeSink` (okno działa jak dotąd), jedno zadanie naraz, zajęty kontroler daje 409. Straż osieroconego zadania: dopiero gdy kontroler jest wolny i minęło 60 s.
- Ograniczenie: kliknięcie w oknie podczas zadania z API może je oznaczyć jako nieudane, bo oba kanały dzielą jeden strumień komunikatów. Zadanie rozpoczęte w oknie nie jest śledzone, ale liczy się do zajętości.

## D-031 Serwer MCP w samym stdlib (ACCEPTED, 2026-10-13)

- Wybór: własna implementacja MCP (JSON-RPC po stdio, linie), bez oficjalnego SDK. Powód: rdzeń nie ma zależności, protokół dla samych narzędzi jest mały, a SDK ciągnie pydantic i httpx. Obsługiwane wersje protokołu 2025-06-18, 2025-03-26, 2024-11-05.
- Narzędzia: `lcsc_search`, `lcsc_part_info`, `lcsc_convert`, `fusion_status`, `fusion_add_part`, `fusion_job`. Błędy oczekiwane (brak części, sieć, add-in nie działa) to wyniki z `isError`, błędy argumentów to -32602. Postęp przez `notifications/progress`.
- Bezpieczeństwo: klient add-inu używa `http.client` (bez przekierowań i proxy), adres musi być dokładnie `http://127.0.0.1:<port>`, token nigdy do logów ani do odpowiedzi, `out_dir` tylko absolutna ścieżka lokalna lub `~`, teksty z LCSC bez znaków sterujących.
- Rejestracja: `.mcp.json` w repozytorium (zakres projektu), opis rejestracji globalnej i dla Claude Desktop w docs/MCP.md.

## D-032 Nazwa pliku .lbr nigdy z dokumentu z sieci (ACCEPTED, 2026-10-13)

- Kontekst: przy przeglądzie MCP wyszło, że `convert_parts` brało nazwę pliku z pola `lcsc.number` pobranego dokumentu. Wartość typu `../../x` zapisałaby plik poza katalogiem wyjściowym (dotyczyło też CLI i add-inu).
- Wybór: pojedyncza część dostaje nazwę ze znormalizowanego, poproszonego id, a `library_name` musi być zwykłą nazwą pliku (bez separatorów, znaków zarezerwowanych Windows, kropki na końcu), inaczej `ConversionError`. Pakiet `mcp` nie trafia do paczki add-inu.

## D-034 Instalator dla Windows i budowa plików do release (ACCEPTED, 2026-10-14)

- Kontekst: dotąd add-in trzeba było zbudować skryptem Pythona i ręcznie rozpakować do `FusionAddins`. Zwykły użytkownik Fusiona nie ma Pythona.
- Wybór: instalator na użytkownika, bez administratora, bez sieci i bez Pythona: `Install-LCSC2Fusion.bat` plus `install.ps1` (Windows PowerShell 5.1), `uninstall.ps1`. Kopiuje add-in do `%APPDATA%\Autodesk\FusionAddins\LCSC2Fusion` (to miejsce potwierdził Fusion w bramach 0 do 5), a Fusion uruchamia go sam przy następnym starcie (`runOnStartup`). Instalacja jest atomowa (staging, odłożenie starej kopii, zamiana, wycofanie przy błędzie), sprawdza sumy SHA256 z `payload.sha256` (brak pliku spoza listy, brak niezgodnego hasha), nie rusza niczego poza własnym folderem ani stanu w `%LOCALAPPDATA%\LCSC2Fusion`. Odmawia dla ścieżek względnych, z `..` i dla dowiązań (junction, symlink). Dane użytkownika usuwa tylko `uninstall.ps1 -RemoveData` po potwierdzeniu.
- Jednoplikowy `LCSC2Fusion-Setup-<wersja>.exe` robi wbudowany w Windows IExpress (zawiera zip instalatora i `bootstrap.cmd`). Powód wyboru: zero zależności i zero instalowania narzędzi, działa na runnerze CI. Odrzucone: Inno Setup i NSIS (trzeba je instalować), PyInstaller (nowa zależność, częstsze fałszywe alarmy antywirusów). Ograniczenie: IExpress nie przekazuje kodu wyjścia instalatora (przy błędzie exe zwraca 0, wynik widać w oknie komunikatu i w logu `%TEMP%\LCSC2Fusion-setup.log`). Główna ścieżka pozostaje zip z plikiem `.bat`, exe jest wygodą.
- Pliki wydania (`scripts/build_release.py`): `LCSC2Fusion-<wersja>-addin.zip` (sam add-in, ręcznie i dla macOS), `...-installer.zip`, `LCSC2Fusion-Setup-<wersja>.exe`, `SHA256SUMS.txt`. Wersja musi być identyczna w `pyproject.toml`, `lcsc2fusion.__version__` i manifeście, a tag musi być równy `v<wersja>`. Pliki instalatora są zapisywane z CRLF (`.ps1` z BOM), niezależnie od ustawień gita (`.gitattributes`).
- Ryzyka: pliki nie są podpisane, więc SmartScreen pokaże "nieznany wydawca", a antywirusy mogą podejrzewać samorozpakowujące się exe (wzorzec: exe uruchamia `cmd`, potem `powershell -ExecutionPolicy Bypass`). Zip z `.bat` jest planem awaryjnym, README to mówi. Następny krok to podpis kodu (SignPath Foundation daje go darmowo projektom open source).
- Nie sprawdzone: ładowanie zainstalowanego add-inu w prawdziwym Fusionie (nie dotykaliśmy prawdziwego `FusionAddins`), Windows 10, inne języki interfejsu, OneDrive przekierowujący AppData, AppLocker i tryb ograniczony PowerShella.

## D-035 CI i publikacja release przez GitHub Actions (ACCEPTED, 2026-10-14)

- Wybór: `ci.yml` (ruff, mypy, pytest na ubuntu i windows, Python 3.11 i 3.13, uprawnienia tylko do odczytu) oraz `release.yml`: tag `v*` buduje na `windows-latest` (IExpress), uruchamia cały zestaw testów, buduje pliki, sprawdza sumy kontrolne i dopiero osobne zadanie `publish` (jedyne z `contents: write`, bez checkoutu kodu) tworzy release przez `gh release create ... --verify-tag`. `workflow_dispatch` to próba na sucho, nigdy nie publikuje.
- Tylko akcje pierwszej strony `actions/*` w głównych wersjach (checkout v7, setup-python v7, upload-artifact v7, download-artifact v8, sprawdzone na GitHubie 2026-10-14), bez zewnętrznej akcji do release. Przypięcie do SHA byłoby ostrzejsze. Wartości trafiają do skryptów tylko przez zmienne środowiskowe, kształt tagu jest walidowany, istniejącego release nie nadpisujemy (brak `--clobber`), wersja z przyrostkiem po myślniku jest oznaczana jako prerelease.
- Release publikuje workflow na koncie właściciela repozytorium (`gh` na maszynie dewelopera jest zalogowany na inne konto), więc pierwsze wydanie robi się przez wypchnięcie tagu. Przed pierwszym tagiem zrobić próbę przez `workflow_dispatch`. Workflow nie był uruchomiony na GitHubie.
