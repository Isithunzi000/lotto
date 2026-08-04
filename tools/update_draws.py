#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Aktualizacja bazy losowań osadzonej w index.html z oficjalnego LOTTO OpenAPI.

Działanie:
  1. Dekoduje HIST_DATA_B64 (gzip+base64) z index.html.
  2. Dla każdej gry pobiera z API brakujące dni (od dnia po ostatnim rekordzie
     do dziś, czas Europe/Warsaw).
  3. Dopisuje rekordy w IDENTYCZNYM formacie co istniejące:
     [nr, "DD.MM.YYYY", "liczby,główne"[, "liczby,dodatkowe"]]
  4. Waliduje jak kalkulator (pula, liczność, unikalność) i deduplikuje po
     numerze losowania (drawSystemId z API = numer losowania, ciągłość z bazą
     zweryfikowana).
  5. Przepakowuje bazę (gzip mtime=0 — deterministycznie) i podmienia
     zakotwiczoną linię bloba w index.html.
  6. Brak nowych losowań -> plik bez zmian (idempotentnie).

Wymagania: Python 3.9+ (tylko biblioteka standardowa), zmienna LOTTO_API_KEY.
Kody wyjścia: 0 = OK (także gdy brak nowości), 1 = błąd (API, klucz, dane).
"""

import base64
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

API_BASE = 'https://developers.lotto.pl/api/open/v1/'
WARSAW = ZoneInfo('Europe/Warsaw')
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX_PATH = os.path.join(REPO_ROOT, 'index.html')
BLOB_RE = re.compile(r'^const HIST_DATA_B64 = "H4sI[^"]*"', re.M)

MAX_BACKFILL_DAYS = 370   # bezpiecznik: większa luka = coś nie tak z bazą/API
REQUEST_PAUSE_S = 0.3     # grzecznościowy odstęp między zapytaniami
HTTP_RETRIES = 3

# Gra w bazie -> (gameType API, liczba liczb głównych, pula, liczba dodatkowych, pula dodatkowych)
# Zgodne z HIST_GAME_POOL / HIST_GAME_DRAW_COUNT / HIST_GAME_EXTRA_POOL w index.html.
GAMES = {
    'lotto':         ('Lotto',        6, 49, 0, None),
    'lotto_plus':    ('LottoPlus',    6, 49, 0, None),
    'multi_multi':   ('MultiMulti',  20, 80, 1, None),
    'mini_lotto':    ('MiniLotto',    5, 42, 0, None),
    'eurojackpot':   ('EuroJackpot',  5, 50, 2, 12),
    'ekstra_pensja': ('EkstraPensja', 5, 35, 1, 4),
    'ekstra_premia': ('EkstraPremia', 5, 35, 1, 4),
}

# Rodziny: jedno zapytanie API zwraca kilka gier z bazy.
# (Lotto -> Lotto+LottoPlus, EkstraPensja -> EkstraPensja+EkstraPremia — zweryfikowane.)
FAMILY_QUERIES = ['Lotto', 'MultiMulti', 'MiniLotto', 'EuroJackpot', 'EkstraPensja']
API_TO_DB = {}
for db_key, spec in GAMES.items():
    API_TO_DB[spec[0]] = db_key


def log(msg):
    print(msg, flush=True)


def fail(msg):
    log(f'BŁĄD: {msg}')
    sys.exit(1)


def load_db():
    """Wyciąga i dekoduje bazę z index.html. Zwraca (tekst_pliku, obiekt_bazy)."""
    with open(INDEX_PATH, 'r', encoding='utf-8') as f:
        src = f.read()
    m = BLOB_RE.search(src)
    if not m:
        fail('nie znaleziono zakotwiczonej linii HIST_DATA_B64 w index.html')
    b64 = m.group(0).split('"')[1]
    try:
        data = json.loads(gzip.decompress(base64.b64decode(b64)).decode('utf-8'))
    except Exception as e:
        fail(f'nie można zdekodować bazy: {e}')
    for game in GAMES:
        data.setdefault(game, [])
    return src, data


def parse_db_date(s):
    return datetime.strptime(s, '%d.%m.%Y').date()


def api_get(path, params, api_key):
    """GET z nagłówkiem secret; retry na błędy sieci/5xx. Zwraca sparsowany JSON."""
    url = API_BASE + path + '?' + urllib.parse.urlencode(params)
    last_err = None
    for attempt in range(1, HTTP_RETRIES + 1):
        req = urllib.request.Request(url, headers={
            'accept': 'application/json',
            'secret': api_key,
            'User-Agent': 'lotto-kalkulator-actions/1.0',
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8', 'replace')[:300]
            if e.code == 401:
                fail(f'HTTP 401 — klucz API odrzucony (endpoint {path})')
            if e.code == 404:
                return None  # brak danych dla tych parametrów
            last_err = f'HTTP {e.code}: {body}'
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last_err = str(e)
        if attempt < HTTP_RETRIES:
            time.sleep(2 * attempt)
    fail(f'zapytanie nie powiodło się po {HTTP_RETRIES} próbach ({path}): {last_err}')


def fetch_family_for_date(family, day, api_key):
    """Wyniki jednej rodziny gier dla jednego dnia (data lokalna Warszawy)."""
    resp = api_get('lotteries/draw-results/by-date-per-game', {
        'gameType': family,
        'drawDate': f'{day.isoformat()}T00:00:00Z',
        'index': 1,
        'size': 10,
        'sort': 'drawDate',
        'order': 'ASC',
    }, api_key)
    if not resp:
        return []
    items = resp.get('items') if isinstance(resp, dict) else resp
    return items or []


def validate_record(game, nums, extra):
    """Te same reguły co rejectReason() w kalkulatorze. Zwraca None lub powód."""
    _, main_count, pool, extra_count, extra_pool = GAMES[game]
    if len(nums) != main_count:
        return f'zła liczba liczb ({len(nums)} zamiast {main_count})'
    if any(not isinstance(n, int) or n < 1 or n > pool for n in nums):
        return f'liczby spoza puli 1-{pool}'
    if len(set(nums)) != len(nums):
        return 'duplikat liczby w losowaniu'
    if extra_count > 0 and extra is not None:
        if len(extra) != extra_count:
            return f'zła liczba liczb dodatkowych ({len(extra)} zamiast {extra_count})'
        if extra_pool and any(not isinstance(n, int) or n < 1 or n > extra_pool for n in extra):
            return f'pole dodatkowe spoza 1-{extra_pool}'
    return None


def item_to_record(game, item):
    """Mapuje obiekt API na rekord bazy. None = rekord do pominięcia."""
    _, _, _, extra_count, _ = GAMES[game]
    res = (item.get('results') or [{}])[0]
    nums = res.get('resultsJson') or []
    extra = res.get('specialResults') or []
    reason = validate_record(game, nums, extra if extra_count > 0 else None)
    if reason:
        log(f'  POMINIĘTO {game} losowanie {item.get("drawSystemId")}: {reason}')
        return None
    # Data: API podaje UTC -> data lokalna Europe/Warsaw
    dt_utc = datetime.fromisoformat(item['drawDate'].replace('Z', '+00:00'))
    day = dt_utc.astimezone(WARSAW).date()
    rec = ['',  # nr nadpisywany w pętli głównej (kontynuacja numeracji bazy)
           day.strftime('%d.%m.%Y'),
           ','.join(f'{n:02d}' for n in nums)]
    if extra_count > 0 and extra:
        rec.append(','.join(f'{n:02d}' for n in extra))
    return rec


def main():
    api_key = os.environ.get('LOTTO_API_KEY', '').strip()
    if not api_key:
        fail('brak zmiennej środowiskowej LOTTO_API_KEY')

    src, db = load_db()
    today = datetime.now(WARSAW).date()
    total_added = 0

    for game in GAMES:
        rows = db[game]
        last_day = max(parse_db_date(r[1]) for r in rows)
        gap_days = (today - last_day).days
        if gap_days <= 0:
            log(f'{game}: aktualne (ostatnie {last_day.strftime("%d.%m.%Y")})')
            continue
        if gap_days > MAX_BACKFILL_DAYS:
            fail(f'{game}: luka {gap_days} dni > {MAX_BACKFILL_DAYS} — przerwano (sprawdź bazę)')
        family = GAMES[game][0]
        log(f'{game}: uzupełniam {gap_days} dni (od {last_day + timedelta(days=1)} do {today})')

        # Dedupe po (data, liczby) — odporne na różnice numeracji między API a bazą.
        # UWAGA: drawSystemId z API NIE zawsze jest numerem losowania z bazy
        # (Eurojackpot: API liczy osobno — 689 przy bazowym 0974), więc nie wolno
        # deduplikować po samym numerze. Numeracja nowych rekordów: drawSystemId,
        # gdy jest kontynuacją bazy (> ostatni numer), w przeciwnym razie kolejny
        # numer sekwencyjny — deterministycznie i zgodnie z dotychczasowym stylem.
        known = {(r[1], r[2]) for r in rows}
        nr_width = len(rows[-1][0])  # zachowaj styl wiodących zer (np. MM: 5 cyfr)
        last_nr = int(rows[-1][0])
        new_records = []
        day = last_day + timedelta(days=1)
        while day <= today:
            for item in fetch_family_for_date(family, day, api_key):
                time.sleep(REQUEST_PAUSE_S)
                if API_TO_DB.get(item.get('gameType')) != game:
                    continue
                rec = item_to_record(game, item)
                if rec is None:
                    continue
                if (rec[1], rec[2]) in known:
                    continue
                api_id = item.get('drawSystemId')
                last_nr = api_id if isinstance(api_id, int) and api_id > last_nr else last_nr + 1
                rec[0] = str(last_nr).zfill(nr_width)
                new_records.append(rec)
                known.add((rec[1], rec[2]))
            day += timedelta(days=1)

        new_records.sort(key=lambda r: (parse_db_date(r[1]), int(r[0])))
        db[game] = rows + new_records
        total_added += len(new_records)
        if new_records:
            log(f'  +{len(new_records)} losowań, ostatnie: {new_records[-1][1]}')

    if total_added == 0:
        log('Brak nowych wyników — index.html bez zmian.')
        write_github_output('false', '')
        return

    payload = json.dumps(db, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    b64 = base64.b64encode(gzip.compress(payload, compresslevel=9, mtime=0)).decode('ascii')
    if not b64.startswith('H4sI'):
        fail('wygenerowany blob nie ma nagłówka gzip — przerwano')
    new_src, n_subs = BLOB_RE.subn(f'const HIST_DATA_B64 = "{b64}"', src, count=1)
    if n_subs != 1:
        fail('podmiana bloba nie powiodła się')
    if new_src != src:
        with open(INDEX_PATH, 'w', encoding='utf-8') as f:
            f.write(new_src)
    through = max(parse_db_date(r[1]) for rows in db.values() for r in rows)
    log(f'Dopisano łącznie {total_added} losowań. Baza aktualna do {through.strftime("%d.%m.%Y")}.')
    write_github_output('true', through.strftime('%d.%m.%Y'))


def write_github_output(changed, through):
    out = os.environ.get('GITHUB_OUTPUT')
    if not out:
        return
    with open(out, 'a', encoding='utf-8') as f:
        f.write(f'changed={changed}\n')
        f.write(f'through={through}\n')


if __name__ == '__main__':
    main()
