# STATUS — Kalkulator Lotto (hand-off)

> Punkt startowy dla nowych sesji/czatów pracujących nad projektem.
> Aktualizowany w tym samym commicie co każda większa zmiana.
> **Repo jest publiczne — w tym pliku NIGDY nie ma sekretów, kluczy ani danych osobowych.**
> Ostatnia aktualizacja: **05.08.2026**

## Projekt

- **Repo**: github.com/Isithunzi000/lotto (publiczne), gałąź `main`
- **Live**: isithunzi000.github.io/lotto/ (GitHub Pages)
- **Aplikacja**: jednoplikowy kalkulator lotto (`index.html`, ~1,5 MB, 100% offline). Baza losowań osadzona jako gzip+base64 w zakotwiczonej linii `const HIST_DATA_B64 = "H4sI..."`.
- **Dokumentacja techniczna**: README.md opisuje całą automatykę.

## Stan w chwili ostatniej aktualizacji (05.08.2026)

- **Wersja**: v4.15.2 (tag + release istnieją)
- **main**: `44d7ad7` — working tree czysty, local == remote
  _(po tym commicie mogą już być nowsze commity sondy z danymi — to normalne)_
- **Testy**: 66 testów node zielonych + 9 scenariuszy strażników + 3 próby generalne online + pełny test fail-safe (b) — wszystko zweryfikowane
- **Issues**: 0 otwartych

## Automatyka (GitHub Actions)

1. **Sonda** (`tools/update_draws.py` + `.github/workflows/update-draws.yml`) — pobiera wyniki z oficjalnego LOTTO OpenAPI 3×/dziennie, aktualizuje dane w `index.html`, idempotentna.
2. **Auto-tag/release** (`tag-version.yml`) — po wykryciu nowej wersji w `version-tag` tworzy tag i release. Uwaga: pushe z GITHUB_TOKEN NIE odpalają innych workflow — dlatego rekalibracja wywołuje go jawnie.
3. **Auto-rekalibracja wag popularności** (`recalibrate.yml` + `tools/calibrate_popularity.py` + `tools/guard_calibration.py`):
   - cron: 2. dzień miesiąca 06:00 UTC + ręczne `workflow_dispatch` (inputy `force`, `dry_run`)
   - bramka „due": min. 183 dni od ostatniego commita kalibracji (`git log -G '^const POPULARNOSC_KALIBR'`)
   - determinizm: podwójny przebieg, wyniki muszą być bajtowo identyczne
   - strażnicy G1-G7 (schema, n, zakresy wag, korelacja, |Δw| ≤ 0,05, monotoniczność i świeżość stanNa)
   - sukces: auto-bump wersji + commit + push + dispatch tag-version.yml + weryfikacja release
   - porażka strażników: reset pliku, czerwony job, deduplikowany issue — nic nie publikuje
   - pierwsza realna automatyczna rekalibracja: ~luty 2027 (6 mies. po kalibracji z v4.15.0)

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

- **Test (a)** — wymuszona rekalibracja (`force`) na main po nowych losowaniach, zaplanowana na piątek 07.08.2026. Jeśli wagi przesuną się ≥ 0,0001 → pełna ścieżka publikacji odpali na żywo (v4.15.3); jeśli nie → kolejny dowód idempotencji (też poprawne).
- Comiesięczne ticki crona rekalibracji do ~lutego 2027 będą kończyć się statusem „niedue" — to oczekiwane.
