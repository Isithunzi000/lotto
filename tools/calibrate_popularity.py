#!/usr/bin/env python3
"""Kalibracja empiryczna wag modelu popularności (Lotto) — v4.14.0.

DANE:   data/lotto.csv (wylosowane liczby) + data/wyplaty_lotto.csv
        (liczby zwycięzców per stopień, z oficjalnego API LOTTO).
MODEL:  E[zwycięzcy stopnia d] = sprzedaż(losowanie) x p_d x M(zestaw).
        W ilorazie w_d/w_3 sprzedaż się skraca (estymator bez danych o
        sprzedaży). Trafienie obejmuje d z 6 wylosowanych liczb, więc
        nachylenie względem cech zestawu skaluje się z d (dyfuzja):
            log(w_d/w_3) - log(p_d/p_3) = alpha_d + d * (b*f_b + r*f_r)
        f_b = frakcja liczb <=31 ("urodzinowe"), f_r = frakcja "okrągłych"
        (podzbiór <=31 — efekt brzegowy). Estymacja WLS z wagami Poissona
        (1/w_d + 1/w_3)^-1, osobne intercepty alpha_4 i alpha_5.
WYNIK:  const POPULARNOSC_KALIBR_JSON w index.html (linia zakotwiczona,
        stała kolejność kluczy). b i r to log-wagi per liczba; w = exp(b),
        wr = exp(r) używane przez popularityScore() tylko dla puli 49.

DETERMINISTYCZNY: stałe dane wejściowe -> stały wynik (brak losowości,
podział train/test po dacie 80/20). IDEMPOTENTNY: ten sam wynik -> brak
zapisu. Uruchamiany ręcznie (nie w Actions) — zmienia stałe modelu, wymaga
przeglądu. Użycie:  python3 tools/calibrate_popularity.py [--dry-run]
"""

import json
import math
import os
import re
import statistics
import sys

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOTTO_PATH = os.path.join(REPO_DIR, 'data', 'lotto.csv')
WYPLATY_PATH = os.path.join(REPO_DIR, 'data', 'wyplaty_lotto.csv')
INDEX_PATH = os.path.join(REPO_DIR, 'index.html')
KALIBR_RE = re.compile(r'^const POPULARNOSC_KALIBR_JSON = \{[^\n]+\};$', re.M)

BIRTHDAY_MAX = 31
ROUND_NUMBERS = {7, 13, 14, 17, 21, 22, 27, 28}
TEST_FRACTION = 0.2          # ostatnie 20% losowań (po dacie) = zbiór testowy
MIN_DRAWS = 200              # poniżej — odmowa kalibracji (za mało danych)

# Dokładne prawdopodobieństwa trafień Lotto 6/49 (z definicji gry)
P = {h: math.comb(6, h) * math.comb(43, 6 - h) / math.comb(49, 6) for h in (3, 4, 5)}


def load_data():
    results = {}
    with open(LOTTO_PATH, encoding='utf-8') as f:
        for line in f:
            p = line.strip().split(',')
            if len(p) == 8:
                results[int(p[0])] = (p[1], [int(x) for x in p[2:8]])
    wyplaty = {}
    with open(WYPLATY_PATH, encoding='utf-8') as f:
        for line in f:
            p = line.strip().split(',')
            if len(p) == 5:
                nr, date, hits, cnt, _kw = p
                wyplaty.setdefault(int(nr), {'date': date})[int(hits)] = int(cnt)
    # wspólne losowania, chronologicznie
    nrs = sorted(n for n in wyplaty if n in results
                 and all(h in wyplaty[n] for h in (3, 4, 5, 6)))
    rows = []
    for nr in nrs:
        nums = results[nr][1]
        w = wyplaty[nr]
        rows.append({
            'nr': nr, 'date': wyplaty[nr]['date'], 'nums': nums,
            'f_b': sum(1 for n in nums if n <= BIRTHDAY_MAX) / 6,
            'f_r': sum(1 for n in nums if n in ROUND_NUMBERS) / 6,
            'w3': w[3], 'w4': w[4], 'w5': w[5], 'w6': w[6],
        })
    return rows


def fit_wls(rows):
    """WLS: y_d = alpha_4*[d==4] + alpha_5*[d==5] + b*(d*f_b) + r*(d*f_r).
    Zwraca (b, r, se_b, se_r, y, yhat)."""
    X, Y, W = [], [], []
    for row in rows:
        for d in (4, 5):
            wd = row[f'w{d}']
            if wd <= 0 or row['w3'] <= 0:
                continue
            y = math.log(wd / row['w3']) - math.log(P[d] / P[3])
            X.append([1.0 if d == 4 else 0.0, 1.0 if d == 5 else 0.0,
                      d * row['f_b'], d * row['f_r']])
            Y.append(y)
            W.append(1.0 / (1.0 / wd + 1.0 / row['w3']))
    n = len(Y)
    k = 4
    # rozwiązanie WLS przez normal equations (deterministyczne)
    XtWX = [[sum(X[i][a] * X[i][b_] * W[i] for i in range(n)) for b_ in range(k)]
            for a in range(k)]
    XtWY = [sum(X[i][a] * Y[i] * W[i] for i in range(n)) for a in range(k)]
    beta = solve4(XtWX, XtWY)
    resid = [Y[i] - sum(X[i][a] * beta[a] for a in range(k)) for i in range(n)]
    sigma2 = sum(W[i] * resid[i] ** 2 for i in range(n)) / max(1, n - k)
    inv = inv4(XtWX)
    se = [math.sqrt(max(0.0, sigma2 * inv[a][a])) for a in range(k)]
    yhat = [sum(X[i][a] * beta[a] for a in range(k)) for i in range(n)]
    return beta[2], beta[3], se[2], se[3], Y, yhat


def solve4(A, b):
    """Eliminacja Gaussa z częściowym pivotem (stały rozmiar 4x4)."""
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for col in range(4):
        piv = max(range(col, 4), key=lambda r: abs(M[r][col]))
        M[col], M[piv] = M[piv], M[col]
        for r in range(col + 1, 4):
            f = M[r][col] / M[col][col]
            M[r] = [m - f * mc for m, mc in zip(M[r], M[col])]
    x = [0.0] * 4
    for r in range(3, -1, -1):
        x[r] = (M[r][4] - sum(M[r][c] * x[c] for c in range(r + 1, 4))) / M[r][r]
    return x


def inv4(A):
    """Odwrotność 4x4 przez Gaussa-Jordana (do błędów standardowych)."""
    M = [row[:] + [1.0 if i == j else 0.0 for j in range(4)] for i, row in enumerate(A)]
    for col in range(4):
        piv = max(range(col, 4), key=lambda r: abs(M[r][col]))
        M[col], M[piv] = M[piv], M[col]
        d = M[col][col]
        M[col] = [m / d for m in M[col]]
        for r in range(4):
            if r != col:
                f = M[r][col]
                M[r] = [m - f * mc for m, mc in zip(M[r], M[col])]
    return [row[4:] for row in M]


def pearson(xs, ys):
    mx, my = statistics.mean(xs), statistics.mean(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    return cov / math.sqrt(vx * vy) if vx > 0 and vy > 0 else 0.0


def validate_hits6(test_rows, b):
    """Walidacja 6/6: częstość trafień (ktoś trafił) w dolnej vs górnej
    połowie score zestawu wg skalibrowanej wagi. Sygnał wtórny — główna
    estymacja bierze się ze stopni 4-5."""
    def score(nums):
        return sum(math.exp(b) if n <= BIRTHDAY_MAX else 1.0 for n in nums) / 6
    srt = sorted(test_rows, key=lambda r: score(r['nums']))
    half = len(srt) // 2
    lo, hi = srt[:half], srt[half:]
    rate_lo = sum(1 for r in lo if r['w6'] > 0) / len(lo) if lo else None
    rate_hi = sum(1 for r in hi if r['w6'] > 0) / len(hi) if hi else None
    return rate_lo, rate_hi


def main():
    dry_run = '--dry-run' in sys.argv
    rows = load_data()
    if len(rows) < MIN_DRAWS:
        print(f'Za mało wspólnych losowań ({len(rows)} < {MIN_DRAWS}) — przerwano.')
        sys.exit(1)
    split = int(len(rows) * (1 - TEST_FRACTION))
    train, test = rows[:split], rows[split:]

    b, r, se_b, se_r, y_tr, yhat_tr = fit_wls(train)
    corr_train = pearson(y_tr, yhat_tr)

    # out-of-sample: predykcja z wagami z train
    def predict(row_list):
        Yt, Ph = [], []
        for row in row_list:
            for d in (4, 5):
                wd = row[f'w{d}']
                if wd <= 0 or row['w3'] <= 0:
                    continue
                y = math.log(wd / row['w3']) - math.log(P[d] / P[3])
                Yt.append(y)
                # alpha_d z train oszacowane ponownie jako średnia residuów —
                # dla korelacji wystarczy sam składnik cech (stała nie wpływa)
                Ph.append(d * (b * row['f_b'] + r * row['f_r']))
        return pearson(Yt, Ph)
    corr_test = predict(test)

    rate_lo, rate_hi = validate_hits6(test, b)

    w, wr = round(math.exp(b), 4), round(math.exp(r), 4)
    data = {'b': round(b, 4), 'w': w, 'r': round(r, 4), 'wr': wr,
            'n': len(rows), 'nTest': len(test),
            'corrTrain': round(corr_train, 3), 'corrTest': round(corr_test, 3),
            'hit6Lo': round(rate_lo, 3) if rate_lo is not None else None,
            'hit6Hi': round(rate_hi, 3) if rate_hi is not None else None,
            'stanNa': rows[-1]['date']}

    print(f'losowań: {len(rows)} (train {len(train)} / test {len(test)}), '
          f'{rows[0]["date"]}..{rows[-1]["date"]}')
    print(f'b = {b:+.4f} ± {se_b:.4f}  -> waga urodzinowa {w} (heurystyka: 3.0)')
    print(f'r = {r:+.4f} ± {se_r:.4f}  -> waga okrągła    {wr} (heurystyka: 1.3)')
    print(f'korelacja dopasowania: train {corr_train:.3f} / test {corr_test:.3f}')
    if rate_lo is not None:
        print(f'walidacja 6/6 (test): częstość trafień lo-score {rate_lo:.1%} '
              f'vs hi-score {rate_hi:.1%}')

    const_line = 'const POPULARNOSC_KALIBR_JSON = ' + json.dumps(
        data, ensure_ascii=False, separators=(',', ':')) + ';'
    src = open(INDEX_PATH, encoding='utf-8').read()
    new_src, n = KALIBR_RE.subn(lambda _: const_line, src, count=1)
    if n != 1:
        print('BŁĄD: nie znaleziono zakotwiczonej linii POPULARNOSC_KALIBR_JSON')
        sys.exit(1)
    if new_src == src:
        print('wynik identyczny z wbudowanym — bez zapisu (idempotencja)')
        return
    if dry_run:
        print('--dry-run: zmiana NIE zapisana')
        print(const_line)
        return
    open(INDEX_PATH, 'w', encoding='utf-8').write(new_src)
    print('index.html: zaktualizowano POPULARNOSC_KALIBR_JSON')


if __name__ == '__main__':
    main()
