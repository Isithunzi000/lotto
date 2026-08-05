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
  (źródło domyślnych estymacji 4/6 i 5/6 w zakładce EV)

## Dane i prywatność

- Wszystkie obliczenia odbywają się lokalnie w przeglądarce.
- Zapisane parametry i ustawienia trzymane są wyłącznie na urządzeniu
  użytkownika (localStorage) — nic nie jest wysyłane na zewnątrz.

## Licencja

Patrz plik [LICENSE](LICENSE).

## Historia zmian

Historia wersji prowadzona jest w commitach na gałęzi `main` — każdy commit
wersji ma numer i opis w komunikacie; wydania oznaczone są tagami `v*`.
