# Kalkulator Lotto

Narzędzie do analizy gier liczbowych Totalizatora Sportowego: generatory
kuponów i systemów, statystyki losowań, symulacje budżetu oraz wyliczanie
wartości oczekiwanej (EV) i zwrotu dla gracza (RTP). Analiza oparta o oficjalne
regulaminy, tabele wygranych, kumulacje oraz promocje okresowe.

**Wersja online:** https://isithunzi000.github.io/lotto/

Można też pobrać `index.html` i otworzyć lokalnie w przeglądarce —
kalkulator działa w 100% offline, bez serwera i bez instalacji.

## Obsługiwane gry

- **Lotto** (w tym Lotto Plus)
- **Multi Multi** (z promocjami okresowymi wg regulaminów TS)
- **Eurojackpot**
- **Mini Lotto**
- **Ekstra Pensja** (w tym Ekstra Premia)

## Funkcje

### EV — pełne rozbicie
Dokładne wyliczenie wartości oczekiwanej i RTP z rozbiciem na wszystkie
stopnie wygranych. Uwzględnia kumulacje (aktualizowane automatycznie
z oficjalnego API LOTTO), promocje okresowe w Multi Multi
(włączane ręcznie zgodnie z datami obowiązywania w regulaminach) oraz
pełne tabele wygranych.

### Ranking gier wg RTP
Porównanie wszystkich gier na jednym ekranie — łatwo widać, która gra i przy
jakich parametrach (np. wysokość kumulacji) daje matematycznie najlepszy zwrot.

### Generator kuponu
Generowanie zestawów liczb z filtrami puli (suma, parzystość, zakresy,
rozrzut i inne), deterministycznym seedem (pole seed + kostka do losowania)
oraz oceną popularności zestawu — można unikać kombinacji, które grają tłumy
(mniejsze ryzyko podziału wygranej).

### System
Generator systemowy (skrócony) z pełnym zestawem filtrów i seedem —
rozbicie większej puli liczb na zakłady proste z gwarancjami trafień.

### Historia losowań
Przeglądanie archiwalnych wyników ze statystykami częstotliwościowymi
i wagami opartymi na danych z ostatnich 12 miesięcy lub całej historii.
Import CSV z własnych źródeł jako opcja aktualizacji/backupu.

### Laboratorium
Raporty syntetyczne i testy parametrów wejścia (wagi, zakresy dat) —
w tym testy na wszystkich permutacjach, rozłącznych zakresach i oknach
kroczących, z podaniem najlepszych konfiguracji.

### Podział budżetu
Symulacja budżetu na wybraną grę — ile zakładów, jakim kosztem,
z jaką wartością oczekiwaną.

## Aktualizacja danych

Baza wyników jest zaszyta w aplikacji i aktualizowana automatycznie —
sonda (GitHub Actions, 3× dziennie) odpytuje oficjalne LOTTO OpenAPI
i dopisuje nowe losowania. Pliki w repo:

- `data/*.csv` — pełna historia wyników 7 gier, format 1:1
  z wynikilotto.net.pl (zweryfikowane, bez historycznych błędów)
- `data/kumulacje.csv` — aktualne kumulacje Lotto i Eurojackpot
- `data/wyplaty_lotto.csv` — faktyczne wypłaty per stopień dla Lotto
  (źródło domyślnych estymacji 4/6 i 5/6 w zakładce EV; z liczb zwycięzców
  3/4/5-trafień sonda estymuje też sprzedaż zakładów wg głębokości kumulacji
  — presety sprzedaży w panelu „Kumulacja a EV")
- `data/wyplaty_minilotto.csv` — faktyczne wypłaty Mini Lotto (500 losowań;
  mediany warunkowe „padła/nie padła 5/5" — szacunek EV w zakładce EV,
  a liczby zwycięzców 3/4-trafień zasilają kalibrację wag popularności)
- `data/wyplaty_eurojackpot.csv` — faktyczne wypłaty Eurojackpot
  (średnie i mediany stopni V–XII — szacunek EV niższych stopni;
  kolumna nr to identyfikator losowania z API, nie numer z bazy wyników)

Dodatkowo repo zawiera `tools/calibrate_popularity.py` — skrypt kalibracji
empirycznej wag modelu popularności zestawów (regresja log-ilorazowa WLS
na liczbach zwycięzców z API; train/test 80/20). Kalibracja nie jest
uruchamiana w Actions — zmienia stałe modelu i wymaga ręcznego przeglądu.
Wynik jest wbudowywany w `index.html` jako `POPULARNOSC_KALIBR_JSON`
(Lotto / Lotto Plus, pula 49) i `POPULARNOSC_KALIBR_MINI_JSON` (Mini Lotto,
pula 42); pozostałe gry używają dotychczasowej heurystyki, bo albo mają
wygrane stałe (popularność nie wpływa na wypłatę), albo dzielą pulę
w skali międzynarodowej przy danych tylko polskich (EuroJackpot).
Uruchomienie: `python3 tools/calibrate_popularity.py [--game lotto|mini|all]`
(domyślnie obie gry; `--dry-run` bez zapisu).

## Dane i prywatność

- Wszystkie obliczenia odbywają się lokalnie w przeglądarce.
- Zapisane parametry i ustawienia trzymane są wyłącznie na urządzeniu
  użytkownika (localStorage) — nic nie jest wysyłane na zewnątrz.

## Licencja

Patrz plik [LICENSE](LICENSE).

## Historia zmian

Historia wersji prowadzona jest w commitach na gałęzi `main` — każdy commit
wersji ma numer i opis w komunikacie; wydania oznaczone są tagami `v*`.
