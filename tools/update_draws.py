#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Sonda aktualizacji wyników — Kalkulator Lotto.

Przepływ danych (jeden kierunek, CSV = źródło prawdy):

  LOTTO OpenAPI ──→ dopiski do data/*.csv ──→ przebudowa bloba w index.html z CSV
                ──→ nadpis data/kumulacje.csv (stan bieżący, nie historia)
                    ──→ wbudowanie kumulacji w index.html (const KUMULACJE_JSON,
                        czytany przez zakładkę EV kalkulatora; v4.11.0)
                ──→ dopiski do data/wyplaty_lotto.csv (faktyczne wypłaty per stopień)
                    ──→ wbudowanie median z 30 losowań w index.html
                        (const WYPLATY_JSON; v4.12.0)

Zasady:
  * format CSV 1:1 z wynikilotto.net.pl (LF, bez nagłówka, nr z zerami wiodącymi,
    DD.MM.YYYY, Multi Multi z kolumną HH:MM i liczbą Plus, liczby rosnąco 2-cyfrowe)
  * append-only: istniejące linie CSV nigdy nie są modyfikowane; konflikt numeru
    losowania = błąd krytyczny (nie nadpisujemy danych)
  * walidacja nowych rekordów tymi samymi regułami co kalkulator
    (pula / liczność / unikalność / pula pola dodatkowego)
  * deterministycznie: te same wejścia -> identyczne bajty (gzip mtime=0,
    stała kolejność kluczy, brak znaczników czasu generowania w danych)
  * idempotentnie: brak nowych danych -> zero zmian w plikach -> brak commitu
  * backfill: luka w danych (np. tydzień przestoju) uzupełniana dzień po dniu

Wymagania: Python 3.9+ (tylko stdlib), zmienna LOTTO_API_KEY.
Kody wyjścia: 0 = OK (także gdy brak nowości), 1 = błąd.
"""

import base64
import gzip
import json
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

API_BASE = 'https://developers.lotto.pl/api/open/v1/'
WARSAW = ZoneInfo('Europe/Warsaw')
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX_PATH = os.path.join(REPO_ROOT, 'index.html')
DATA_DIR = os.path.join(REPO_ROOT, 'data')
JACKPOTS_PATH = os.path.join(DATA_DIR, 'kumulacje.csv')
BLOB_RE = re.compile(r'^const HIST_DATA_B64 = "H4sI[^"]*"', re.M)
KUMULACJE_RE = re.compile(r'^const KUMULACJE_JSON = \{[^\n]+\};$', re.M)

MAX_BACKFILL_DAYS = 370   # bezpiecznik na wypadek uszkodzonej bazy
REQUEST_PAUSE_S = 0.3
HTTP_RETRIES = 3

# Kolejność kluczy w blobie JSON (jak w dotychczasowych wersjach index.html).
BLOB_KEY_ORDER = ['ekstra_premia', 'ekstra_pensja', 'multi_multi', 'mini_lotto',
                  'lotto_plus', 'lotto', 'eurojackpot']

# Specyfikacja CSV per gra — zgodna z CSV_SPEC w index.html (import kalkulatora).
CSV_SPEC = {
    'lotto':         {'main': 6,  'time': False, 'extra': 0},
    'lotto_plus':    {'main': 6,  'time': False, 'extra': 0},
    'multi_multi':   {'main': 20, 'time': True,  'extra': 1},
    'mini_lotto':    {'main': 5,  'time': False, 'extra': 0},
    'eurojackpot':   {'main': 5,  'time': False, 'extra': 2},
    'ekstra_pensja': {'main': 5,  'time': False, 'extra': 1},
    'ekstra_premia': {'main': 5,  'time': False, 'extra': 1},
}
POOL = {'lotto': 49, 'lotto_plus': 49, 'multi_multi': 80, 'mini_lotto': 42,
        'eurojackpot': 50, 'ekstra_pensja': 35, 'ekstra_premia': 35}
EXTRA_POOL = {'eurojackpot': 12, 'ekstra_pensja': 4, 'ekstra_premia': 4}

# Rodziny API: jedno zapytanie zwraca kilka gier (zweryfikowane na openapi.json
# i żywych odpowiedziach: Lotto->Lotto+LottoPlus, EkstraPensja->EP+EkstraPremia).
FAMILY_QUERIES = ['Lotto', 'MultiMulti', 'MiniLotto', 'EuroJackpot', 'EkstraPensja']
API_TO_DB = {'Lotto': 'lotto', 'LottoPlus': 'lotto_plus', 'MultiMulti': 'multi_multi',
             'MiniLotto': 'mini_lotto', 'EuroJackpot': 'eurojackpot',
             'EkstraPensja': 'ekstra_pensja', 'EkstraPremia': 'ekstra_premia'}
GAME_TO_FAMILY = {'lotto': 'Lotto', 'lotto_plus': 'Lotto', 'multi_multi': 'MultiMulti',
                  'mini_lotto': 'MiniLotto', 'eurojackpot': 'EuroJackpot',
                  'ekstra_pensja': 'EkstraPensja', 'ekstra_premia': 'EkstraPensja'}

# Gry kumulacyjne wg openapi.json („Wartość kumulacji dla gier kumulacyjnych
# – Lotto oraz Eurojackpot. Dla pozostałych zwraca najwyższą wartość możliwą
# do wygrania."). Kolejność wierszy w kumulacje.csv jest stała.
JACKPOT_GAMES = [('lotto', 'Lotto'), ('eurojackpot', 'EuroJackpot')]

# Numeracja: dla tych gier drawSystemId z API == numer losowania w bazie
# (zweryfikowane na danych 04.08.2026: Lotto 7387, MM 16943, EP 3741 itd.).
# Eurojackpot ma w API osobną numerację (692 przy bazowym 0978) — tam numery
# nadajemy sekwencyjnie, kontynuując bazę (tak samo robi wynikilotto).
ALIGNED_ID_GAMES = {'lotto', 'lotto_plus', 'multi_multi', 'mini_lotto',
                    'ekstra_pensja', 'ekstra_premia'}

# ---------- wypłaty Lotto (v4.12.0) ----------
# Faktyczne wypłaty per stopień z endpointu draw-prizes — źródło domyślnych
# „Szac. wygrana czwórka/piątka" w panelu kumulacji kalkulatora.
WYPLATY_PATH = os.path.join(DATA_DIR, 'wyplaty_lotto.csv')
WYPLATY_RE = re.compile(r'^const WYPLATY_JSON = \{[^\n]+\};$', re.M)
WYPLATY_BACKFILL = 60   # losowań wstecz przy pierwszym uruchomieniu
WYPLATY_MEDIAN_N = 30   # okno mediany dla agregatu wbudowywanego w index.html
LOTTO_DEG_TO_HITS = {'1': 6, '2': 5, '3': 4, '4': 3}  # stopień API -> trafienia


def log(msg):
    print(msg, flush=True)


def fail(msg):
    log(f'BŁĄD: {msg}')
    sys.exit(1)


def api_get(path, params, api_key, fatal=True):
    """GET z nagłówkiem secret; retry na błędy sieci/5xx.
    fatal=False -> zwraca None zamiast przerywać (dane dodatkowe)."""
    url = API_BASE + path + '?' + urllib.parse.urlencode(params)
    last_err = None
    for attempt in range(1, HTTP_RETRIES + 1):
        req = urllib.request.Request(url, headers={
            'accept': 'application/json',
            'secret': api_key,
            'User-Agent': 'lotto-kalkulator-sonda/1.0',
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8', 'replace')[:300]
            if e.code == 401:
                fail(f'HTTP 401 — klucz API odrzucony (endpoint {path})')
            if e.code == 404:
                return None
            last_err = f'HTTP {e.code}: {body}'
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last_err = str(e)
        if attempt < HTTP_RETRIES:
            time.sleep(2 * attempt)
    if fatal:
        fail(f'zapytanie nie powiodło się po {HTTP_RETRIES} próbach ({path}): {last_err}')
    log(f'  UWAGA: pomijam {path} ({last_err}) — kumulacje bez aktualizacji')
    return None


# ---------- CSV: odczyt, walidacja, zapis ----------

def parse_line(line, game):
    """Linia CSV -> rekord [nr, data, liczby(, extra)] albo None (uszkodzona).
    Logika zgodna z parseCsvForGame + normalizeMmExtra w kalkulatorze."""
    spec = CSV_SPEC[game]
    parts = [p.strip() for p in line.split(',')]
    need = 2 + (1 if spec['time'] else 0) + spec['main'] + spec['extra']
    if len(parts) < need:
        return None
    nr, date = parts[0], parts[1]
    if not re.fullmatch(r'\d+', nr) or not re.fullmatch(r'\d{2}\.\d{2}\.\d{4}', date):
        return None
    i0 = 2 + (1 if spec['time'] else 0)
    main = parts[i0:i0 + spec['main']]
    if not all(re.fullmatch(r'\d+', p) for p in main):
        return None
    rec = [nr, date, ','.join(main)]
    if spec['extra'] > 0:
        ext = parts[i0 + spec['main']:i0 + spec['main'] + spec['extra']]
        if not all(re.fullmatch(r'\d+', p) for p in ext):
            return None
        es = ','.join(ext)
        if not (game == 'multi_multi' and es == '00'):
            rec.append(es)
    return rec


def validate_record(rec, game):
    """Reguły isValidHistDraw z kalkulatora. Zwraca powód odrzucenia lub None."""
    spec = CSV_SPEC[game]
    nums = [int(x) for x in rec[2].split(',')]
    if len(nums) != spec['main']:
        return f'zła liczba liczb ({len(nums)} zamiast {spec["main"]})'
    if any(n < 1 or n > POOL[game] for n in nums):
        return f'liczby spoza puli 1-{POOL[game]}'
    if len(set(nums)) != len(nums):
        return 'duplikat liczby w losowaniu'
    ep = EXTRA_POOL.get(game)
    if ep and len(rec) > 3:
        ex = [int(x) for x in rec[3].split(',')]
        if any(n < 1 or n > ep for n in ex):
            return f'pole dodatkowe spoza 1-{ep}'
    return None


def load_csv(game):
    """Czyta data/<gra>.csv. Zwraca (linie_surowe, rekordy). Plik musi istnieć."""
    path = os.path.join(DATA_DIR, f'{game}.csv')
    if not os.path.exists(path):
        fail(f'brak pliku data/{game}.csv')
    lines = [l for l in open(path, encoding='utf-8').read().split('\n') if l.strip()]
    records = []
    for l in lines:
        rec = parse_line(l, game)
        if rec is None:
            fail(f'uszkodzona linia w data/{game}.csv: {l[:60]}')
        records.append(rec)
    if not records:
        fail(f'data/{game}.csv jest pusty')
    return lines, records





# ---------- pobieranie z API ----------

def fetch_family_for_date(family, day, api_key):
    resp = api_get('lotteries/draw-results/by-date-per-game', {
        'gameType': family,
        'drawDate': f'{day.isoformat()}T00:00:00Z',
        'index': 1, 'size': 10, 'sort': 'drawDate', 'order': 'ASC',
    }, api_key)
    if not resp:
        return []
    items = resp.get('items') if isinstance(resp, dict) else resp
    return items or []


def item_to_record(game, item):
    """Rekord API -> (rekord [nr='', data, liczby(, extra)], czas HH:MM Warszawy).
    None = rekord odrzucony walidacją (z logiem)."""
    res = (item.get('results') or [{}])[0]
    nums = res.get('resultsJson') or []
    extra = res.get('specialResults') or []
    dt = datetime.fromisoformat(item['drawDate'].replace('Z', '+00:00')).astimezone(WARSAW)
    rec = ['', dt.strftime('%d.%m.%Y'), ','.join(f'{n:02d}' for n in sorted(nums))]
    if CSV_SPEC[game]['extra'] > 0 and extra:
        rec.append(','.join(f'{n:02d}' for n in extra))
    reason = validate_record(rec, game)
    if reason:
        log(f'  ODRZUCONO {game} losowanie {item.get("drawSystemId")}: {reason}')
        return None
    return rec, dt.strftime('%H:%M')


def record_to_line(game, nr, rec, time_hm):
    """Rekord -> linia CSV w formacie wynikilotto."""
    parts = [nr, rec[1]]
    if CSV_SPEC[game]['time']:
        parts.append(time_hm)
    parts.append(rec[2])
    if len(rec) > 3:
        parts.append(rec[3])
    return ','.join(parts)


# ---------- kumulacje ----------

def build_jackpots_csv(api_key):
    """Składa zawartość data/kumulacje.csv. None = pomiń (błąd API, nie blokuje)."""
    rows = []
    for db_key, api_type in JACKPOT_GAMES:
        resp = api_get('lotteries/info/game-jackpot', {'gameType': api_type},
                       api_key, fatal=False)
        if not resp or not resp.get('jackpotValue'):
            return None
        dt = datetime.fromisoformat(resp['closestDraw'].replace('Z', '+00:00')).astimezone(WARSAW)
        rows.append(f'{db_key},{int(round(resp["jackpotValue"]))},{dt.strftime("%d.%m.%Y")}')
    return '\n'.join(rows) + '\n'


# ---------- blob w index.html ----------

def rebuild_blob():
    """Buduje bazę JSON z data/*.csv i podmienia blob w index.html.
    Zwraca True, gdy plik się zmienił."""
    db = {}
    for game in BLOB_KEY_ORDER:
        _, records = load_csv(game)
        db[game] = records
    payload = json.dumps(db, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    b64 = base64.b64encode(gzip.compress(payload, compresslevel=9, mtime=0)).decode('ascii')
    if not b64.startswith('H4sI'):
        fail('wygenerowany blob nie ma nagłówka gzip')
    src = open(INDEX_PATH, encoding='utf-8').read()
    new_src, n = BLOB_RE.subn(lambda _: f'const HIST_DATA_B64 = "{b64}"', src, count=1)
    if n != 1:
        fail('nie znaleziono zakotwiczonej linii HIST_DATA_B64 w index.html')
    if new_src == src:
        return False
    open(INDEX_PATH, 'w', encoding='utf-8').write(new_src)
    return True


def embed_jackpots(jk_csv_text):
    """Wbudowuje kumulacje z data/kumulacje.csv do index.html
    (const KUMULACJE_JSON, czytany przez zakładkę EV kalkulatora).
    Stała kolejność kluczy = JACKPOT_GAMES. Zwraca True, gdy plik się zmienił."""
    data = {}
    for line in jk_csv_text.strip().split('\n'):
        game, value, date = line.split(',')
        data[game] = {'value': int(value), 'date': date}
    ordered = {game: data[game] for game, _ in JACKPOT_GAMES if game in data}
    const_line = 'const KUMULACJE_JSON = ' + json.dumps(
        ordered, ensure_ascii=False, separators=(',', ':')) + ';'
    src = open(INDEX_PATH, encoding='utf-8').read()
    new_src, n = KUMULACJE_RE.subn(lambda _: const_line, src, count=1)
    if n != 1:
        fail('nie znaleziono zakotwiczonej linii KUMULACJE_JSON w index.html')
    if new_src == src:
        return False
    open(INDEX_PATH, 'w', encoding='utf-8').write(new_src)
    return True


# ---------- wypłaty Lotto ----------

def fetch_lotto_prize_rows(draw_id, api_key):
    """Wiersze CSV wypłat dla losowania Lotto (gameType=Lotto).
    None = wypłaty jeszcze nieopublikowane — dokończymy przy następnym runie."""
    items = api_get(f'lotteries/draw-prizes/Lotto/{draw_id}', {}, api_key, fatal=False)
    if not items:
        return None
    lotto = [i for i in items if i.get('gameType') == 'Lotto']
    if len(lotto) != 1 or lotto[0].get('prizesEmpty'):
        return None
    it = lotto[0]
    dt = datetime.fromisoformat(it['drawDate'].replace('Z', '+00:00')).astimezone(WARSAW)
    date_pl = dt.strftime('%d.%m.%Y')
    rows = []
    for deg in ('1', '2', '3', '4'):
        p = it['prizes'].get(deg)
        if p is None:
            return None
        rows.append(f'{draw_id},{date_pl},{LOTTO_DEG_TO_HITS[deg]},'
                    f'{p["prize"]},{p["prizeValue"]:.2f}')
    return rows


def update_wyplaty(api_key):
    """Dopisuje brakujące wypłaty do data/wyplaty_lotto.csv (append-only,
    po numerze losowania). Zwraca True przy dopisku."""
    _, lotto_records = load_csv('lotto')
    last_nr = int(lotto_records[-1][0])
    existing = []
    if os.path.exists(WYPLATY_PATH):
        with open(WYPLATY_PATH, encoding='utf-8') as f:
            existing = [l.rstrip('\n') for l in f if l.strip()]
    last_done = int(existing[-1].split(',')[0]) if existing else last_nr - WYPLATY_BACKFILL
    new_rows = []
    nr = last_done + 1
    while nr <= last_nr:
        rows = fetch_lotto_prize_rows(nr, api_key)
        if rows is None:
            log(f'  wypłaty: nr {nr} jeszcze nieopublikowane — dokończę później')
            break
        new_rows.extend(rows)
        time.sleep(REQUEST_PAUSE_S)
        nr += 1
    if not new_rows:
        return False
    with open(WYPLATY_PATH, 'a', encoding='utf-8', newline='') as f:
        f.write('\n'.join(new_rows) + '\n')
    log(f'wyplaty_lotto.csv: +{len(new_rows)} wierszy (do nr {new_rows[-1].split(",")[0]})')
    return True


def embed_wyplaty():
    """Mediana wypłat 4/6 i 5/6 z WYPLATY_MEDIAN_N ostatnich losowań ->
    const WYPLATY_JSON w index.html (stała kolejność kluczy)."""
    by_nr = {}
    with open(WYPLATY_PATH, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            nr, date, hits, _cnt, kwota = line.split(',')
            by_nr.setdefault(int(nr), {'date': date})[hits] = float(kwota)
    last_nrs = sorted(by_nr)[-WYPLATY_MEDIAN_N:]
    data = {'t4': round(statistics.median(by_nr[n]['4'] for n in last_nrs), 2),
            't5': round(statistics.median(by_nr[n]['5'] for n in last_nrs), 2),
            'n': len(last_nrs), 'stanNa': by_nr[last_nrs[-1]]['date']}
    const_line = 'const WYPLATY_JSON = ' + json.dumps(
        data, ensure_ascii=False, separators=(',', ':')) + ';'
    src = open(INDEX_PATH, encoding='utf-8').read()
    new_src, n = WYPLATY_RE.subn(lambda _: const_line, src, count=1)
    if n != 1:
        fail('nie znaleziono zakotwiczonej linii WYPLATY_JSON w index.html')
    if new_src == src:
        return False
    open(INDEX_PATH, 'w', encoding='utf-8').write(new_src)
    return True


# ---------- przebieg główny ----------

def main():
    api_key = os.environ.get('LOTTO_API_KEY', '').strip()
    if not api_key:
        fail('brak zmiennej środowiskowej LOTTO_API_KEY')

    today = datetime.now(WARSAW).date()
    total_added = 0

    csv_records = {}
    last_dates = {}
    known = {}
    last_nr = {}
    nr_width = {}
    for game in CSV_SPEC:
        _, records = load_csv(game)
        csv_records[game] = records
        last_dates[game] = records[-1][1]
        known[game] = {(r[1], r[2]) for r in records}
        last_nr[game] = int(records[-1][0])
        nr_width[game] = len(records[-1][0])

    # Zakres sondowania rodziny: od najstarszego brakującego dnia w jej grach.
    family_first_day = {}
    for game in CSV_SPEC:
        family = GAME_TO_FAMILY[game]
        day = datetime.strptime(last_dates[game], '%d.%m.%Y').date() + timedelta(days=1)
        family_first_day[family] = min(family_first_day.get(family, day), day)

    new_lines = {game: [] for game in CSV_SPEC}
    for family in FAMILY_QUERIES:
        day = family_first_day[family]
        if (today - day).days > MAX_BACKFILL_DAYS:
            fail(f'{family}: luka {(today - day).days} dni > {MAX_BACKFILL_DAYS} — sprawdź bazę')
        while day <= today:
            for item in fetch_family_for_date(family, day, api_key):
                time.sleep(REQUEST_PAUSE_S)
                game = API_TO_DB.get(item.get('gameType'))
                if not game:
                    continue
                built = item_to_record(game, item)
                if built is None:
                    continue
                rec, time_hm = built
                if (rec[1], rec[2]) in known[game]:
                    continue
                api_id = item.get('drawSystemId')
                if game in ALIGNED_ID_GAMES:
                    if not isinstance(api_id, int) or api_id <= last_nr[game]:
                        # Twarde zabezpieczenie append-only: nowe losowanie gry
                        # o zsynchronizowanej numeracji MUSI mieć świeży numer.
                        # Numer już w pliku przy nieznanej treści = konflikt danych.
                        fail(f'{game}: konflikt numeracji — API zwróciło drawSystemId '
                             f'{api_id} przy ostatnim numerze w bazie {last_nr[game]} '
                             f'({rec[1]} {rec[2]}). Przerwano bez zapisu.')
                    last_nr[game] = api_id
                else:
                    last_nr[game] = last_nr[game] + 1
                nr = str(last_nr[game]).zfill(nr_width[game])
                new_lines[game].append(record_to_line(game, nr, rec, time_hm))
                known[game].add((rec[1], rec[2]))
                total_added += 1
            day += timedelta(days=1)

    # Zapis dopisków (append-only)
    for game in CSV_SPEC:
        if not new_lines[game]:
            continue
        path = os.path.join(DATA_DIR, f'{game}.csv')
        with open(path, 'a', encoding='utf-8', newline='') as f:
            f.write('\n'.join(new_lines[game]) + '\n')
        log(f'{game}: +{len(new_lines[game])} losowań, ostatnie: '
            f'{new_lines[game][-1].split(",")[1]}')

    # Kumulacje (soft-fail: błąd endpointu nie blokuje wyników)
    jackpots_changed = False
    jk_embedded = False
    jk = build_jackpots_csv(api_key)
    if jk is not None:
        old = open(JACKPOTS_PATH, encoding='utf-8').read() if os.path.exists(JACKPOTS_PATH) else None
        if jk != old:
            open(JACKPOTS_PATH, 'w', encoding='utf-8', newline='').write(jk)
            jackpots_changed = True
            log('kumulacje.csv: zaktualizowano -> ' + ' | '.join(jk.strip().split('\n')))
        else:
            log('kumulacje.csv: bez zmian')
        # v4.11.0: kumulacje wbudowane w index.html (zakładka EV czyta je z pliku)
        jk_embedded = embed_jackpots(jk)
        if jk_embedded:
            log('index.html: wbudowano zaktualizowane kumulacje (KUMULACJE_JSON)')

    # Wypłaty Lotto (v4.12.0, soft-fail: błąd nie blokuje wyników)
    wyplaty_changed = update_wyplaty(api_key)
    wyplaty_embedded = embed_wyplaty() if os.path.exists(WYPLATY_PATH) else False
    if wyplaty_embedded:
        log('index.html: wbudowano zaktualizowane mediany wypłat (WYPLATY_JSON)')

    # Przebudowa bloba z CSV (zawsze z aktualnych plików)
    blob_changed = rebuild_blob() if total_added > 0 else False

    if (total_added == 0 and not jackpots_changed and not jk_embedded
            and not wyplaty_changed and not wyplaty_embedded):
        log('Brak nowych danych — repo bez zmian.')
        write_github_output('false', '')
        return

    through = max(datetime.strptime(d, '%d.%m.%Y').date() for d in last_dates.values())
    if total_added > 0:
        through = max(through, max(
            datetime.strptime(l.split(',')[1], '%d.%m.%Y').date()
            for g in CSV_SPEC for l in new_lines[g]))
        summary = f'data: wyniki do {through.strftime("%d.%m.%Y")}'
        log(f'Dopisano łącznie {total_added} losowań. Blob przebudowany: {blob_changed}.')
    else:
        if jackpots_changed or jk_embedded:
            summary = 'data: kumulacje ' + ', '.join(l.split(',')[0] + ' ' + l.split(',')[1]
                                                     for l in jk.strip().split('\n'))
        else:
            summary = 'data: wypłaty lotto (mediana 30 losowań)'
        log(summary)
    write_github_output('true', summary)


def write_github_output(changed, summary):
    out = os.environ.get('GITHUB_OUTPUT')
    if not out:
        return
    with open(out, 'a', encoding='utf-8') as f:
        f.write(f'changed={changed}\n')
        f.write(f'summary={summary}\n')


if __name__ == '__main__':
    main()
