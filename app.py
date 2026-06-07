import re
from datetime import datetime, timedelta, timezone
from math import comb

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title='Gold League Tracker - Lottomatica', layout='wide')

st.title('⚽ Gold League Tracker')
st.caption(
    'Archivio risultati Lottomatica Gold League (canale 574) · '
    'cicli distinti · forecast su blocchi da 6 · ranking GG/NG · reset giornaliero dopo l\'1:00'
)

# -------------------------
# Configurazione
# -------------------------
LOCAL_TZ_OFFSET_HOURS = 1
MATCHES_PER_BLOCK = 6   # Gold League ha 6 partite per giornata
MAX_GIORNATA = 22         # Campionato virtuale tipicamente 34 giornate
REQUEST_TIMEOUT = 30

# Canale 574 = Gold League Lottomatica
# L'endpoint è lo stesso backend Sisal/Better, solo il canale cambia
# URL pattern: /getArchivioGareCampionato/{tipo}/{sottoTipo}/{canale}/{data}
# Sisal FAS League usa: 1/3/6 → Gold League usa probabilmente 1/3/574
# ⚠️ NOTA: Se l'endpoint non risponde, vedere la sezione "Configurazione API" sotto

API_BASE_URL = "https://www.lottomatica.it/api/sport/virtual/getArchivioGareCampionato"
API_TIPO = "1"
API_SOTTO_TIPO = "3"
API_CANALE = "574"
API_REFERER = "https://www.lottomatica.it/"

# Mappa nomi squadre Gold League (placeholder - aggiornare con le squadre reali)
TEAM_NAME_MAP = {
    'BAR': 'BAR', 'REA': 'REA', 'ATM': 'ATM', 'SEV': 'SEV', 'VAL': 'VAL',
    'VIL': 'VIL', 'BET': 'BET', 'CEL': 'CEL', 'OSA': 'OSA', 'GRA': 'GRA',
    'MAL': 'MAL', 'ESP': 'ESP', 'GET': 'GET', 'ALA': 'ALA', 'LEV': 'LEV',
    'ATH': 'ATH', 'CAD': 'CAD', 'ELC': 'ELC', 'RAY': 'RAY', 'HUE': 'HUE',
}


# -------------------------
# Utility tempo / reset
# -------------------------
def local_now() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=LOCAL_TZ_OFFSET_HOURS)


def get_operational_datetime() -> datetime:
    now_utc = datetime.now(timezone.utc)
    local = now_utc + timedelta(hours=LOCAL_TZ_OFFSET_HOURS)
    if local.hour < 1:
        return local - timedelta(days=1)
    return local


def maybe_reset_daily_after_one() -> bool:
    now_utc = datetime.now(timezone.utc)
    local = now_utc + timedelta(hours=LOCAL_TZ_OFFSET_HOURS)
    today = local.date().isoformat()
    current_hour = local.hour
    active_data_day = st.session_state.get('active_data_day')

    if active_data_day is None:
        st.session_state['active_data_day'] = get_operational_datetime().date().isoformat()
        return False

    if active_data_day != today and current_hour >= 1:
        st.session_state['matches'] = []
        st.session_state['last_update'] = '-'
        st.session_state['active_data_day'] = today
        st.session_state['reset_notice'] = (
            f"Reset giornaliero eseguito automaticamente alle {local.strftime('%H:%M:%S')}."
        )
        return True

    return False


# -------------------------
# Parsing risultati API
# -------------------------
def infer_gol_gol(result_list):
    for rr in result_list:
        market = str(rr.get('descrizioneScommessa') or rr.get('modelloScommessa') or '').lower()
        result = str(rr.get('risultato') or rr.get('descrizioneEsito') or '').lower()
        if 'goal/no goal' in market or 'gol/no gol' in market or 'gg/ng' in market:
            if '+ goal' in result or result.strip() in ['goal', 'gol', 'gg', 'goal/goal']:
                return 'GOL'
            if ('+ no goal' in result or '+ no gol' in result
                    or result.strip() in ['no goal', 'no gol', 'nogol', 'ng', 'no goal/no goal']):
                return 'NO GOL'
    return 'N/D'


def get_event_description(ev):
    candidates = [
        ev.get('descrizioneAvventimento'), ev.get('descrizioneAvvenimento'),
        ev.get('descrizioneEvento'), ev.get('evento'), ev.get('match'),
        ev.get('avvenimento'), ev.get('nomeEvento'), ev.get('labelEvento')
    ]
    for value in candidates:
        value = str(value or '').strip()
        if value:
            return value.replace(' ', '')
    return ''


def normalize_match_name(name):
    name = str(name or '').upper().strip()
    name = name.replace('-', ' ').replace('_', ' ')
    name = ' '.join(name.split())
    return name


def split_teams(match_name):
    cleaned = normalize_match_name(match_name)
    parts = cleaned.split(' ')
    if len(parts) >= 2:
        home = TEAM_NAME_MAP.get(parts[0], parts[0])
        away = TEAM_NAME_MAP.get(parts[1], parts[1])
        return home, away
    return cleaned, ''


def parse_datetime_fields(date_str, data_ora):
    raw = str(data_ora or '').strip()
    combined = f'{date_str} {raw}'.strip()
    dt = pd.to_datetime(combined, dayfirst=True, errors='coerce')
    if pd.isna(dt):
        return pd.NaT, raw[:5] if raw else ''
    return dt, dt.strftime('%H:%M')


def normalize_giornata_value(giornata):
    try:
        g = int(str(giornata).strip())
        if 1 <= g <= MAX_GIORNATA:
            return g
    except Exception:
        pass
    return None


# -------------------------
# Fetch API Lottomatica
# -------------------------
def fetch_matches():
    operational_dt = get_operational_datetime()
    date_str = operational_dt.strftime('%d-%m-%Y')
    api_url = f'{API_BASE_URL}/{API_TIPO}/{API_SOTTO_TIPO}/{API_CANALE}/{date_str}'

    response = requests.get(
        api_url,
        timeout=REQUEST_TIMEOUT,
        headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Referer': API_REFERER,
            'Origin': 'https://www.lottomatica.it',
        }
    )
    response.raise_for_status()
    data = response.json()
    matches = []

    if not isinstance(data, list):
        return matches, date_str

    for giornata_block in data:
        giornata_api = normalize_giornata_value(giornata_block.get('giornata'))
        risultato_map = giornata_block.get('risultatoModelloScommessaCampionatoMap', {})
        if not isinstance(risultato_map, dict):
            continue

        for _, model_list in risultato_map.items():
            if not isinstance(model_list, list):
                continue
            for model in model_list:
                eventi = model.get('eventiScommessaList', [])
                if not isinstance(eventi, list):
                    continue
                for ev in eventi:
                    desc = get_event_description(ev)
                    home_team, away_team = split_teams(desc)
                    data_ora = str(ev.get('dataOra') or '').strip()
                    codice_palinsesto = str(ev.get('codicePalinsesto') or '').strip()
                    codice_avvenimento = str(ev.get('codiceAvvenimento') or '').strip()
                    result_list = ev.get('risultatoScommessaUfficialeList', [])
                    if not isinstance(result_list, list):
                        result_list = []
                    esito = infer_gol_gol(result_list)
                    dt_value, orario = parse_datetime_fields(date_str, data_ora)

                    matches.append({
                        'match_id': f'{date_str}-{codice_palinsesto}-{codice_avvenimento}',
                        'api_day': date_str,
                        'timestamp': dt_value,
                        'timestamp_str': f'{date_str} {data_ora}',
                        'orario': orario,
                        'giornata': giornata_api,
                        'codice_palinsesto': codice_palinsesto,
                        'codice_avvenimento': codice_avvenimento,
                        'descrizione_avventimento': desc,
                        'home_team': home_team,
                        'away_team': away_team,
                        'esito': esito,
                    })

    # Deduplicazione
    dedup = {}
    for m in matches:
        current = dedup.get(m['match_id'])
        if current is None:
            dedup[m['match_id']] = m
        else:
            current_score = (pd.notna(current['timestamp']), current['esito'] != 'N/D')
            new_score = (pd.notna(m['timestamp']), m['esito'] != 'N/D')
            if new_score >= current_score:
                dedup[m['match_id']] = m

    return list(dedup.values()), date_str


# -------------------------
# Normalizzazione dataset
# -------------------------
def empty_prepared_df():
    return pd.DataFrame(columns=[
        'match_id', 'api_day', 'timestamp', 'timestamp_str', 'orario', 'giornata',
        'codice_palinsesto', 'codice_avvenimento', 'descrizione_avventimento',
        'home_team', 'away_team', 'esito', 'sort_timestamp', 'cycle_id',
        'group_key', 'group_label', 'match_nel_blocco'
    ])


def prepare_matches_df(matches):
    if not matches:
        return empty_prepared_df()

    df = pd.DataFrame(matches).copy()
    required = [
        'match_id', 'api_day', 'timestamp', 'timestamp_str', 'orario', 'giornata',
        'codice_palinsesto', 'codice_avvenimento', 'descrizione_avventimento',
        'home_team', 'away_team', 'esito'
    ]
    for col in required:
        if col not in df.columns:
            df[col] = None

    df['timestamp'] = pd.to_datetime(df['timestamp'], errors='coerce')
    df['timestamp_str'] = df['timestamp_str'].fillna('').astype(str)
    fallback_time = pd.to_datetime(df['timestamp_str'], dayfirst=True, errors='coerce')
    df['sort_timestamp'] = df['timestamp'].fillna(fallback_time)
    df['orario'] = df['orario'].fillna('').astype(str)
    df['codice_avvenimento'] = df['codice_avvenimento'].fillna('').astype(str)
    df['descrizione_avventimento'] = df['descrizione_avventimento'].fillna('').astype(str)
    df['giornata'] = df['giornata'].apply(normalize_giornata_value)
    df = df.dropna(subset=['giornata']).copy()

    if df.empty:
        return empty_prepared_df()

    df['giornata'] = df['giornata'].astype(int)
    df = df.sort_values(
        ['sort_timestamp', 'giornata', 'orario', 'codice_avvenimento', 'match_id'],
        ascending=[True, True, True, True, True],
        kind='stable'
    ).reset_index(drop=True)

    cycle_ids = []
    current_cycle = 1
    prev_giornata = None
    for g in df['giornata'].tolist():
        if prev_giornata is not None and g < prev_giornata:
            current_cycle += 1
        cycle_ids.append(current_cycle)
        prev_giornata = g
    df['cycle_id'] = cycle_ids
    df['group_key'] = df['cycle_id'].astype(str) + '-' + df['giornata'].astype(str)
    df['group_label'] = df.apply(lambda r: f"Ciclo {int(r['cycle_id'])} · Giornata {int(r['giornata'])}", axis=1)
    df['match_nel_blocco'] = df.groupby('group_key', sort=False).cumcount() + 1

    return df.reset_index(drop=True)


def ensure_block_columns(df):
    required = ['group_key', 'cycle_id', 'giornata', 'group_label', 'sort_timestamp']
    if df is None or df.empty:
        return empty_prepared_df()

    work = df.copy()
    for col in required:
        if col not in work.columns:
            work[col] = pd.NA

    if 'timestamp' in work.columns:
        work['timestamp'] = pd.to_datetime(work['timestamp'], errors='coerce')
    else:
        work['timestamp'] = pd.NaT

    if 'timestamp_str' not in work.columns:
        work['timestamp_str'] = ''
    work['timestamp_str'] = work['timestamp_str'].fillna('').astype(str)

    if work['sort_timestamp'].isna().all():
        fallback = pd.to_datetime(work['timestamp_str'], dayfirst=True, errors='coerce')
        work['sort_timestamp'] = work['timestamp'].fillna(fallback)

    if 'giornata' in work.columns:
        work['giornata'] = work['giornata'].apply(normalize_giornata_value)
    else:
        work['giornata'] = pd.NA

    work = work.dropna(subset=['giornata']).copy()
    if work.empty:
        return empty_prepared_df()

    work['giornata'] = work['giornata'].astype(int)

    needs_rebuild = work['group_key'].isna().any() or work['cycle_id'].isna().any() or work['group_label'].isna().any()
    if needs_rebuild:
        if 'match_id' not in work.columns:
            work['match_id'] = range(1, len(work) + 1)
        if 'orario' not in work.columns:
            work['orario'] = ''
        if 'codice_avvenimento' not in work.columns:
            work['codice_avvenimento'] = ''
        work = work.sort_values(
            ['sort_timestamp', 'giornata', 'orario', 'codice_avvenimento', 'match_id'],
            ascending=[True, True, True, True, True],
            kind='stable'
        ).reset_index(drop=True)
        cycle_ids = []
        current_cycle = 1
        prev_giornata = None
        for g in work['giornata'].tolist():
            if prev_giornata is not None and g < prev_giornata:
                current_cycle += 1
            cycle_ids.append(current_cycle)
            prev_giornata = g
        work['cycle_id'] = cycle_ids
        work['group_key'] = work['cycle_id'].astype(str) + '-' + work['giornata'].astype(str)
        work['group_label'] = work.apply(lambda r: f"Ciclo {int(r['cycle_id'])} · Giornata {int(r['giornata'])}", axis=1)

    if 'match_nel_blocco' not in work.columns:
        work['match_nel_blocco'] = work.groupby('group_key', sort=False).cumcount() + 1

    return work.reset_index(drop=True)


# -------------------------
# Blocchi reali
# -------------------------
def get_valid_matches_df(df):
    if df is None or df.empty:
        return empty_prepared_df()
    if 'esito' not in df.columns:
        return empty_prepared_df()
    return df[df['esito'].isin(['GOL', 'NO GOL'])].copy()


def build_blocks(df):
    valid_df = ensure_block_columns(get_valid_matches_df(df))
    cols = [
        'cycle_id', 'giornata', 'partite', 'GG', 'NO_GOL', '% sul totale',
        'orario_inizio', 'orario_fine', 'group_label', 'completa'
    ]
    if valid_df.empty:
        return pd.DataFrame(columns=cols)

    needed = ['group_key', 'cycle_id', 'giornata', 'group_label', 'sort_timestamp', 'orario', 'esito']
    if any(c not in valid_df.columns for c in needed):
        return pd.DataFrame(columns=cols)

    grouped = valid_df.groupby(['group_key', 'cycle_id', 'giornata', 'group_label'], dropna=False).agg(
        partite=('esito', 'count'),
        GG=('esito', lambda x: int((x == 'GOL').sum())),
        NO_GOL=('esito', lambda x: int((x == 'NO GOL').sum())),
        orario_inizio=('orario', 'first'),
        orario_fine=('orario', 'last'),
        last_ts=('sort_timestamp', 'max')
    ).reset_index()

    grouped['% sul totale'] = ((grouped['GG'] / grouped['partite']) * 100).round(2)
    grouped['completa'] = grouped['partite'] >= MATCHES_PER_BLOCK
    grouped = grouped.sort_values(['last_ts', 'cycle_id', 'giornata'], ascending=[False, False, False], kind='stable')
    return grouped[cols].reset_index(drop=True)


def build_trend_metrics(df):
    blocks = build_blocks(df)
    if blocks.empty:
        return {'last5': 0, 'prev5': 0, 'last10': 0, 'prev10': 0, 'latest_block_pct': 0.0}
    return {
        'last5': int(blocks.head(5)['GG'].sum()),
        'prev5': int(blocks.iloc[5:10]['GG'].sum()) if len(blocks) > 5 else 0,
        'last10': int(blocks.head(10)['GG'].sum()),
        'prev10': int(blocks.iloc[10:20]['GG'].sum()) if len(blocks) > 10 else 0,
        'latest_block_pct': float(blocks.iloc[0]['% sul totale']) if not blocks.empty else 0.0,
    }


def build_all_gg_stats(df):
    blocks = build_blocks(df)
    if blocks.empty:
        return {
            'total_all_gg_blocks': 0,
            'latest_streak': 0,
            'blocks_table': pd.DataFrame(columns=['group_label', 'GG', 'partite', 'all_gg_6su6'])
        }
    blocks = blocks.copy()
    blocks['all_gg_6su6'] = (blocks['partite'] == MATCHES_PER_BLOCK) & (blocks['GG'] == MATCHES_PER_BLOCK)
    streak = 0
    for value in blocks['all_gg_6su6'].tolist():
        if value:
            streak += 1
        else:
            break
    return {
        'total_all_gg_blocks': int(blocks['all_gg_6su6'].sum()),
        'latest_streak': streak,
        'blocks_table': blocks[['group_label', 'GG', 'partite', 'all_gg_6su6']]
    }


def build_forecast(df):
    blocks = build_blocks(df)
    if blocks.empty:
        return {
            'rate_5': 0.0, 'rate_10': 0.0, 'rate_20': 0.0, 'weighted_rate': 0.0,
            'next_block_expected': 0.0, 'next_block_rounded': 0, 'next_3_blocks_expected': 0.0,
            'range_min': 0, 'range_max': 0,
            'details': pd.DataFrame(columns=['finestra', 'percentuale_GG'])
        }
    blocks = blocks.copy()
    blocks['rate'] = blocks['GG'] / blocks['partite']

    def mean_rate(n):
        subset = blocks.head(n)
        return float(subset['rate'].mean()) if not subset.empty else 0.0

    rate_5 = mean_rate(5)
    rate_10 = mean_rate(10)
    rate_20 = mean_rate(20)
    weighted_rate = max(0.0, min(1.0, (0.5 * rate_5) + (0.3 * rate_10) + (0.2 * rate_20)))
    next_block_expected = round(weighted_rate * MATCHES_PER_BLOCK, 2)
    next_block_rounded = int(round(next_block_expected))
    next_3_blocks_expected = round(next_block_expected * 3, 2)
    range_min = max(0, int(next_block_expected // 1))
    range_max = min(MATCHES_PER_BLOCK, int(next_block_expected // 1) + 1)
    details = pd.DataFrame([
        {'finestra': 'Ultimi 5 blocchi', 'percentuale_GG': round(rate_5 * 100, 2)},
        {'finestra': 'Ultimi 10 blocchi', 'percentuale_GG': round(rate_10 * 100, 2)},
        {'finestra': 'Ultimi 20 blocchi', 'percentuale_GG': round(rate_20 * 100, 2)},
        {'finestra': 'Media pesata finale', 'percentuale_GG': round(weighted_rate * 100, 2)},
    ])
    return {
        'rate_5': rate_5, 'rate_10': rate_10, 'rate_20': rate_20,
        'weighted_rate': weighted_rate,
        'next_block_expected': next_block_expected,
        'next_block_rounded': next_block_rounded,
        'next_3_blocks_expected': next_3_blocks_expected,
        'range_min': range_min, 'range_max': range_max,
        'details': details
    }


def build_backtest(df):
    valid_df = ensure_block_columns(get_valid_matches_df(df))
    cols = ['group_label', 'actual_GG', 'predicted_GG', 'error']
    empty = pd.DataFrame(columns=cols)
    if valid_df.empty:
        return {'mae_10': 0.0, 'mae_20': 0.0, 'bias': 0.0, 'table': empty}

    grouped = valid_df.groupby(['group_key', 'cycle_id', 'giornata', 'group_label'], dropna=False).agg(
        GG=('esito', lambda x: int((x == 'GOL').sum())),
        totale=('esito', 'count'),
        last_ts=('sort_timestamp', 'max')
    ).reset_index()
    grouped = grouped.sort_values(['last_ts', 'cycle_id', 'giornata'], ascending=[True, True, True], kind='stable').reset_index(drop=True)
    if grouped.empty or len(grouped) < 2:
        return {'mae_10': 0.0, 'mae_20': 0.0, 'bias': 0.0, 'table': empty}
    grouped['rate'] = grouped['GG'] / grouped['totale']

    preds = []
    for i in range(1, len(grouped)):
        hist = grouped.iloc[:i].copy()

        def mean_rate(n, h=hist):
            sub = h.tail(n)
            return float(sub['rate'].mean()) if not sub.empty else 0.0

        r5 = mean_rate(5)
        r10 = mean_rate(10)
        r20 = mean_rate(20)
        wr = max(0.0, min(1.0, (0.5 * r5) + (0.3 * r10) + (0.2 * r20)))
        predicted = round(wr * MATCHES_PER_BLOCK, 2)
        actual = int(grouped.iloc[i]['GG'])
        preds.append({
            'group_label': grouped.iloc[i]['group_label'],
            'actual_GG': actual,
            'predicted_GG': predicted,
            'error': round(predicted - actual, 2),
            'abs_error': round(abs(predicted - actual), 2),
        })

    if not preds:
        return {'mae_10': 0.0, 'mae_20': 0.0, 'bias': 0.0, 'table': empty}
    backtest_df = pd.DataFrame(preds).iloc[::-1].reset_index(drop=True)
    return {
        'mae_10': round(float(backtest_df.head(10)['abs_error'].mean()), 2),
        'mae_20': round(float(backtest_df.head(20)['abs_error'].mean()), 2),
        'bias': round(float(backtest_df['error'].mean()), 2),
        'table': backtest_df[['group_label', 'actual_GG', 'predicted_GG', 'error']]
    }


def build_probabilities(df):
    forecast = build_forecast(df)
    p = forecast['weighted_rate']

    def binom_prob_at_least(k, n=MATCHES_PER_BLOCK, prob=0.0):
        return sum(comb(n, i) * (prob ** i) * ((1 - prob) ** (n - i)) for i in range(k, n + 1))

    p_ge4 = binom_prob_at_least(4, MATCHES_PER_BLOCK, p)
    p_ge5 = binom_prob_at_least(5, MATCHES_PER_BLOCK, p)
    p_eq6 = p ** MATCHES_PER_BLOCK

    alert_level = 'low'
    if forecast['next_block_expected'] >= 4.5 or p_eq6 >= 0.12:
        alert_level = 'high'
    elif forecast['next_block_expected'] >= 3.8 or p_ge4 >= 0.55:
        alert_level = 'medium'

    if alert_level == 'high':
        msg = f"Alert alto: forecast {forecast['next_block_expected']} GG su 6, P(4+)= {p_ge4:.1%}, P(6)= {p_eq6:.1%}."
    elif alert_level == 'medium':
        msg = f"Alert medio: forecast {forecast['next_block_expected']} GG su 6, P(4+)= {p_ge4:.1%}."
    else:
        msg = f"Scenario standard: forecast {forecast['next_block_expected']} GG su 6, P(4+)= {p_ge4:.1%}."

    return {
        'p_ge4': p_ge4,
        'p_ge5': p_ge5,
        'p_eq6': p_eq6,
        'alert_level': alert_level,
        'alert_message': msg,
    }


def build_giornata_summary(df):
    blocks = build_blocks(df)
    if blocks.empty:
        return pd.DataFrame(columns=['group_label', 'partite', 'GG', 'NO_GOL', 'pct_gg'])
    out = blocks[['group_label', 'partite', 'GG', 'NO_GOL']].copy()
    out['pct_gg'] = ((out['GG'] / out['partite']) * 100).round(2)
    return out.reset_index(drop=True)


def build_trend_visual_df(df):
    blocks = build_blocks(df)
    if blocks.empty:
        return pd.DataFrame(columns=[
            'group_label', 'GG', '% sul totale', 'gg_ma_3', 'gg_ma_5',
            'pct_ma_3', 'pct_ma_5', 'state_color', 'state_label', 'cycle_id', 'giornata'
        ])

    chart_df = blocks.copy().sort_values(['cycle_id', 'giornata'], ascending=[True, True], kind='stable').reset_index(drop=True)
    chart_df['gg_ma_3'] = chart_df['GG'].rolling(3, min_periods=1).mean().round(2)
    chart_df['gg_ma_5'] = chart_df['GG'].rolling(5, min_periods=1).mean().round(2)
    chart_df['pct_ma_3'] = chart_df['% sul totale'].rolling(3, min_periods=1).mean().round(2)
    chart_df['pct_ma_5'] = chart_df['% sul totale'].rolling(5, min_periods=1).mean().round(2)

    def classify(v):
        if v >= 4:
            return 'Alta spinta', '#16a34a'
        if v == 3:
            return 'Neutro', '#eab308'
        return 'Freddo', '#dc2626'

    states = chart_df['GG'].apply(classify)
    chart_df['state_label'] = states.apply(lambda x: x[0])
    chart_df['state_color'] = states.apply(lambda x: x[1])
    return chart_df


def build_trend_status(df):
    chart_df = build_trend_visual_df(df)
    if chart_df.empty:
        return {
            'label': 'Nessun dato', 'delta_short_vs_long': 0.0, 'momentum': 0, 'stress': 0,
            'last_gg': 0, 'last_pct': 0.0, 'last_vs_ma5': 0.0
        }

    short_ma = float(chart_df['gg_ma_3'].iloc[-1])
    long_ma = float(chart_df['gg_ma_5'].iloc[-1])
    delta = round(short_ma - long_ma, 2)

    if delta >= 0.5:
        label = 'Trend in accelerazione'
    elif delta <= -0.5:
        label = 'Trend in frenata'
    else:
        label = 'Trend stabile'

    momentum = 0
    for v in chart_df['GG'].iloc[::-1].tolist():
        if v >= 4:
            momentum += 1
        else:
            break

    stress = 0
    for v in chart_df['GG'].iloc[::-1].tolist():
        if v <= 2:
            stress += 1
        else:
            break

    last_gg = int(chart_df['GG'].iloc[-1])
    last_pct = float(chart_df['% sul totale'].iloc[-1])
    last_vs_ma5 = round(last_gg - long_ma, 2)

    return {
        'label': label,
        'delta_short_vs_long': delta,
        'momentum': momentum,
        'stress': stress,
        'last_gg': last_gg,
        'last_pct': last_pct,
        'last_vs_ma5': last_vs_ma5
    }


def build_forecast_compare_df(df):
    forecast = build_forecast(df)
    trend_df = build_trend_visual_df(df)
    ma3 = float(trend_df['gg_ma_3'].iloc[-1]) if not trend_df.empty else 0.0
    ma5 = float(trend_df['gg_ma_5'].iloc[-1]) if not trend_df.empty else 0.0
    recent10 = float(trend_df.tail(10)['GG'].mean()) if not trend_df.empty else 0.0
    return pd.DataFrame([
        {'metrica': 'Ultimo blocco', 'valore': float(trend_df['GG'].iloc[-1]) if not trend_df.empty else 0.0},
        {'metrica': 'Media mobile 3', 'valore': round(ma3, 2)},
        {'metrica': 'Media mobile 5', 'valore': round(ma5, 2)},
        {'metrica': 'Media ultimi 10', 'valore': round(recent10, 2)},
        {'metrica': 'Forecast prossimo', 'valore': float(forecast['next_block_expected'])},
    ])


def build_heatmap_pivot(df):
    blocks = build_blocks(df)
    if blocks.empty:
        return pd.DataFrame()
    pivot = blocks.pivot(index='cycle_id', columns='giornata', values='GG').sort_index().sort_index(axis=1)
    return pivot


# -------------------------
# Predict Top-N
# -------------------------
def team_recent_form(df, team_code, max_matchdays=10):
    valid_df = ensure_block_columns(get_valid_matches_df(df))
    if valid_df.empty:
        return pd.DataFrame(columns=['group_label', 'esito'])
    subset = valid_df[(valid_df['home_team'] == team_code) | (valid_df['away_team'] == team_code)].copy()
    if subset.empty:
        return pd.DataFrame(columns=['group_label', 'esito'])
    group_order = (
        subset.groupby('group_key', dropna=False)
        .agg(last_ts=('sort_timestamp', 'max'), group_label=('group_label', 'first'))
        .reset_index(drop=True)
        .sort_values('last_ts', ascending=False, kind='stable')
    )
    keep_labels = group_order.head(max_matchdays)['group_label'].tolist()
    subset = subset[subset['group_label'].isin(keep_labels)].copy()
    subset = subset.drop_duplicates(subset=['group_label', 'home_team', 'away_team', 'esito'])
    return subset[['group_label', 'esito']]


def rate_from_last_matchdays(team_df, n_days):
    if team_df.empty:
        return 0.0, 0
    ordered_days = []
    for g in team_df['group_label'].dropna().tolist():
        if g not in ordered_days:
            ordered_days.append(g)
    keep_days = ordered_days[:n_days]
    subset = team_df[team_df['group_label'].isin(keep_days)].copy()
    matches = len(subset)
    if matches == 0:
        return 0.0, 0
    gg_rate = float((subset['esito'] == 'GOL').sum() / matches)
    return gg_rate, matches


def get_team_trend_5_10(df, team_code):
    team_df = team_recent_form(df, team_code, max_matchdays=10)
    rate_5, matches_5 = rate_from_last_matchdays(team_df, 5)
    rate_10, matches_10 = rate_from_last_matchdays(team_df, 10)
    trend_score = (0.6 * rate_5) + (0.4 * rate_10)
    return {
        'team': team_code,
        'rate_5': rate_5, 'rate_10': rate_10,
        'trend_score': trend_score,
        'matches_5': matches_5, 'matches_10': matches_10,
    }


def get_global_trend_score(df):
    blocks = build_blocks(df)
    if blocks.empty:
        return {'rate_5': 0.0, 'rate_10': 0.0, 'trend_score': 0.0}
    blocks = blocks.copy()
    blocks['rate'] = blocks['GG'] / blocks['partite']

    def mean_rate(n):
        subset = blocks.head(n)
        return float(subset['rate'].mean()) if not subset.empty else 0.0

    rate_5 = mean_rate(5)
    rate_10 = mean_rate(10)
    trend_score = (0.6 * rate_5) + (0.4 * rate_10)
    return {'rate_5': rate_5, 'rate_10': rate_10, 'trend_score': trend_score}


def clean_decimal(val):
    try:
        val = str(val).replace(',', '.').strip()
        return float(val)
    except Exception:
        return None


def implied_probability_from_odds(odds):
    if odds is None or odds <= 1:
        return None
    return 1 / odds


def ranking_bonus(home_rank, away_rank):
    if home_rank is None or away_rank is None:
        return 0.0
    diff = abs(home_rank - away_rank)
    avg_rank = (home_rank + away_rank) / 2
    bonus = 0.0
    if diff <= 2:
        bonus += 0.03
    elif diff <= 5:
        bonus += 0.015
    elif diff >= 12:
        bonus -= 0.03
    if avg_rank <= 6:
        bonus += 0.01
    if avg_rank >= 14:
        bonus += 0.005
    return bonus


def parse_text_lines(raw_text):
    rows = []
    pattern = re.compile(r'^(.*?)\s+([0-9]+[\.,][0-9]+)\s+([0-9]{1,2})\s+([0-9]{1,2})$')
    for line in raw_text.splitlines():
        line = ' '.join(line.strip().split())
        if not line:
            continue
        m = pattern.match(line)
        if m:
            match_name = m.group(1)
            home_team, away_team = split_teams(match_name)
            rows.append({
                'match': normalize_match_name(match_name),
                'home_team': home_team,
                'away_team': away_team,
                'quota_gg': clean_decimal(m.group(2)),
                'rank_home': int(m.group(3)),
                'rank_away': int(m.group(4)),
            })
    return pd.DataFrame(rows)


def build_match_scores(input_df, history_df):
    if input_df.empty:
        return pd.DataFrame(columns=[
            'match', 'home_team', 'away_team', 'quota_gg', 'prob_mercato',
            'home_rate_5', 'home_rate_10', 'away_rate_5', 'away_rate_10',
            'team_trend_avg', 'global_trend', 'bonus_classifica', 'score_finale'
        ])

    global_trend = get_global_trend_score(history_df)
    rows = []
    for _, r in input_df.iterrows():
        home_trend = get_team_trend_5_10(history_df, r['home_team'])
        away_trend = get_team_trend_5_10(history_df, r['away_team'])
        prob_mercato = implied_probability_from_odds(r['quota_gg']) or 0.0
        team_trend_avg = (home_trend['trend_score'] + away_trend['trend_score']) / 2
        bonus_classifica = ranking_bonus(r['rank_home'], r['rank_away'])

        score_finale = (
            (0.50 * team_trend_avg) +
            (0.25 * global_trend['trend_score']) +
            (0.20 * prob_mercato) +
            (0.05 * bonus_classifica)
        )
        score_finale = max(0.0, min(0.95, score_finale))

        rows.append({
            'match': r['match'],
            'home_team': r['home_team'],
            'away_team': r['away_team'],
            'quota_gg': r['quota_gg'],
            'prob_mercato': prob_mercato,
            'home_rate_5': home_trend['rate_5'],
            'home_rate_10': home_trend['rate_10'],
            'away_rate_5': away_trend['rate_5'],
            'away_rate_10': away_trend['rate_10'],
            'team_trend_avg': team_trend_avg,
            'global_trend': global_trend['trend_score'],
            'bonus_classifica': bonus_classifica,
            'score_finale': score_finale,
            'home_matches_5': home_trend['matches_5'],
            'home_matches_10': home_trend['matches_10'],
            'away_matches_5': away_trend['matches_5'],
            'away_matches_10': away_trend['matches_10'],
        })

    return pd.DataFrame(rows).sort_values('score_finale', ascending=False).reset_index(drop=True)


def assign_topn_predictions(score_df):
    if score_df.empty:
        return score_df.copy(), 0.0, 0
    out = score_df.copy()
    expected_gg_total = float(out['score_finale'].sum())
    gg_slots = max(0, min(len(out), int(round(expected_gg_total))))
    out['prediction'] = 'NG'
    if gg_slots > 0:
        out.loc[:gg_slots - 1, 'prediction'] = 'GG'
    return out, round(expected_gg_total, 2), gg_slots


def build_manual_summary(pred_df, expected_gg_total, gg_slots):
    if pred_df.empty:
        return {
            'matches_count': 0,
            'expected_gg_total': 0.0,
            'expected_gg_rounded': 0,
            'predicted_gg_count': 0,
            'predicted_ng_count': 0,
        }
    predicted_gg_count = int((pred_df['prediction'] == 'GG').sum())
    predicted_ng_count = int((pred_df['prediction'] == 'NG').sum())
    return {
        'matches_count': int(len(pred_df)),
        'expected_gg_total': round(expected_gg_total, 2),
        'expected_gg_rounded': int(gg_slots),
        'predicted_gg_count': predicted_gg_count,
        'predicted_ng_count': predicted_ng_count,
    }


# ============================
# UI PRINCIPALE
# ============================

# Sidebar: configurazione API
with st.sidebar:
    st.header('⚙️ Configurazione API')
    st.caption(
        'Se l\'endpoint automatico non funziona, puoi personalizzarlo. '
        'Recupera l\'URL dalle DevTools del browser (F12 → Network) su lottomatica.it/scommesse/virtuali/canale/574/1'
    )
    custom_base = st.text_input('Base URL API', value=API_BASE_URL)
    custom_tipo = st.text_input('Tipo', value=API_TIPO)
    custom_sotto = st.text_input('SottoTipo', value=API_SOTTO_TIPO)
    custom_canale = st.text_input('Canale (Gold League)', value=API_CANALE)
    custom_referer = st.text_input('Referer', value=API_REFERER)

    if custom_base != API_BASE_URL:
        API_BASE_URL = custom_base
    if custom_tipo != API_TIPO:
        API_TIPO = custom_tipo
    if custom_sotto != API_SOTTO_TIPO:
        API_SOTTO_TIPO = custom_sotto
    if custom_canale != API_CANALE:
        API_CANALE = custom_canale
    if custom_referer != API_REFERER:
        API_REFERER = custom_referer

    st.divider()
    st.markdown(f'**URL costruito:**')
    st.code(f'{API_BASE_URL}/{API_TIPO}/{API_SOTTO_TIPO}/{API_CANALE}/{{data}}')

    st.divider()
    st.info(
        '**Gold League** ha 10 partite per giornata.\n\n'
        'I blocchi sono da 6 match per giornata.'
    )

# Session state init
if 'active_data_day' not in st.session_state:
    st.session_state['active_data_day'] = get_operational_datetime().date().isoformat()

api_day_used = st.session_state.get('api_day_used', '-')

col_btn, col_info = st.columns([1, 3])
with col_btn:
    if st.button('🔄 Aggiorna risultati', type='primary'):
        did_reset = maybe_reset_daily_after_one()
        try:
            matches_fetched, api_day = fetch_matches()
            st.session_state['matches'] = matches_fetched
            st.session_state['last_update'] = local_now().strftime('%d-%m-%Y %H:%M:%S')
            st.session_state['active_data_day'] = get_operational_datetime().date().isoformat()
            st.session_state['api_day_used'] = api_day
            if did_reset:
                st.success(st.session_state.get('reset_notice', 'Reset giornaliero eseguito.'))
            st.success(f'Partite trovate: {len(matches_fetched)}')
        except Exception as e:
            st.error(f'Errore API: {e}')
            st.warning(
                'Se l\'errore persiste, apri lottomatica.it/scommesse/virtuali/canale/574/1 '
                'e verifica l\'endpoint nelle DevTools (F12 → Network), poi aggiornalo nella sidebar.'
            )

with col_info:
    st.caption(
        f"Ultimo aggiornamento: {st.session_state.get('last_update', '-')} | "
        f"Giorno dati attivo: {st.session_state.get('active_data_day', '-')} | "
        f"Data API: {api_day_used} | Partite in sessione: {len(st.session_state.get('matches', []))}"
    )

matches = st.session_state.get('matches', [])
df = prepare_matches_df(matches)

if not df.empty:
    # ---- METRICHE PRINCIPALI ----
    st.subheader('📊 Metriche principali')
    trend = build_trend_metrics(df)
    forecast = build_forecast(df)
    trend_status = build_trend_status(df)

    m1, m2, m3, m4, m5, m6 = st.columns(6)
    m1.metric('GG ultimi 5 blocchi', trend['last5'], delta=trend['last5'] - trend['prev5'])
    m2.metric('GG ultimi 10 blocchi', trend['last10'], delta=trend['last10'] - trend['prev10'])
    m3.metric('Forecast prossima giornata', f"{forecast['next_block_expected']} GG")
    m4.metric('Range atteso', f"{forecast['range_min']}–{forecast['range_max']} GG")
    m5.metric('Trend corrente', trend_status['label'])
    m6.metric('Momentum (GG≥4 consecutivi)', trend_status['momentum'])

    # ---- STATO TREND ----
    st.subheader('📈 Analisi trend')
    ts1, ts2, ts3, ts4 = st.columns(4)
    ts1.metric('Ultimo blocco GG', trend_status['last_gg'], delta=round(trend_status['last_vs_ma5'], 2))
    ts2.metric('% GG ultimo blocco', f"{trend_status['last_pct']:.1f}%")
    ts3.metric('MA3 - MA5', f"{trend_status['delta_short_vs_long']:+.2f}")
    ts4.metric('Serie stress (GG≤3)', trend_status['stress'])

    # ---- GRAFICI TREND ----
    trend_df = build_trend_visual_df(df)
    if not trend_df.empty:
        cfa, cfb = st.columns(2)
        with cfa:
            st.markdown('#### GG per blocco + medie mobili')
            chart_data = trend_df.set_index('group_label')[['GG', 'gg_ma_3', 'gg_ma_5']]
            st.line_chart(chart_data, height=300, use_container_width=True)
        with cfb:
            st.markdown('#### % GG per blocco')
            chart_pct = trend_df.set_index('group_label')[['% sul totale', 'pct_ma_3', 'pct_ma_5']]
            st.line_chart(chart_pct, height=300, use_container_width=True)

    # ---- FORECAST DETTAGLIO ----
    st.subheader('🔮 Forecast dettaglio')
    fc1, fc2, fc3, fc4 = st.columns(4)
    fc1.metric('Rate 5 blocchi', f"{forecast['rate_5']:.1%}")
    fc2.metric('Rate 10 blocchi', f"{forecast['rate_10']:.1%}")
    fc3.metric('Rate 20 blocchi', f"{forecast['rate_20']:.1%}")
    fc4.metric('GG attesi prossimi 3 blocchi', forecast['next_3_blocks_expected'])

    with st.expander('Dettaglio forecast', expanded=False):
        st.dataframe(forecast['details'], use_container_width=True, hide_index=True)

    forecast_compare_df = build_forecast_compare_df(df)
    heatmap_df = build_heatmap_pivot(df)
    cfa2, cfb2 = st.columns(2)
    with cfa2:
        st.markdown('#### Confronto indicatori forecast')
        if not forecast_compare_df.empty:
            st.bar_chart(forecast_compare_df.set_index('metrica')[['valore']], height=320, use_container_width=True)
        else:
            st.info('Nessun dato disponibile.')
    with cfb2:
        st.markdown('#### Heatmap GG per ciclo/giornata')
        if not heatmap_df.empty:
            st.dataframe(heatmap_df, use_container_width=True)
            st.caption('Valori più alti = più GG in quella giornata del ciclo.')
        else:
            st.info('Nessun dato disponibile.')

    # ---- BACKTEST E PROBABILITÀ ----
    st.subheader('🧮 Backtest e probabilità')
    backtest = build_backtest(df)
    prob = build_probabilities(df)

    bt1, bt2, bt3 = st.columns(3)
    bt1.metric('MAE ultimi 10 blocchi', backtest['mae_10'])
    bt2.metric('MAE ultimi 20 blocchi', backtest['mae_20'])
    bt3.metric('Bias medio', backtest['bias'])

    pb1, pb2, pb3 = st.columns(3)
    pb1.metric('Probabilità 4+ GG', f"{prob['p_ge4']:.1%}")
    pb2.metric('Probabilità 5+ GG', f"{prob['p_ge5']:.1%}")
    pb3.metric('Probabilità 6 GG', f"{prob['p_eq6']:.1%}")

    if prob['alert_level'] == 'high':
        st.error(prob['alert_message'])
    elif prob['alert_level'] == 'medium':
        st.warning(prob['alert_message'])
    else:
        st.info(prob['alert_message'])

    with st.expander('Dettaglio backtest', expanded=False):
        st.dataframe(backtest['table'], use_container_width=True, hide_index=True)

    # ---- BLOCCHI 10/10 ----
    st.subheader('🏆 Blocchi con 6 GG su 6')
    all_gg_stats = build_all_gg_stats(df)
    col4, col5 = st.columns(2)
    col4.metric('Totale blocchi 6 su 6', all_gg_stats['total_all_gg_blocks'])
    col5.metric('Serie aperta 6 su 6', all_gg_stats['latest_streak'])

    with st.expander('Dettaglio blocchi 6 GG su 6', expanded=False):
        st.dataframe(all_gg_stats['blocks_table'], use_container_width=True, hide_index=True)

    # ---- STORICO GIORNATE ----
    with st.expander('📅 Partite giornata per giornata', expanded=False):
        storico_df = ensure_block_columns(df)[[
            'cycle_id', 'giornata', 'group_label', 'match_nel_blocco', 'orario',
            'codice_avvenimento', 'descrizione_avventimento', 'esito', 'group_key', 'sort_timestamp'
        ]].copy()

        block_order = (
            storico_df.groupby('group_key', dropna=False)
            .agg(last_ts=('sort_timestamp', 'max'), group_label=('group_label', 'first'))
            .reset_index(drop=True)
            .sort_values('last_ts', ascending=False, kind='stable')
        )
        ordered_labels = block_order['group_label'].tolist()

        if ordered_labels:
            for label in ordered_labels:
                blocco_g = storico_df[storico_df['group_label'] == label].copy()
                blocco_g = blocco_g.sort_values(
                    ['match_nel_blocco', 'orario', 'codice_avvenimento'],
                    ascending=[True, True, True], kind='stable'
                ).reset_index(drop=True)
                gg_count = int((blocco_g['esito'] == 'GOL').sum())
                ng_count = int((blocco_g['esito'] == 'NO GOL').sum())
                giornata_value = int(blocco_g['giornata'].iloc[0]) if not blocco_g.empty else 0
                ciclo_value = int(blocco_g['cycle_id'].iloc[0]) if not blocco_g.empty else 0
                with st.expander(
                    f'Giornata {giornata_value} · Ciclo {ciclo_value} · '
                    f'Partite {len(blocco_g)} · GG {gg_count} · NG {ng_count}',
                    expanded=False
                ):
                    st.dataframe(
                        blocco_g[[
                            'match_nel_blocco', 'orario', 'giornata',
                            'codice_avvenimento', 'descrizione_avventimento', 'esito'
                        ]].rename(columns={'match_nel_blocco': 'n_match'}),
                        use_container_width=True,
                        hide_index=True
                    )
        else:
            st.info('Nessun blocco disponibile.')

    st.subheader('📋 Blocchi Gold League distinti')
    st.dataframe(build_blocks(df), use_container_width=True, hide_index=True)

else:
    st.info(
        "Premi 'Aggiorna risultati' per caricare i dati storici del giorno. "
        "Prima dell'1:00 viene interrogato il giorno operativo precedente."
    )
    st.caption(
        f"Giorno dati attivo: {st.session_state.get('active_data_day')} | "
        f"Data API usata: {api_day_used}"
    )


# ============================
# UI PREDICT FINALE TOP-N
# ============================
st.divider()
st.subheader('🎯 Predict finale GG / NG con logica Top-N')
st.caption('Formato input: NomePartita quotaGG rankCasa rankTrasferta')
st.caption('Esempio: BAR REA 1.82 1 2  (Gold League ha 10 partite → inserisci tutte e 10)')

raw_text = st.text_area(
    'Inserimento manuale partite (10 righe per giornata completa)',
    height=280,
    placeholder=(
        'BAR REA 1.82 1 2\n'
        'ATM SEV 1.95 3 4\n'
        'VAL VIL 2.10 5 6\n'
        'BET CEL 1.90 7 8\n'
        'OSA GRA 2.20 9 10\n'
        'MAL ESP 2.05 11 12\n'
        'GET ALA 2.15 13 14\n'
        'LEV ATH 1.88 15 16\n'
        'CAD ELC 2.30 17 18\n'
        'RAY HUE 2.00 19 20'
    )
)

parsed_df = parse_text_lines(raw_text) if raw_text.strip() else pd.DataFrame(
    columns=['match', 'home_team', 'away_team', 'quota_gg', 'rank_home', 'rank_away']
)

if not raw_text.strip():
    st.info('Inserisci le righe manualmente per ottenere la predict finale completa GG / NG.')
elif parsed_df.empty:
    st.error('Nessuna riga riconosciuta. Usa il formato: NomePartita quotaGG rankCasa rankTrasferta')
else:
    score_df = build_match_scores(parsed_df, df)
    pred_df, expected_gg_total, gg_slots = assign_topn_predictions(score_df)
    summary = build_manual_summary(pred_df, expected_gg_total, gg_slots)
    global_trend = get_global_trend_score(df)

    g1, g2, g3, g4 = st.columns(4)
    g1.metric('Trend globale 5/10 blocchi', f"{global_trend['trend_score']:.1%}")
    g2.metric('GG attesi totali', summary['expected_gg_total'])
    g3.metric('Partite previste GG', summary['predicted_gg_count'])
    g4.metric('Partite previste NG', summary['predicted_ng_count'])
    st.caption(
        f"Logica finale: vengono marcate GG le migliori {summary['expected_gg_rounded']} "
        f"partite del ranking, coerentemente con i GG attesi totali."
    )

    st.markdown('### Predict completa match per match')
    predict_list = pred_df[['match', 'prediction', 'score_finale']].copy()
    predict_list['score_finale'] = (predict_list['score_finale'] * 100).round(2)
    predict_list.columns = ['Match', 'Previsione', 'Score %']
    st.dataframe(predict_list, use_container_width=True, hide_index=True)

    st.markdown('### Elenco secco finale')
    gg_list = pred_df[pred_df['prediction'] == 'GG']['match'].tolist()
    ng_list = pred_df[pred_df['prediction'] == 'NG']['match'].tolist()

    c1, c2 = st.columns(2)
    with c1:
        st.markdown('#### ✅ Match previsti GG')
        for m in gg_list:
            st.write(f'- {m}')
        if not gg_list:
            st.write('Nessun match previsto GG')
    with c2:
        st.markdown('#### ❌ Match previsti NG')
        for m in ng_list:
            st.write(f'- {m}')
        if not ng_list:
            st.write('Nessun match previsto NG')

    st.markdown('### Tabella tecnica completa')
    display_df = pred_df.copy()
    for col in [
        'prob_mercato', 'home_rate_5', 'home_rate_10',
        'away_rate_5', 'away_rate_10', 'team_trend_avg',
        'global_trend', 'bonus_classifica', 'score_finale'
    ]:
        display_df[col] = (display_df[col] * 100).round(2)
    display_df.columns = [
        'Match', 'Team casa', 'Team trasferta', 'Quota GG', 'Prob. mercato %',
        'Casa rate 5g %', 'Casa rate 10g %', 'Trasferta rate 5g %', 'Trasferta rate 10g %',
        'Trend medio match %', 'Trend globale %', 'Bonus classifica %',
        'Score finale %', 'Previsione',
        'Casa match 5g', 'Casa match 10g', 'Trasferta match 5g', 'Trasferta match 10g'
    ]
    st.dataframe(display_df, use_container_width=True, hide_index=True)

    st.markdown('### Grafici predict')
    pcol1, pcol2 = st.columns(2)
    with pcol1:
        chart_df = pred_df.set_index('match')[['score_finale']]
        st.bar_chart(chart_df, height=320)
    with pcol2:
        pred_counts = pred_df.groupby('prediction').size().reset_index(name='count')
        if not pred_counts.empty:
            st.bar_chart(pred_counts.set_index('prediction')[['count']], height=320)
        else:
            st.info('Nessuna predict disponibile.')

    with st.expander('Formula predict usata', expanded=False):
        st.write(f'Blocco Gold League: {MATCHES_PER_BLOCK} partite per giornata (stesso formato Sisal FAS League).')
        st.write('Forecast generale prossimo blocco: 50% ultimi 5 blocchi + 30% ultimi 10 blocchi + 20% ultimi 20 blocchi.')
        st.write('I blocchi mantengono la giornata originale da 1 a 34.')
        st.write('Se la sequenza 1-34 riparte, viene creato un nuovo ciclo distinto.')
        st.write('Trend squadra = 60% rate ultime 5 giornate + 40% rate ultime 10 giornate.')
        st.write('Trend match = media trend squadra casa e trasferta.')
        st.write('Score finale = 50% trend match + 25% trend globale + 20% probabilità mercato + 5% bonus classifica.')
        st.write('GG attesi totali = somma degli score finali delle partite inserite.')
        st.write('Predict finale = Top-N del ranking, dove N = numero arrotondato di GG attesi totali.')
