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
- **Ekstra Pensja**

## Funkcje

### EV — pełne rozbicie
Dokładne wyliczenie wartości oczekiwanej i RTP z rozbiciem na wszystkie
stopnie wygranych. Uwzględnia kumulacje, promocje okresowe w Multi Multi
(włączane/wyłączane zgodnie z datami obowiązywania w regulaminach) oraz
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

### Laboratorium
Raporty syntetyczne i testy parametrów wejścia (wagi, zakresy dat) —
w tym testy na wszystkich permutacjach, rozłącznych zakresach i oknach
kroczących, z podaniem najlepszych konfiguracji.

### Podział budżetu
Symulacja budżetu na wybraną grę — ile zakładów, jakim kosztem,
z jaką wartością oczekiwaną.

## Dane i prywatność

- Wszystkie obliczenia odbywają się lokalnie w przeglądarce.
- Zapisane parametry i ustawienia trzymane są wyłącznie na urządzeniu
  użytkownika (localStorage) — nic nie jest wysyłane na zewnątrz.
- Baza wyników zaszyta w aplikacji; planowana automatyczna aktualizacja
  z oficjalnych źródeł.

## Licencja

Patrz plik [LICENSE](LICENSE).

## Changelog

### v4.10.0 (2026-07-25)
- Pierwsza wersja opublikowana na GitHub Pages
