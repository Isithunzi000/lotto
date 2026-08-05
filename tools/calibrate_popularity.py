#!/usr/bin/env python3
"""Kalibracja empiryczna wag modelu popularności (Lotto + Mini Lotto) — v4.15.0.

DANE:   data/{gra}.csv (wylosowane liczby) + data/wyplaty_{gra}.csv
        (liczby zwycięzców per stopień, z oficjalnego API LOTTO).
MODEL:  E[zwycięzcy stopnia d] = sprzedaż(losowanie) x p_d x M(zestaw).
        W ilorazie w_d/w_base sprzedaż się skraca (estymator bez danych
        o sprzedaży). Trafienie obejmuje d z N wylosowanych liczb, więc
        nachylenie względem cech zestawu skaluje się z d (dyfuzja):
            log(w_d/w_base) - log(p_d/p_base) = alpha_d + d * (b*f_b + r*f_r)
        f_b = frakcja liczb <=31 ("urodzinowe"), f_r = frakcja "okrągłych"
        (podzbiór <=31 — efekt brzegowy). Estymacja WLS z wagami Poissona
        (1/w_d + 1/w_base)^-1, osobny intercept alpha na stopień.
        Lotto: fit stopni 4-5/6, walidacja 6/6. Mini Lotto: fit stopnia
        4/5 (5/5 ma 0-2 zwycięzców — za mało do fitu), walidacja 5/5.
WYNIK:  const POPULARNOSC_KALIBR_JSON (pula 49) i POPULARNOSC_KALIBR_MINI_JSON
        (pula 42) w index.html (linie zakotwiczone, stała kolejność kluczy).
        b i r to log-wagi per liczba; w = exp(b), wr = exp(r) używane przez
        popularityScore() dla odpowiedniej puli. Uwaga: CSV Mini Lotto trzyma
        surowe stopnie API (1=5/5, 2=4/5, 3=3/5) — loader mapuje na trafienia.

DETERMINISTYCZNY: stałe dane wejściowe -> stały wynik (brak losowości,
podział train/test po dacie 80/20). IDEMPOTENTNY: ten sam wynik -> brak
zapisu. Uruchamiany ręcznie (nie w Actions) — zmienia stałe modelu, wymaga
przeglądu.
Użycie: python3 tools/calibrate_popularity.py [--game lotto|mini|all] [--dry-run]
"""

import json
import math
import os
import re
import statistics
import sys

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX_PATH = os.path.join(REPO_DIR, 'index.html')

BIRTHDAY_MAX = 31
ROUND_NUMBERS = {7, 13, 14, 17, 21, 22, 27, 28}
TEST_FRACTION = 0.2          # ostatnie 20% losowań (po dacie) = zbiór testowy
MIN_DRAWS = 200              # poniżej — odmowa kalibracji (za mało danych)

GAMES = {
    'lotto': {
        'results': 'lotto.csv',
        'wyplaty': 'wyplaty_lotto.csv',
        'pick': 6, 'pool': 49,
        'raw_api_degrees': False,   # CSV trzyma trafienia (3,4,5,6)
        'fit_degs': (4, 5), 'base_deg': 3, 'val_deg': 6,
        'const': 'POPULARNOSC_KALIBR_JSON',
        'label': 'Lotto',
    },
    'mini': {
        'results': 'mini_lotto.csv',
        'wyplaty': 'wyplaty_minilotto.csv',
        'pick': 5, 'pool': 42,
        'raw_api_degrees': True,    # CSV trzyma surowe stopnie API: 1=5/5, 2=4/5, 3=3/5
        'fit_degs': (4,), 'base_deg': 3, 'val_deg': 5,
        'const': 'POPULARNOSC_KALIBR_MINI_JSON',
        'label': 'Mini Lotto',
    },
}


def game_prob(cfg, h):
    """Dokładne prawdopodobieństwo trafienia h z pick w puli pool."""
    pick, pool = cfg['pick'], cfg['pool']
    return math.comb(pick, h) * math.comb(pool - pick, pick - h) / math.comb(pool, pick)


def load_data(cfg):
    results = {}
    need_len = cfg['pick'] + 2      # nr, data, pick liczb
    with open(os.path.join(REPO_DIR, 'data', cfg['results']), encoding='utf-8') as f:
        for line in f:
            p = line.strip().split(',')
            if len(p) == need_len:
                results[int(p[0])] = (p[1], [int(x) for x in p[2:need_len]])
    wyplaty = {}
    with open(os.path.join(REPO_DIR, 'data', cfg['wyplaty']), encoding='utf-8') as f:
        for line in f:
            p = line.strip().split(',')
            if len(p) == 5:
                nr, date, deg, cnt, _kw = p
                hits = (cfg['pick'] + 1 - int(deg)) if cfg['raw_api_degrees'] else int(deg)
                wyplaty.setdefault(int(nr), {'date': date})[hits] = int(cnt)
    need = set(cfg['fit_degs']) | {cfg['base_deg'], cfg['val_deg']}
    # wspólne losowania, chronologicznie (numery obu źródeł są zgodne 1:1)
    nrs = sorted(n for n in wyplaty if n in results
                 and all(h in wyplaty[n] for h in need))
    rows = []
    for nr in nrs:
        nums = results[nr][1]
        w = wyplaty[nr]
        row = {
            'nr': nr, 'date': wyplaty[nr]['date'], 'nums': nums,
            'f_b': sum(1 for n in nums if n <= BIRTHDAY_MAX) / cfg['pick'],
            'f_r': sum(1 for n in nums if n in ROUND_NUMBERS) / cfg['pick'],
        }
        for h in need:
            row[f'w{h}'] = w[h]
        rows.append(row)
    return rows


def solve(A, b):
    """Eliminacja Gaussa z częściowym pivotem (dowolny rozmiar n x n)."""
    n = len(A)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(M[r][col]))
        M[col], M[piv] = M[piv], M[col]
        for r in range(col + 1, n):
            f = M[r][col] / M[col][col]
            M[r] = [m - f * mc for m, mc in zip(M[r], M[col])]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        x[r] = (M[r][n] - sum(M[r][c] * x[c] for c in range(r + 1, n))) / M[r][r]
    return x


def inv(A):
    """Odwrotność n x n przez Gaussa-Jordana (do błędów standardowych)."""
    n = len(A)
    M = [row[:] + [1.0 if i == j else 0.0 for j in range(n)] for i, row in enumerate(A)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(M[r][col]))
        M[col], M[piv] = M[piv], M[col]
        d = M[col][col]
        M[col] = [m / d for m in M[col]]
        for r in range(n):
            if r != col:
                f = M[r][col]
                M[r] = [m - f * mc for m, mc in zip(M[r], M[col])]
    return [row[n:] for row in M]


def design_row(cfg, row, d):
    """Wiersz macierzy planu: intercepty per stopień + d*f_b + d*f_r."""
    return [1.0 if d == dj else 0.0 for dj in cfg['fit_degs']] + \
           [d * row['f_b'], d * row['f_r']]


def fit_wls(cfg, rows):
    """WLS: y_d = sum_d' alpha_d'*[d==d'] + b*(d*f_b) + r*(d*f_r).
    Zwraca (b, r, se_b, se_r, y, yhat)."""
    base = cfg['base_deg']
    X, Y, W = [], [], []
    for row in rows:
        if row[f'w{base}'] <= 0:
            continue
        for d in cfg['fit_degs']:
            wd = row[f'w{d}']
            if wd <= 0:
                continue
            y = math.log(wd / row[f'w{base}']) - math.log(game_prob(cfg, d) / game_prob(cfg, base))
            X.append(design_row(cfg, row, d))
            Y.append(y)
            W.append(1.0 / (1.0 / wd + 1.0 / row[f'w{base}']))
    n = len(Y)
    k = len(cfg['fit_degs']) + 2
    # rozwiązanie WLS przez normal equations (deterministyczne)
    XtWX = [[sum(X[i][a] * X[i][b_] * W[i] for i in range(n)) for b_ in range(k)]
            for a in range(k)]
    XtWY = [sum(X[i][a] * Y[i] * W[i] for i in range(n)) for a in range(k)]
    beta = solve(XtWX, XtWY)
    resid = [Y[i] - sum(X[i][a] * beta[a] for a in range(k)) for i in range(n)]
    sigma2 = sum(W[i] * resid[i] ** 2 for i in range(n)) / max(1, n - k)
    invXtWX = inv(XtWX)
    se = [math.sqrt(max(0.0, sigma2 * invXtWX[a][a])) for a in range(k)]
    yhat = [sum(X[i][a] * beta[a] for a in range(k)) for i in range(n)]
    return beta[-2], beta[-1], se[-2], se[-1], Y, yhat


def pearson(xs, ys):
    mx, my = statistics.mean(xs), statistics.mean(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    return cov / math.sqrt(vx * vy) if vx > 0 and vy > 0 else 0.0


def validate_hits(cfg, test_rows, b):
    """Walidacja najwyższego stopnia: częstość trafień (ktoś trafił) w dolnej
    vs górnej połowie score zestawu wg skalibrowanej wagi. Sygnał wtórny —
    główna estymacja bierze się ze stopni fit_degs."""
    val = cfg['val_deg']

    def score(nums):
        return sum(math.exp(b) if n <= BIRTHDAY_MAX else 1.0 for n in nums) / cfg['pick']

    srt = sorted(test_rows, key=lambda r: score(r['nums']))
    half = len(srt) // 2
    lo, hi = srt[:half], srt[half:]
    rate_lo = sum(1 for r in lo if r[f'w{val}'] > 0) / len(lo) if lo else None
    rate_hi = sum(1 for r in hi if r[f'w{val}'] > 0) / len(hi) if hi else None
    return rate_lo, rate_hi


def calibrate_game(name, cfg, dry_run):
    print(f'=== {cfg["label"]} (pula {cfg["pool"]}, {cfg["pick"]} liczb) ===')
    rows = load_data(cfg)
    if len(rows) < MIN_DRAWS:
        print(f'Za mało wspólnych losowań ({len(rows)} < {MIN_DRAWS}) — pominięto.')
        return False
    split = int(len(rows) * (1 - TEST_FRACTION))
    train, test = rows[:split], rows[split:]

    b, r, se_b, se_r, y_tr, yhat_tr = fit_wls(cfg, train)
    corr_train = pearson(y_tr, yhat_tr)

    base = cfg['base_deg']

    # out-of-sample: predykcja z wagami z train
    def predict(row_list):
        Yt, Ph = [], []
        for row in row_list:
            if row[f'w{base}'] <= 0:
                continue
            for d in cfg['fit_degs']:
                wd = row[f'w{d}']
                if wd <= 0:
                    continue
                y = math.log(wd / row[f'w{base}']) - math.log(game_prob(cfg, d) / game_prob(cfg, base))
                Yt.append(y)
                # alpha_d z train oszacowane ponownie jako średnia residuów —
                # dla korelacji wystarczy sam składnik cech (stała nie wpływa)
                Ph.append(d * (b * row['f_b'] + r * row['f_r']))
        return pearson(Yt, Ph)
    corr_test = predict(test)

    val = cfg['val_deg']
    rate_lo, rate_hi = validate_hits(cfg, test, b)

    w, wr = round(math.exp(b), 4), round(math.exp(r), 4)
    data = {'b': round(b, 4), 'w': w, 'r': round(r, 4), 'wr': wr,
            'n': len(rows), 'nTest': len(test),
            'corrTrain': round(corr_train, 3), 'corrTest': round(corr_test, 3),
            f'hit{val}Lo': round(rate_lo, 3) if rate_lo is not None else None,
            f'hit{val}Hi': round(rate_hi, 3) if rate_hi is not None else None,
            'stanNa': rows[-1]['date']}

    print(f'losowań: {len(rows)} (train {len(train)} / test {len(test)}), '
          f'{rows[0]["date"]}..{rows[-1]["date"]}')
    print(f'b = {b:+.4f} ± {se_b:.4f}  -> waga urodzinowa {w} (heurystyka: 3.0)')
    print(f'r = {r:+.4f} ± {se_r:.4f}  -> waga okrągła    {wr} (heurystyka: 1.3)')
    print(f'korelacja dopasowania: train {corr_train:.3f} / test {corr_test:.3f}')
    if rate_lo is not None:
        print(f'walidacja {val}/{cfg["pick"]} (test): częstość trafień lo-score '
              f'{rate_lo:.1%} vs hi-score {rate_hi:.1%}')

    const_line = f'const {cfg["const"]} = ' + json.dumps(
        data, ensure_ascii=False, separators=(',', ':')) + ';'
    src = open(INDEX_PATH, encoding='utf-8').read()
    kal_re = re.compile(rf'^const {cfg["const"]} = \{{[^\n]+\}};$', re.M)
    new_src, n = kal_re.subn(lambda _: const_line, src, count=1)
    if n != 1:
        print(f'BŁĄD: nie znaleziono zakotwiczonej linii {cfg["const"]}')
        return False
    if new_src == src:
        print('wynik identyczny z wbudowanym — bez zapisu (idempotencja)')
        return True
    if dry_run:
        print('--dry-run: zmiana NIE zapisana')
        print(const_line)
        return True
    open(INDEX_PATH, 'w', encoding='utf-8').write(new_src)
    print(f'index.html: zaktualizowano {cfg["const"]}')
    return True


def main():
    dry_run = '--dry-run' in sys.argv
    game = 'all'
    if '--game' in sys.argv:
        i = sys.argv.index('--game')
        game = sys.argv[i + 1]
    if game == 'all':
        selected = list(GAMES)          # stała kolejność: lotto, mini
    elif game in GAMES:
        selected = [game]
    else:
        print(f'BŁĄD: nieznana gra "{game}" (dostępne: lotto, mini, all)')
        sys.exit(1)
    ok = True
    for i, name in enumerate(selected):
        if i:
            print()
        ok = calibrate_game(name, GAMES[name], dry_run) and ok
    if not ok:
        sys.exit(1)


if __name__ == '__main__':
    main()
