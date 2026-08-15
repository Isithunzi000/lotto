# STATUS — Kalkulator Lotto (hand-off)

> Punkt startowy dla nowych sesji/czatów pracujących nad projektem.
> Aktualizowany w tym samym commicie co każda większa zmiana.
> **Repo jest publiczne — w tym pliku NIGDY nie ma sekretów, kluczy ani danych osobowych.**
> Ostatnia aktualizacja: **15.08.2026**

## Projekt

- **Repo**: github.com/Isithunzi000/lotto (publiczne), gałąź `main`
- **Live**: isithunzi000.github.io/lotto/ (GitHub Pages)
- **Aplikacja**: jednoplikowy kalkulator lotto (`index.html`, ~1,5 MB, 100% offline). Baza losowań osadzona jako gzip+base64 w zakotwiczonej linii `const HIST_DATA_B64 = "H4sI..."`.
- **Dokumentacja techniczna**: README.md opisuje całą automatykę.

## Stan w chwili ostatniej aktualizacji (13.08.2026)

- **Wersja**: v4.15.3 (tag + release istnieją) — auto-kalibracja wag 13.08.2026, strażnicy G1-G7 OK
- **main**: `923d3f8` — dane losowań aktualne do 13.08.2026
- **Wagi po kalibracji 13.08**: Lotto w=1,0302, wr=1,016 (n=503, stan 11.08); Mini w=1,0191, wr=1,0112 (n=508, stan 12.08)
- **Issues**: 0 otwartych
- **CI**: pełny hardening wdrożony i zweryfikowany runami na żywo (wszystkie zielone)

## Automatyka (GitHub Actions)

1. **Sonda** (`tools/update_draws.py` + `.github/workflows/update-draws.yml`) — pobiera wyniki z oficjalnego LOTTO OpenAPI, aktualizuje dane w `index.html`, idempotentna.
   - **Harmonogram od 15.08.2026: 8 cronów w parach sezonowych** (losowania trzymają czas warszawski, cron jest UTC — dla każdego celu 2 crony: lato/zima; run „poza sezonem" kończy się pusto): MM 14:00 → +35 min (`35 12`/`35 13` UTC); EJ 20:15 wt/pt → +60 min (`15 19`/`15 20` UTC `2,5`); blok 22:00 → +45 lato/+35 zima (`45 20`/`35 21` UTC); zapas `35 22` UTC; **poranna wyciągarka** `15 6` UTC (domyka, co umknęło w nocy). Minuty poza :00/:30, odstępy ≥30 min.
   - Typowa świeżość po losowaniu: ~35–60 min; worst case: rano następnego dnia (backfill).
2. **Auto-tag/release** (`tag-version.yml`) — po wykryciu nowej wersji w `version-tag` tworzy tag i release. Uwaga: pushe z GITHUB_TOKEN NIE odpalają innych workflow — dlatego rekalibracja wywołuje go jawnie.
3. **Auto-rekalibracja wag popularności** (`recalibrate.yml` + `tools/calibrate_popularity.py` + `tools/guard_calibration.py`):
   - cron: 2. dzień miesiąca 06:00 UTC + ręczne `workflow_dispatch` (inputy `force`, `dry_run`)
   - bramka „due": min. 183 dni od ostatniego commita kalibracji (`git log -G '^const POPULARNOSC_KALIBR'`)
   - determinizm: podwójny przebieg, wyniki muszą być bajtowo identyczne
   - strażnicy G1-G7 (schema, n, zakresy wag, korelacja, |Δw| ≤ 0,05, monotoniczność i świeżość stanNa)
   - sukces: auto-bump wersji + commit + push + dispatch tag-version.yml + weryfikacja release
   - porażka strażników: reset pliku, czerwony job, deduplikowany issue — nic nie publikuje
   - pierwsza realna automatyczna rekalibracja: ~luty 2027 (6 mies. po kalibracji z v4.15.0)

## Hardening CI (13.08.2026)

Po dwóch fałszywych alarmach „Run failed" (kolejka runnerów GitHub + wyścig o push) wszystkie trzy workflow dostały spójny zestaw zabezpieczeń:

- **concurrency per workflow** (`sonda-lotto`, `tag-version`, `recalibrate`; `cancel-in-progress: false`) — runy kolejkują się zamiast ścigać o push/tag
- **odporne pushe** — `pull --rebase` + 3 próby z rosnącą pauzą (sonda, rekalibracja, push taga)
- **retry sondy** po 60 s przy porażce (skrypt idempotentny)
- **guard na pusty staging** zamiast „nothing to commit" (exit 1)
- **timeout-minutes** na każdym jobie (10–20 min)
- **akcje v7** (`checkout`, `setup-python`) — Node 24, koniec warningów deprecacji

Zmiany w `tools/update_draws.py` (13.08.2026):

- **trwałe luki wypłat pomijane look-ahead** (`GAP_LOOKAHEAD = 5`): jeśli nowsze numery mają już wypłaty, a dany nr nie — luka jest pomijana z ostrzeżeniem zamiast blokować mediany na zawsze
- **sekcje opcjonalne soft-fail**: błąd wypłat/median/sprzedaży (także `fail()`) nie przerywa sondy — dane główne (losowania, kumulacje, blob) zapisują się zawsze; jądro nadal hard-fail
- porażki sekcji opcjonalnych zgłaszane outputami `optfail`/`optfail_sections` → workflow zakłada **deduplikowany issue** (kolejne przypadki jako komentarze)

## Zasady stałe (obowiązują zawsze)

1. **Zielone światło**: żadnych akcji zewnętrznych (push, commit na main, release, workflow) bez wyraźnej zgody użytkownika. Analiza i testy lokalne — dowolnie.
2. **openapi.json** (w plikach projektu Kimi) = jedyne źródło prawdy o LOTTO API.
3. Bump wersji przy każdej zmianie aplikacji, z markerem `// vX.Y.Z:` w kodzie; zmiany czysto CI/docs — bez bumpu.
4. Edycje `index.html`: jeden atomowy skrypt z asercjami `src.count(old) == N`, przerwanie PRZED zapisem przy niezgodności.
5. Uczciwe ramy statystyczne: dane mierzone vs heurystyki zawsze oznaczone; zero obietnic „na 100%" — zamiast tego enumeracja ryzyk, odwracalności i worst case.
6. Automatyka deterministyczna + idempotentna.
7. STATUS.md aktualizowany w commicie każdej większej zmiany; bez sekretów (repo publiczne).

## Dane techniczne (dla orientacji)

- Kluczowe consty w `index.html`: `KUMULACJE_JSON`, `WYPLATY_JSON`, `POPULARNOSC_KALIBR_JSON`, `POPULARNOSC_KALIBR_MINI_JSON` (ok. linie 8324–8346)
- Skalibrowane wagi: Lotto w=1,03, wr=1,0165 (n=500); Mini w=1,0189, wr=1,0101 (n=500); stan na 04.08.2026
- CSV wypłat (wyplaty_lotto.csv, wyplaty_minilotto.csv) są append-only — okno kalibracji rośnie

## Sekrety

PAT do GitHuba i klucz LOTTO_API_KEY są w **plikach projektu Kimi** (nigdy w repo). W nowej sesji wystarczy powiedzieć „użyj PAT z plików projektu".

## Jak wznowić pracę w nowym czacie

1. Otwórz czat w projekcie Kimi (nie zwykły czat).
2. Napisz np.: „Kontynuujemy projekt Lotto — przeczytaj STATUS.md z repo i sklonuj je".
3. Agent sklonuje repo do `/mnt/agents/repo`, przywróci `.git`, zweryfikuje stan i zgłosi gotowość.

## Zaległe / zaplanowane

- **Test (a)** — ✅ WYKONANY 13.08.2026: wymuszona rekalibracja (`force`) przeszła pełną ścieżkę publikacji na żywo (kalibracja → dowód determinizmu → strażnicy G1-G7 → bump v4.15.3 → tag → release → Pages). Wagi przesunęły się minimalnie (model stabilny).
- Comiesięczne ticki crona rekalibracji do ~lutego 2027 będą kończyć się statusem „niedue" — to oczekiwane.
