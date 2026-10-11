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
