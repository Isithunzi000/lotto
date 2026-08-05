#!/usr/bin/env python3
"""Strażnicy auto-rekalibracji wag popularności (G1-G7).

Porównuje wbudowane const POPULARNOSC_KALIBR(_MINI)_JSON PRZED (HEAD) i PO
przebiegu tools/calibrate_popularity.py. Każde naruszenie = komunikat + exit 1
(fail-safe: zero publikacji). Używane przez .github/workflows/recalibrate.yml.

Lokalnie:
    python3 tools/guard_calibration.py [--old PLIK] [--new PLIK] [--today DD.MM.YYYY]

Domyślnie: old = `git show HEAD:index.html`, new = index.html, today = dziś.

Strażnicy:
    G1 schema: klucze kompletne, wartości skończone i w sensownych zakresach
    G2 n nie spada istotnie vs poprzednia kalibracja (ubytek danych?)
    G3 wagi w granicach [1.00; 1.30] (odwrócony znak / powrót do heurystyki = stop)
    G4 corrTest > 0 (fit zachowuje moc predykcyjną)
    G5 |Δw|, |Δwr| <= 0.05 absolutnie vs wbudowane (skok = podejrzany)
    G6 stanNa monotonicznie niemalejący
    G7 stanNa nie starsze niż 14 dni (martwa sonda = stop)
"""
import json
import os
import re
import subprocess
import sys
from datetime import datetime

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX_PATH = os.path.join(REPO_DIR, 'index.html')

GAMES = {
    'lotto': ('POPULARNOSC_KALIBR_JSON', ('hit6Lo', 'hit6Hi')),
    'mini': ('POPULARNOSC_KALIBR_MINI_JSON', ('hit5Lo', 'hit5Hi')),
}
REQUIRED_KEYS = ['b', 'w', 'r', 'wr', 'n', 'nTest', 'corrTrain', 'corrTest', 'stanNa']
W_MIN, W_MAX = 1.00, 1.30    # G3
MAX_DW = 0.05                # G5 (absolutnie)
N_MIN_RATIO = 0.9            # G2
FRESH_DAYS = 14              # G7
DATE_RE = re.compile(r'^\d{2}\.\d{2}\.\d{4}$')

errors = []


def err(msg):
    errors.append(msg)
    print('GUARD-FAIL:', msg)


def _no_const(x):
    raise ValueError(f'niedozwolona stała w JSON: {x}')


def extract_consts(src, origin):
    """Wyciąga oba const kalibracji. NaN/Inf odrzucane na poziomie parsera."""
    out = {}
    for game, (name, _hits) in GAMES.items():
        m = re.search(rf'^const {name} = (\{{[^\n]+\}});$', src, re.M)
        if not m:
            err(f'{origin}: brak zakotwiczonej linii {name}')
            continue
        try:
            out[game] = json.loads(m.group(1), parse_constant=_no_const)
        except ValueError as e:
            err(f'{origin}: {name} — niepoprawny/nieskończony JSON ({e})')
    return out


def parse_date(s):
    return datetime.strptime(s, '%d.%m.%Y')


def check_game(game, old, new, today):
    name, (hit_lo, hit_hi) = GAMES[game]
    n_err0 = len(errors)

    # ---- G1: schema i skończoność ----
    for k in REQUIRED_KEYS:
        if k not in new:
            err(f'G1 {game}: brak klucza {k}')
    for k in (hit_lo, hit_hi):
        if k not in new:
            err(f'G1 {game}: brak klucza walidacji {k}')
    for k in ('b', 'w', 'r', 'wr', 'corrTrain', 'corrTest'):
        v = new.get(k)
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            err(f'G1 {game}: {k} nie jest liczbą ({v!r})')
    for k in ('n', 'nTest'):
        v = new.get(k)
        if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
            err(f'G1 {game}: {k} nie jest dodatnią liczbą całkowitą ({v!r})')
    for k in (hit_lo, hit_hi):
        v = new.get(k)
        if k in new and v is not None and not (isinstance(v, (int, float)) and 0.0 <= v <= 1.0):
            err(f'G1 {game}: {k}={v!r} poza [0,1]')
    ct = new.get('corrTest')
    if isinstance(ct, (int, float)) and not (-1.0 <= ct <= 1.0):
        err(f'G1 {game}: corrTest={ct} poza [-1,1]')
    if isinstance(new.get('n'), int) and isinstance(new.get('nTest'), int) and new['n'] > 0:
        if not (0.05 * new['n'] <= new['nTest'] <= 0.35 * new['n']):
            err(f'G1 {game}: nTest={new["nTest"]} poza 5–35% n={new["n"]}')
    sn = new.get('stanNa')
    if not isinstance(sn, str) or not DATE_RE.match(sn):
        err(f'G1 {game}: stanNa={sn!r} — oczekiwano DD.MM.YYYY')
    if len(errors) > n_err0:
        return  # bez poprawnej G1 dalsze strażnicy tej gry nie mają sensu

    # ---- G2: n nie spada istotnie ----
    if new['n'] < N_MIN_RATIO * old['n']:
        err(f'G2 {game}: n={new["n"]} < {N_MIN_RATIO}× poprzednie n={old["n"]} (ubytek danych?)')

    # ---- G3: granice wag ----
    for k in ('w', 'wr'):
        if not (W_MIN <= new[k] <= W_MAX):
            err(f'G3 {game}: {k}={new[k]} poza [{W_MIN}; {W_MAX}]')

    # ---- G4: moc predykcyjna ----
    if new['corrTest'] <= 0:
        err(f'G4 {game}: corrTest={new["corrTest"]} <= 0 (fit bez mocy predykcyjnej)')

    # ---- G5: limit zmiany vs wbudowane ----
    for k in ('w', 'wr'):
        d = abs(new[k] - old[k])
        if d > MAX_DW:
            err(f'G5 {game}: |Δ{k}|={d:.4f} > {MAX_DW} (było {old[k]}, jest {new[k]}) — skok podejrzany')

    # ---- G6: monotoniczność stanNa ----
    if parse_date(new['stanNa']) < parse_date(old['stanNa']):
        err(f'G6 {game}: stanNa cofnęło się {old["stanNa"]} -> {new["stanNa"]}')

    # ---- G7: świeżość danych ----
    age = (today - parse_date(new['stanNa'])).days
    if age > FRESH_DAYS:
        err(f'G7 {game}: stanNa={new["stanNa"]} ma {age} dni (> {FRESH_DAYS}) — dane nieświeże (sonda działa?)')


def main():
    args = sys.argv[1:]

    def opt(flag):
        return args[args.index(flag) + 1] if flag in args else None

    if opt('--old'):
        old_src = open(opt('--old'), encoding='utf-8').read()
    else:
        old_src = subprocess.run(['git', 'show', 'HEAD:index.html'], cwd=REPO_DIR,
                                 capture_output=True, text=True, check=True).stdout
    new_src = open(opt('--new') or INDEX_PATH, encoding='utf-8').read()
    today = parse_date(opt('--today')) if opt('--today') else datetime.now()

    old = extract_consts(old_src, 'HEAD')
    new = extract_consts(new_src, 'working')
    for game in GAMES:
        if game in old and game in new:
            check_game(game, old[game], new[game], today)

    if errors:
        print(f'\nODRZUCONO — {len(errors)} naruszeń strażników. Brak publikacji.')
        sys.exit(1)
    print('Strażnicy G1-G7: OK (lotto, mini)')


if __name__ == '__main__':
    main()
