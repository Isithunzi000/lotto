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
                ──→ dopiski do data/wyplaty_minilotto.csv i data/wyplaty_eurojackpot.csv
                    (v4.13.0 — mediany warunkowe Mini Lotto, średnie/mediany EJ V-XII)
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
import math
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

# ---------- wypłaty Mini Lotto i EuroJackpot (v4.13.0) ----------
# Mini Lotto: numeracja API == numeracja bazy (gra w ALIGNED_ID_GAMES).
# EuroJackpot: API ma własną numerację (drawSystemId 692 przy bazie 0978) —
# w wyplaty_eurojackpot.csv kolumna nr przechowuje ID z API (potrzebne do
# inkrementalnego pobierania z draw-prizes), data pozwala zmapować na bazę.
WYPLATY_MINI_PATH = os.path.join(DATA_DIR, 'wyplaty_minilotto.csv')
WYPLATY_EJ_PATH = os.path.join(DATA_DIR, 'wyplaty_eurojackpot.csv')
WYPLATY_MINI_RE = re.compile(r'^const WYPLATY_MINI_JSON = \{[^\n]+\};$', re.M)
WYPLATY_EJ_RE = re.compile(r'^const WYPLATY_EJ_JSON = \{[^\n]+\};$', re.M)
WYPLATY_EJ_BACKFILL = 40  # EJ: 2 losowania/tydz. — 40 ~= 20 tygodni
MINI_DEG_KEYS = ('1', '2', '3')              # 1=5/5, 2=4/5, 3=3/5
EJ_DEG_KEYS = tuple(str(i) for i in range(1, 13))   # stopnie I-XII
EJ_EMBED_DEGS = range(5, 13)                 # do embeda: V-XII (I-IV zbyt rzadkie/zmienne)

# ---------- sprzedaż zakładów Lotto (v4.14.0) ----------
# Estymacja z liczb zwycięzców (wyplaty_lotto.csv): sprzedaż ~= zwycięzcy / p.
# Szum Poissona przy ~38 tys. zwycięzców 3/6 to ~0,5% — zmienność między
# losowaniami to realna zmienność sprzedaży (rośnie z głębokością kumulacji).
SPRZEDAZ_PATH = None  # brak osobnego CSV — liczone czysto z wyplaty_lotto.csv
SPRZEDAZ_RE = re.compile(r'^const SPRZEDAZ_JSON = \{[^\n]+\};$', re.M)
SPRZEDAZ_WINDOW = 60  # ostatnich losowań (~5 mies.) — sprzedaż dryfuje w skali roku
LOTTO_P345 = (math.comb(6, 3) * math.comb(43, 3) + math.comb(6, 4) * math.comb(43, 2)
              + math.comb(6, 5) * math.comb(43, 1)) / math.comb(49, 6)
LOTTO_DEG_TO_HITS = {'1': 6, '2': 5, '3': 4, '4': 3}  # stopień API -> trafienia


def log(msg):
    print(msg, flush=True)


def fail(msg):
    log(f'BŁĄD: {msg}')
    sys.exit(1)


# Raportowanie sekcji opcjonalnych (wypłaty/mediany/sprzedaż) — ich błąd NIE
# może blokować commitu danych głównych (losowania, kumulacje, blob).
optional_failures = []   # etykiety sekcji, które się wywaliły
optional_notes = []      # nietypowe, ale obsłużone zdarzenia (np. pominięte luki)


def soft(label, fn, *args):
    """Odpala sekcję opcjonalną: błąd (także fail()/SystemExit) = ostrzeżenie
    i kontynuacja sondy, zgłoszona później przez output optfail. Zwraca False
    przy porażce — sekcje opcjonalne zwracają bool 'czy zmieniono', więc
    False jest bezpieczną wartością domyślną."""
    try:
        return fn(*args)
    except SystemExit:
        optional_failures.append(label)
        log(f'  UWAGA: sekcja opcjonalna „{label}" przerwana — kontynuuję sondę')
        return False
    except Exception as e:
        optional_failures.append(label)
        log(f'  UWAGA: sekcja opcjonalna „{label}" wywaliła się: {e} — kontynuuję sondę')
        return False


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

GAP_LOOKAHEAD = 5   # ile nowszych numerów sprawdzamy, zanim uznamy lukę za trwałą


def _gap_is_permanent(fetch_fn, nr, last_id, api_key):
    """True = któryś z GAP_LOOKAHEAD nowszych numerów ma już wypłaty, a nr nie,
    czyli luka jest trwała (a nie „jeszcze nieopublikowane"). Deterministyczne:
    zależy wyłącznie od stanu API. Dodatkowe zapytania tylko przy trafieniu luki."""
    for ahead in range(nr + 1, min(nr + GAP_LOOKAHEAD, last_id) + 1):
        if fetch_fn(ahead, api_key) is not None:
            return True
    return False


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
    skipped = []
    nr = last_done + 1
    while nr <= last_nr:
        rows = fetch_lotto_prize_rows(nr, api_key)
        if rows is None:
            if _gap_is_permanent(fetch_lotto_prize_rows, nr, last_nr, api_key):
                log(f'  OSTRZEŻENIE: wypłaty Lotto nr {nr} trwale niedostępne '
                    f'(nowsze już są) — pomijam lukę')
                skipped.append(nr)
                nr += 1
                continue
            log(f'  wypłaty: nr {nr} jeszcze nieopublikowane — dokończę później')
            break
        new_rows.extend(rows)
        time.sleep(REQUEST_PAUSE_S)
        nr += 1
    if skipped:
        optional_notes.append(f'pominięte trwałe luki wypłat Lotto: {skipped}')
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


# ---------- wypłaty Mini Lotto / EuroJackpot (v4.13.0) ----------

def fetch_game_prize_rows(draw_type, draw_id, deg_keys, api_key):
    """Wiersze CSV wypłat dowolnej gry z draw-prizes (format jak wyplaty_lotto.csv:
    nr,data,stopien,count,kwota). None = wypłaty jeszcze nieopublikowane."""
    items = api_get(f'lotteries/draw-prizes/{draw_type}/{draw_id}', {}, api_key, fatal=False)
    if not items:
        return None
    game = [i for i in items if i.get('gameType') == draw_type]
    if len(game) != 1 or game[0].get('prizesEmpty'):
        return None
    it = game[0]
    dt = datetime.fromisoformat(it['drawDate'].replace('Z', '+00:00')).astimezone(WARSAW)
    date_pl = dt.strftime('%d.%m.%Y')
    rows = []
    for deg in deg_keys:
        p = it['prizes'].get(deg)
        if p is None:
            return None
        rows.append(f'{draw_id},{date_pl},{deg},{p["prize"]},{p["prizeValue"]:.2f}')
    return rows


def update_wyplaty_game(path, draw_type, deg_keys, last_api_id, backfill, api_key):
    """Generyczny odpowiednik update_wyplaty: append-only po numerze losowania.
    Zwraca True przy dopisku."""
    existing = []
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            existing = [l.rstrip('\n') for l in f if l.strip()]
    last_done = int(existing[-1].split(',')[0]) if existing else last_api_id - backfill
    new_rows = []
    skipped = []
    nr = last_done + 1
    fetch_one = lambda n, key: fetch_game_prize_rows(draw_type, n, deg_keys, key)
    while nr <= last_api_id:
        rows = fetch_one(nr, api_key)
        if rows is None:
            if _gap_is_permanent(fetch_one, nr, last_api_id, api_key):
                log(f'  OSTRZEŻENIE: wypłaty {draw_type} nr {nr} trwale niedostępne '
                    f'(nowsze już są) — pomijam lukę')
                skipped.append(nr)
                nr += 1
                continue
            log(f'  wypłaty {draw_type}: nr {nr} jeszcze nieopublikowane — dokończę później')
            break
        new_rows.extend(rows)
        time.sleep(REQUEST_PAUSE_S)
        nr += 1
    if skipped:
        optional_notes.append(f'pominięte trwałe luki wypłat {draw_type}: {skipped}')
    if not new_rows:
        return False
    with open(path, 'a', encoding='utf-8', newline='') as f:
        f.write('\n'.join(new_rows) + '\n')
    log(f'{os.path.basename(path)}: +{len(new_rows)} wierszy (do nr {new_rows[-1].split(",")[0]})')
    return True


def _embed_const(const_name, regex, data):
    """Podmienia zakotwiczoną linię const w index.html. True = zmieniono."""
    const_line = f'const {const_name} = ' + json.dumps(
        data, ensure_ascii=False, separators=(',', ':')) + ';'
    src = open(INDEX_PATH, encoding='utf-8').read()
    new_src, n = regex.subn(lambda _: const_line, src, count=1)
    if n != 1:
        fail(f'nie znaleziono zakotwiczonej linii {const_name} w index.html')
    if new_src == src:
        return False
    open(INDEX_PATH, 'w', encoding='utf-8').write(new_src)
    return True


def embed_wyplaty_mini():
    """Mediany warunkowe wypłat Mini Lotto z WYPLATY_MEDIAN_N ostatnich losowań:
    osobno dla losowań, w których padła 5/5 (pula I stopnia do zwycięzcy) i w
    których nie padła (pula rozlewa się na stopnie II i III — wypłaty ~2x wyższe).
    Kalkulator liczy wartość domyślną jako średnią ważoną obu scenariuszy (w).
    -> const WYPLATY_MINI_JSON w index.html (stała kolejność kluczy)."""
    by_nr = {}
    with open(WYPLATY_MINI_PATH, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            nr, date, deg, cnt, kwota = line.split(',')
            by_nr.setdefault(int(nr), {'date': date})[deg] = (int(cnt), float(kwota))
    last_nrs = sorted(by_nr)[-WYPLATY_MEDIAN_N:]
    # Klucze CSV = surowe stopnie API: '1' = 5/5, '2' = 4/5, '3' = 3/5.
    padla = [n for n in last_nrs if by_nr[n]['1'][0] > 0]
    bez = [n for n in last_nrs if by_nr[n]['1'][0] == 0]
    def med(deg, nrs):
        vals = [by_nr[n][deg][1] for n in nrs]
        return round(statistics.median(vals), 2) if vals else None
    p5_vals = [by_nr[n]['1'][1] for n in padla]
    data = {
        'p3p': med('3', padla), 'p3b': med('3', bez),
        'p4p': med('2', padla), 'p4b': med('2', bez),
        'w': round(len(padla) / len(last_nrs), 3),
        'p5': round(statistics.median(p5_vals), 2) if p5_vals else None,
        'n': len(last_nrs), 'stanNa': by_nr[last_nrs[-1]]['date'],
    }
    return _embed_const('WYPLATY_MINI_JSON', WYPLATY_MINI_RE, data)


def embed_wyplaty_ej():
    """Stopnie V-XII EuroJackpot z WYPLATY_MEDIAN_N ostatnich losowań:
    a{t} = średnia wypłata na zwycięzcę (suma wypłat / suma zwycięzców — właściwa
    statystyka do EV), m{t} = mediana (wartość „typowa" do pokazania użytkownikowi).
    Stopnie I-IV pomijane — za mało trafień w oknie / ekstremalna wariancja.
    -> const WYPLATY_EJ_JSON w index.html (stała kolejność kluczy)."""
    by_nr = {}
    with open(WYPLATY_EJ_PATH, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            nr, date, deg, cnt, kwota = line.split(',')
            by_nr.setdefault(int(nr), {'date': date})[deg] = (int(cnt), float(kwota))
    last_nrs = sorted(by_nr)[-WYPLATY_MEDIAN_N:]
    data = {}
    for deg in EJ_EMBED_DEGS:
        cnt_sum = sum(by_nr[n][str(deg)][0] for n in last_nrs)
        paid_sum = sum(by_nr[n][str(deg)][0] * by_nr[n][str(deg)][1] for n in last_nrs)
        med_vals = [by_nr[n][str(deg)][1] for n in last_nrs if by_nr[n][str(deg)][0] > 0]
        data[f'a{deg}'] = round(paid_sum / cnt_sum, 2) if cnt_sum else None
        data[f'm{deg}'] = round(statistics.median(med_vals), 2) if med_vals else None
    data['n'] = len(last_nrs)
    data['stanNa'] = by_nr[last_nrs[-1]]['date']
    return _embed_const('WYPLATY_EJ_JSON', WYPLATY_EJ_RE, data)


def fetch_ej_last_api_id(api_key):
    """Ostatni drawSystemId EuroJackpot w API (osobna numeracja niż baza).
    Soft-fail: None przy błędzie — wypłaty EJ dokończymy przy następnym runie."""
    items = api_get('lotteries/draw-results/last-results-per-game',
                    {'gameType': 'EuroJackpot'}, api_key, fatal=False)
    if not items:
        return None
    sid = items[0].get('drawSystemId')
    return sid if isinstance(sid, int) else None


def embed_sprzedaz():
    """Estymowana sprzedaż zakładów Lotto ze SPRZEDAZ_WINDOW ostatnich losowań
    (wyplaty_lotto.csv): mediana ogólna + mediany kubełkowe wg głębokości
    kumulacji (seria losowań bez trafienia 6/6: b0 = 0-1, b2 = 2-4, b5 = 5+).
    -> const SPRZEDAZ_JSON w index.html (stała kolejność kluczy)."""
    by_nr = {}
    with open(WYPLATY_PATH, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            nr, date, hits, cnt, _kwota = line.split(',')
            by_nr.setdefault(int(nr), {'date': date})[int(hits)] = int(cnt)
    last_nrs = sorted(by_nr)[-SPRZEDAZ_WINDOW:]
    sales_all = []
    buckets = {'b0': [], 'b2': [], 'b5': []}
    streak = 0
    for nr in last_nrs:
        w = by_nr[nr]
        if not all(h in w for h in (3, 4, 5, 6)):
            continue
        sales = (w[3] + w[4] + w[5]) / LOTTO_P345
        sales_all.append(sales)
        buckets['b0' if streak <= 1 else ('b2' if streak <= 4 else 'b5')].append(sales)
        streak = 0 if w[6] > 0 else streak + 1
    if not sales_all:
        fail('sprzedaż: brak kompletnych losowań w oknie — sprawdź wyplaty_lotto.csv')
    def med(vals):
        return round(statistics.median(vals)) if vals else None
    data = {'m': med(sales_all),
            'b0': med(buckets['b0']), 'b2': med(buckets['b2']), 'b5': med(buckets['b5']),
            'n': len(sales_all), 'stanNa': by_nr[last_nrs[-1]]['date']}
    return _embed_const('SPRZEDAZ_JSON', SPRZEDAZ_RE, data)


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
    wyplaty_changed = soft('wypłaty Lotto (CSV)', update_wyplaty, api_key)
    wyplaty_embedded = (soft('mediany wypłat Lotto (embed)', embed_wyplaty)
                        if os.path.exists(WYPLATY_PATH) else False)
    if wyplaty_embedded:
        log('index.html: wbudowano zaktualizowane mediany wypłat (WYPLATY_JSON)')

    # Wypłaty Mini Lotto i EuroJackpot (v4.13.0, soft-fail jak wyżej)
    mini_changed = soft('wypłaty MiniLotto (CSV)', update_wyplaty_game,
                        WYPLATY_MINI_PATH, 'MiniLotto', MINI_DEG_KEYS,
                        last_nr['mini_lotto'], WYPLATY_BACKFILL, api_key)
    mini_embedded = (soft('mediany wypłat Mini Lotto (embed)', embed_wyplaty_mini)
                     if os.path.exists(WYPLATY_MINI_PATH) else False)
    if mini_embedded:
        log('index.html: wbudowano mediany warunkowe wypłat Mini Lotto (WYPLATY_MINI_JSON)')
    ej_last_api = fetch_ej_last_api_id(api_key)
    ej_changed = False
    ej_embedded = False
    if ej_last_api:
        ej_changed = soft('wypłaty EuroJackpot (CSV)', update_wyplaty_game,
                          WYPLATY_EJ_PATH, 'EuroJackpot', EJ_DEG_KEYS,
                          ej_last_api, WYPLATY_EJ_BACKFILL, api_key)
        ej_embedded = (soft('średnie/mediany wypłat EuroJackpot (embed)', embed_wyplaty_ej)
                       if os.path.exists(WYPLATY_EJ_PATH) else False)
        if ej_embedded:
            log('index.html: wbudowano średnie/mediany wypłat EuroJackpot (WYPLATY_EJ_JSON)')
    else:
        log('  wypłaty EuroJackpot: nie udało się pobrać ostatniego ID — pomijam')

    # Sprzedaż zakładów Lotto (v4.14.0) — czysta funkcja wyplaty_lotto.csv
    sprzedaz_embedded = (soft('estymacje sprzedaży (embed)', embed_sprzedaz)
                         if os.path.exists(WYPLATY_PATH) else False)
    if sprzedaz_embedded:
        log('index.html: wbudowano estymacje sprzedaży (SPRZEDAZ_JSON)')

    # Przebudowa bloba z CSV (zawsze z aktualnych plików)
    blob_changed = rebuild_blob() if total_added > 0 else False

    if (total_added == 0 and not jackpots_changed and not jk_embedded
            and not wyplaty_changed and not wyplaty_embedded
            and not mini_changed and not mini_embedded
            and not ej_changed and not ej_embedded
            and not sprzedaz_embedded):
        log('Brak nowych danych — repo bez zmian.')
        write_github_output('false', '')
        report_optional()
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
            summary = 'data: wypłaty (mediany/średnie z 30 losowań)'
        log(summary)
    write_github_output('true', summary)
    report_optional()


def report_optional():
    """Raportuje porażki sekcji opcjonalnych do logu i GITHUB_OUTPUT
    (optfail=true + optfail_sections=... -> krok workflow zakłada issue).
    Nie zmienia kodu wyjścia — dane główne są ważniejsze niż statystyki."""
    for note in optional_notes:
        log(f'UWAGA: {note}')
    if optional_failures:
        sections = ', '.join(optional_failures)
        log(f'::warning::sekcje opcjonalne z błędami: {sections}')
        write_github_output_kv('optfail', 'true')
        write_github_output_kv('optfail_sections', sections)
    if optional_notes:
        write_github_output_kv('optnotes', ' | '.join(optional_notes))


def write_github_output_kv(key, value):
    out = os.environ.get('GITHUB_OUTPUT')
    if not out:
        return
    with open(out, 'a', encoding='utf-8') as f:
        f.write(f'{key}={value}\n')


def write_github_output(changed, summary):
    out = os.environ.get('GITHUB_OUTPUT')
    if not out:
        return
    with open(out, 'a', encoding='utf-8') as f:
        f.write(f'changed={changed}\n')
        f.write(f'summary={summary}\n')


if __name__ == '__main__':
    main()
