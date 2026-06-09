import re
from datetime import datetime, timedelta, timezone
from math import comb

import pandas as pd
import requests
import streamlit as st
import plotly.graph_objects as go

st.set_page_config(page_title='FAS League Tracker', layout='wide')

st.title('FAS League Tracker')
st.caption('VERSIONE CODICE: 2026-06-09 22:00 - label_chart_fix')
st.caption(
    'Archivio risultati Sisal con giornate Sisal 1-22 senza duplicati, cicli distinti, '
    'forecast su blocchi da 6, ranking manuale GG/NG e reset giornaliero dopo l\'1:00'
)

LOCAL_TZ_OFFSET_HOURS = 1
MATCHES_PER_BLOCK = 6
MAX_GIORNATA = 22
REQUEST_TIMEOUT = 30

TEAM_NAME_MAP = {
    'GEN': 'GEN', 'NAP': 'NAP', 'UDI': 'UDI', 'MIL': 'MIL', 'INT': 'INT', 'ROM': 'ROM',
    'FIO': 'FIO', 'LAZ': 'LAZ', 'SAM': 'SAM', 'ATA': 'ATA', 'VER': 'VER', 'JUV': 'JUV'
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
# Storico / API Sisal
# -------------------------
def infer_gol_gol(result_list):
    for rr in result_list:
        market = str(rr.get('descrizioneScommessa') or rr.get('modelloScommessa') or '').lower()
        result = str(rr.get('risultato') or rr.get('descrizioneEsito') or '').lower()
        if 'goal/no goal' in market or 'gol/no gol' in market:
            if '+ goal' in result or result.strip() in ['goal', 'gol', 'gg']:
                return 'GOL'
            if '+ no goal' in result or '+ no gol' in result or result.strip() in ['no goal', 'no gol', 'nogol', 'ng']:
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


def fetch_matches():
    operational_dt = get_operational_datetime()
    date_str = operational_dt.strftime('%d-%m-%Y')
    api_url = f'https://betting.sisal.it/api/vrol-api/vrol/archivio/getArchivioGareCampionato/1/3/6/{date_str}'
    response = requests.get(
        api_url,
        timeout=REQUEST_TIMEOUT,
        headers={
            'User-Agent': 'Mozilla/5.0',
            'Accept': 'application/json',
            'Referer': 'https://www.sisal.it/'
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

    df['match_nel_blocco'] = (
        df.groupby('group_key', sort=False).cumcount() + 1
    )

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
# Blocchi reali Sisal
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
    missing = [c for c in needed if c not in valid_df.columns]
    if missing:
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
        'rate_5': rate_5,
        'rate_10': rate_10,
        'rate_20': rate_20,
        'weighted_rate': weighted_rate,
        'next_block_expected': next_block_expected,
        'next_block_rounded': next_block_rounded,
        'next_3_blocks_expected': next_3_blocks_expected,
        'range_min': range_min,
        'range_max': range_max,
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

        def mean_rate(n):
            sub = hist.tail(n)
            return float(sub['rate'].mean()) if not sub.empty else 0.0

        rate_5 = mean_rate(5)
        rate_10 = mean_rate(10)
        rate_20 = mean_rate(20)
        weighted_rate = max(0.0, min(1.0, (0.5 * rate_5) + (0.3 * rate_10) + (0.2 * rate_20)))
        predicted = round(weighted_rate * MATCHES_PER_BLOCK, 2)
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

    def binom_prob_at_least(k, n=MATCHES_PER_BLOCK, p=0.0):
        return sum(comb(n, i) * (p ** i) * ((1 - p) ** (n - i)) for i in range(k, n + 1))

    p_ge4 = binom_prob_at_least(4, MATCHES_PER_BLOCK, p)
    p_ge5 = binom_prob_at_least(5, MATCHES_PER_BLOCK, p)
    p_eq6 = p ** MATCHES_PER_BLOCK

    alert_level = 'low'
    if forecast['next_block_expected'] >= 4.5 or p_eq6 >= 0.12:
        alert_level = 'high'
    elif forecast['next_block_expected'] >= 3.8 or p_ge4 >= 0.55:
        alert_level = 'medium'

    if alert_level == 'high':
        msg = f"Alert alto: forecast {forecast['next_block_expected']} GG, P(4+)= {p_ge4:.1%}, P(6)= {p_eq6:.1%}."
    elif alert_level == 'medium':
        msg = f"Alert medio: forecast {forecast['next_block_expected']} GG, P(4+)= {p_ge4:.1%}."
    else:
        msg = f"Scenario standard: forecast {forecast['next_block_expected']} GG, P(4+)= {p_ge4:.1%}."

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
            'group_label', 'label_chart', 'GG', '% sul totale', 'gg_ma_3', 'gg_ma_5',
            'pct_ma_3', 'pct_ma_5', 'state_color', 'state_label', 'cycle_id', 'giornata',
            'orario_inizio'
        ])

    chart_df = blocks.copy().sort_values(['cycle_id', 'giornata'], ascending=[True, True], kind='stable').reset_index(drop=True)
    chart_df['orario_inizio'] = chart_df['orario_inizio'].fillna('').astype(str).str[:5]
    chart_df['label_chart'] = chart_df.apply(
        lambda r: f"Giornata {int(r['giornata'])} · {r['orario_inizio'] if r['orario_inizio'] else '--:--'}", axis=1
    )
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


def build_orario_gg_table(df):
    blocks = build_blocks(df)
    if blocks.empty:
        return pd.DataFrame(columns=['giornata', 'orario', 'GG', 'NO_GOL', 'partite', '%GG'])
    out = blocks[['giornata', 'orario_inizio', 'GG', 'NO_GOL', 'partite', '% sul totale']].copy()
    out = out.rename(columns={
        'orario_inizio': 'orario',
        '% sul totale': '%GG'
    })
    out['orario'] = out['orario'].fillna('').astype(str).str[:5]
    out = out.sort_values(['giornata', 'orario'], ascending=[True, True], kind='stable').reset_index(drop=True)
    return out


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
        'rate_5': rate_5,
        'rate_10': rate_10,
        'trend_score': trend_score,
        'matches_5': matches_5,
        'matches_10': matches_10,
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


# -------------------------
# UI STORICO PRINCIPALE
# -------------------------
if 'active_data_day' not in st.session_state:
    st.session_state['active_data_day'] = get_operational_datetime().date().isoformat()

if st.button('Aggiorna risultati', type='primary'):
    did_reset = maybe_reset_daily_after_one()
    try:
        matches, api_day = fetch_matches()
        st.session_state['matches'] = matches
        st.session_state['last_update'] = local_now().strftime('%d-%m-%Y %H:%M:%S')
        st.session_state['active_data_day'] = get_operational_datetime().date().isoformat()
        st.session_state['api_day_used'] = api_day
        if did_reset:
            st.success(st.session_state.get('reset_notice', 'Reset giornaliero eseguito.'))
        st.success(f'Partite trovate: {len(matches)}')
    except Exception as e:
        st.error(f'Errore API: {e}')

matches = st.session_state.get('matches', [])
last_update = st.session_state.get('last_update', '-')
api_day_used = st.session_state.get('api_day_used', get_operational_datetime().strftime('%d-%m-%Y'))

df = prepare_matches_df(matches)

if not df.empty:
    st.markdown(f'**Ultimo aggiornamento (locale):** {last_update}')
    st.caption(
        f"Giorno dati attivo: {st.session_state.get('active_data_day')} | "
        f"Data API usata: {api_day_used}"
    )

    unique_blocks = df['group_key'].nunique() if 'group_key' in df.columns else 0
    duplicate_rows = int(len(matches) - df['match_id'].nunique()) if ('match_id' in df.columns and matches) else 0
    latest_block_size = 0
    if 'group_key' in df.columns and not df.empty:
        tmp_sizes = df.sort_values('sort_timestamp', ascending=False).groupby('group_key').size()
        latest_block_size = int(tmp_sizes.iloc[0]) if not tmp_sizes.empty else 0

    s1, s2, s3 = st.columns(3)
    s1.metric('Partite uniche caricate', int(df['match_id'].nunique()) if 'match_id' in df.columns else 0)
    s2.metric('Blocchi distinti rilevati', unique_blocks)
    s3.metric('Ultimo blocco', f'{latest_block_size}/{MATCHES_PER_BLOCK}')
    if duplicate_rows > 0:
        st.warning(f'Deduplica applicata: rimossi {duplicate_rows} duplicati in fase di caricamento.')

    trend = build_trend_metrics(df)
    col1, col2, col3 = st.columns(3)
    col1.metric('Partite GG ultimi 5 blocchi', trend['last5'], trend['last5'] - trend['prev5'])
    col2.metric('Partite GG ultimi 10 blocchi', trend['last10'], trend['last10'] - trend['prev10'])
    col3.metric('% partite GG ultimo blocco', f"{trend['latest_block_pct']}%")

    st.subheader('Previsione prossimi blocchi')
    forecast = build_forecast(df)
    fc1, fc2, fc3 = st.columns(3)
    fc1.metric('GG attesi prossimo blocco', forecast['next_block_expected'])
    fc2.metric('Stima arrotondata prossimo blocco', forecast['next_block_rounded'])
    fc3.metric('GG attesi prossimi 3 blocchi', forecast['next_3_blocks_expected'])
    st.caption(f"Range stimato prossimo blocco: {forecast['range_min']} - {forecast['range_max']} GG")
    st.dataframe(forecast['details'], use_container_width=True, hide_index=True)

    st.subheader('Grafici storico e forecast')
    trend_chart_df = build_trend_visual_df(df)
    trend_chart_view = trend_chart_df.copy()
    trend_status = build_trend_status(df)
    forecast_compare_df = build_forecast_compare_df(df)
    heatmap_df = build_heatmap_pivot(df)

    t1, t2, t3, t4 = st.columns(4)
    t1.metric('Stato trend', trend_status['label'])
    t2.metric('Delta MA3 vs MA5', trend_status['delta_short_vs_long'])
    t3.metric('Momento 4+ GG', trend_status['momentum'])
    t4.metric('Stress <=2 GG', trend_status['stress'])
    st.caption(
        f"Ultimo blocco: {trend_status['last_gg']} GG ({trend_status['last_pct']:.1f}%) | "
        f"Scostamento vs MA5: {trend_status['last_vs_ma5']}"
    )

    st.markdown('#### GG per blocco con medie mobili')
    if not trend_chart_view.empty:
        gg_chart = trend_chart_view[['label_chart', 'GG', 'gg_ma_3', 'gg_ma_5']].copy()
        gg_chart['GG <=2'] = gg_chart['GG'].apply(lambda x: x if x <= 2 else None)
        gg_chart['GG =3'] = gg_chart['GG'].apply(lambda x: x if x == 3 else None)
        gg_chart['GG >3'] = gg_chart['GG'].apply(lambda x: x if x > 3 else None)

        fig = go.Figure()
        fig.add_bar(x=gg_chart['label_chart'], y=gg_chart['GG <=2'], name='GG <=2', marker_color='#dc2626')
        fig.add_bar(x=gg_chart['label_chart'], y=gg_chart['GG =3'], name='GG =3', marker_color='#eab308')
        fig.add_bar(x=gg_chart['label_chart'], y=gg_chart['GG >3'], name='GG >3', marker_color='#16a34a')
        fig.add_scatter(x=gg_chart['label_chart'], y=gg_chart['gg_ma_3'], mode='lines', name='gg_ma_3', line=dict(color='#7dd3fc', width=2))
        fig.add_scatter(x=gg_chart['label_chart'], y=gg_chart['gg_ma_5'], mode='lines', name='gg_ma_5', line=dict(color='#38bdf8', width=3))
        fig.update_layout(barmode='overlay', height=520, xaxis_title='Giornata + orario', yaxis_title='GG', legend_title='Serie')
        fig.update_xaxes(type='category', categoryorder='array', categoryarray=gg_chart['label_chart'].tolist())
        fig.update_yaxes(range=[0, 6], dtick=1)
        fig.update_xaxes(tickangle=-90)
        st.plotly_chart(fig, use_container_width=True)
        st.caption('Asse Y fisso 0-6. Etichette solo Giornata + orario. Colori: rosso <=2, giallo =3, verde >3.')
    else:
        st.info('Nessun dato disponibile.')

    st.markdown('#### Percentuale GG per blocco con smoothing')
    if not trend_chart_view.empty:
        pct_chart = trend_chart_view[['label_chart', '% sul totale', 'pct_ma_3', 'pct_ma_5']].copy().set_index('label_chart')
        st.line_chart(pct_chart[['% sul totale', 'pct_ma_3', 'pct_ma_5']], height=420, use_container_width=True)
        st.caption('Vista completa. La linea % sul totale mostra la qualità del blocco: sopra 50% sei in equilibrio positivo, sopra 60% hai spinta forte.')
    else:
        st.info('Nessun dato disponibile.')

    cfa, cfb = st.columns(2)
    with cfa:
        st.markdown('#### Confronto rapido forecast')
        if not forecast_compare_df.empty:
            st.bar_chart(forecast_compare_df.set_index('metrica')[['valore']], height=320, use_container_width=True)
        else:
            st.info('Nessun dato disponibile.')
    with cfb:
        st.markdown('#### Heatmap GG per ciclo/giornata')
        if not heatmap_df.empty:
            st.dataframe(heatmap_df, use_container_width=True)
            st.caption('Lettura heatmap: valori più alti = più GG in quella giornata del ciclo.')
        else:
            st.info('Nessun dato disponibile.')

    st.markdown('#### Tabella orario e GG per giornata')
    orario_gg_df = build_orario_gg_table(df)
    if not orario_gg_df.empty:
        st.dataframe(orario_gg_df, use_container_width=True, hide_index=True)
        st.caption("Questa tabella mostra per ogni giornata l'orario del blocco e quanti GG sono usciti in quel blocco.")
    else:
        st.info('Nessun dato disponibile.')

    st.subheader('Backtest e probabilità')
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

    st.subheader('Blocchi con 6 GG su 6')
    all_gg_stats = build_all_gg_stats(df)
    col4, col5 = st.columns(2)
    col4.metric('Totale blocchi 6 su 6', all_gg_stats['total_all_gg_blocks'])
    col5.metric('Serie aperta 6 su 6', all_gg_stats['latest_streak'])

    with st.expander('Dettaglio blocchi 6 GG su 6', expanded=False):
        st.dataframe(all_gg_stats['blocks_table'], use_container_width=True, hide_index=True)

    with st.expander('Partite giorno per giorno', expanded=False):
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
                blocco_g = blocco_g.sort_values(['match_nel_blocco', 'orario', 'codice_avvenimento'], ascending=[True, True, True], kind='stable').reset_index(drop=True)
                gg_count = int((blocco_g['esito'] == 'GOL').sum())
                ng_count = int((blocco_g['esito'] == 'NO GOL').sum())
                giornata_value = int(blocco_g['giornata'].iloc[0]) if not blocco_g.empty else 0
                ciclo_value = int(blocco_g['cycle_id'].iloc[0]) if not blocco_g.empty else 0
                with st.expander(
                    f'Giornata {giornata_value} · Ciclo {ciclo_value} · Partite {len(blocco_g)} · GG {gg_count} · NG {ng_count}',
                    expanded=False
                ):
                    st.dataframe(
                        blocco_g[['match_nel_blocco', 'orario', 'giornata', 'codice_avvenimento', 'descrizione_avventimento', 'esito']].rename(columns={'match_nel_blocco': 'n_match'}),
                        use_container_width=True,
                        hide_index=True
                    )
        else:
            st.info('Nessun blocco disponibile.')

    st.subheader('Blocchi Sisal distinti')
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


# -------------------------
# UI PREDICT FINALE TOP-N
# -------------------------
st.divider()
st.subheader('Predict finale GG / NG con logica Top-N')
st.caption('Formato input: NomePartita quotaGG rankCasa rankTrasferta')
st.caption('Esempio: INT ROM 1.97 7 9')

raw_text = st.text_area(
    'Inserimento manuale partite',
    height=220,
    placeholder='GEN NAP 1.98 10 4\nUDI MIL 1.96 2 12\nINT ROM 1.97 7 9'
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
        st.markdown('#### Match previsti GG')
        if gg_list:
            for m in gg_list:
                st.write(f'- {m}')
        else:
            st.write('Nessun match previsto GG')
    with c2:
        st.markdown('#### Match previsti NG')
        if ng_list:
            for m in ng_list:
                st.write(f'- {m}')
        else:
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
        st.write('Forecast generale prossimo blocco: 50% ultimi 5 blocchi + 30% ultimi 10 blocchi + 20% ultimi 20 blocchi.')
        st.write('I blocchi mantengono la giornata Sisal originale da 1 a 22.')
        st.write('Se la sequenza 1-22 riparte, viene creato un nuovo ciclo distinto per evitare di fondere due giornate omonime.')
        st.write('Deduplica applicata solo sui match identici, non sulle giornate omonime di cicli diversi.')
        st.write('Trend squadra = 60% rate ultime 5 giornate/blocchi + 40% rate ultime 10 giornate/blocchi.')
        st.write('Trend match = media trend squadra casa e trasferta.')
        st.write('Score finale = 50% trend match + 25% trend globale + 20% probabilità mercato + 5% bonus classifica.')
        st.write('GG attesi totali = somma degli score finali delle partite inserite.')
        st.write('Predict finale = Top-N del ranking, dove N è il numero arrotondato di GG attesi totali.')
