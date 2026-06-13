import io
import re
from datetime import datetime, timedelta, timezone
from math import comb

import pandas as pd
import plotly.graph_objects as go
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import streamlit as st

st.set_page_config(page_title='FAS League Tracker', layout='wide')

st.title('FAS League Tracker SNAI')
st.caption('VERSIONE CODICE: 2026-06-13 14:12 - snai fastfail debug')
st.caption('Storico Sisal, forecast blocchi, heatmap, ranking manuale, export, storico pronostici, ROI e bankroll tracker.')

LOCAL_TZ_OFFSET_HOURS = 1
MATCHES_PER_BLOCK = 6
MAX_GIORNATA = 22
REQUEST_TIMEOUT = 15
SNAI_CONNECT_TIMEOUT = 6
SNAI_READ_TIMEOUT = 12


def build_retry_session():
    session = requests.Session()
    retry = Retry(
        total=0,
        connect=0,
        read=0,
        backoff_factor=0,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(['GET'])
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=4)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session

TEAM_NAME_MAP = {
    'GEN': 'GEN', 'NAP': 'NAP', 'UDI': 'UDI', 'MIL': 'MIL', 'INT': 'INT', 'ROM': 'ROM',
    'FIO': 'FIO', 'LAZ': 'LAZ', 'SAM': 'SAM', 'ATA': 'ATA', 'VER': 'VER', 'JUV': 'JUV'
}


def local_now() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=LOCAL_TZ_OFFSET_HOURS)


def get_operational_datetime() -> datetime:
    now_utc = datetime.now(timezone.utc)
    local = now_utc + timedelta(hours=LOCAL_TZ_OFFSET_HOURS)
    if local.hour < 1:
        return local - timedelta(days=1)
    return local


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


def normalize_match_name(name):
    name = str(name or '').upper().strip().replace('-', ' ').replace('_', ' ')
    return ' '.join(name.split())


def split_teams(match_name):
    cleaned = normalize_match_name(match_name)
    parts = cleaned.split(' ')
    if len(parts) >= 2:
        return TEAM_NAME_MAP.get(parts[0], parts[0]), TEAM_NAME_MAP.get(parts[1], parts[1])
    return cleaned, ''


def get_event_description(ev):
    candidates = [
        ev.get('descrizioneAvventimento'), ev.get('descrizioneAvvenimento'), ev.get('descrizioneEvento'),
        ev.get('evento'), ev.get('match'), ev.get('avvenimento'), ev.get('nomeEvento'), ev.get('labelEvento')
    ]
    for value in candidates:
        value = str(value or '').strip()
        if value:
            return value.replace(' ', '')
    return ''


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


def fetch_matches_snai():
    url = 'https://betting-snai.flutterseatech.it/api/vrol-api/vrol/palinsesto/1/championships/2600302038/8127'
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7',
        'Origin': 'https://www.snai.it',
        'Referer': 'https://www.snai.it/',
        'Connection': 'keep-alive',
        'Cache-Control': 'no-cache',
        'Pragma': 'no-cache'
    }
    session = build_retry_session()
    response = session.get(url, timeout=(SNAI_CONNECT_TIMEOUT, SNAI_READ_TIMEOUT), headers=headers, stream=False)
    response.raise_for_status()
    content_type = response.headers.get('Content-Type', '')
    if 'application/json' not in content_type.lower() and 'text/json' not in content_type.lower():
        raise RuntimeError(f'Content-Type inatteso: {content_type}')
    data = response.json()

    matches = []
    api_day = local_now().strftime('%d-%m-%Y')

    def walk(obj):
        if isinstance(obj, dict):
            keys = {k.lower(): k for k in obj.keys()}
            desc = ''
            for cand in ['descrizioneavvenimento', 'descrizioneevento', 'evento', 'match', 'description', 'name']:
                if cand in keys:
                    desc = str(obj[keys[cand]] or '').strip()
                    if desc:
                        break
            code_avv = ''
            for cand in ['codiceavvenimento', 'eventid', 'idavvenimento', 'id']:
                if cand in keys:
                    code_avv = str(obj[keys[cand]] or '').strip()
                    if code_avv:
                        break
            orario_raw = ''
            for cand in ['dataora', 'datetime', 'starttime', 'orario']:
                if cand in keys:
                    orario_raw = str(obj[keys[cand]] or '').strip()
                    if orario_raw:
                        break
            giornata_val = None
            for cand in ['giornata', 'matchday', 'round']:
                if cand in keys:
                    giornata_val = normalize_giornata_value(obj[keys[cand]])
                    if giornata_val is not None:
                        break
            esito = 'N/D'
            text_blob = ' '.join([str(v) for v in obj.values() if isinstance(v, (str, int, float))]).lower()
            if 'goal' in text_blob and 'no goal' in text_blob:
                if any(x in text_blob for x in ['esito goal', 'risultato goal', ' + goal', ' gg ']):
                    esito = 'GOL'
                elif any(x in text_blob for x in ['esito no goal', 'risultato no goal', ' + no goal', ' ng ']):
                    esito = 'NO GOL'
            if desc and code_avv:
                dt_value, orario = parse_datetime_fields(api_day, orario_raw)
                home_team, away_team = split_teams(desc)
                matches.append({
                    'match_id': f"snai-{code_avv}",
                    'api_day': api_day,
                    'timestamp': dt_value,
                    'timestamp_str': f"{api_day} {orario_raw}",
                    'orario': orario,
                    'giornata': giornata_val if giornata_val is not None else 1,
                    'codice_palinsesto': 'SNAI',
                    'codice_avvenimento': code_avv,
                    'descrizione_avventimento': desc.replace(' ', ''),
                    'home_team': home_team,
                    'away_team': away_team,
                    'esito': esito,
                })
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(data)

    dedup = {}
    for m in matches:
        dedup[m['match_id']] = m
    return list(dedup.values()), api_day


def fetch_matches_sisal_disabled():
    operational_dt = get_operational_datetime()
    date_str = operational_dt.strftime('%d-%m-%Y')
    api_url = f'https://betting.sisal.it/api/vrol-api/vrol/archivio/getArchivioGareCampionato/1/3/6/{date_str}'
    response = requests.get(api_url, timeout=REQUEST_TIMEOUT, headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json', 'Referer': 'https://www.sisal.it/'})
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
                        'api_day': date_str, 'timestamp': dt_value, 'timestamp_str': f'{date_str} {data_ora}', 'orario': orario,
                        'giornata': giornata_api, 'codice_palinsesto': codice_palinsesto, 'codice_avvenimento': codice_avvenimento,
                        'descrizione_avventimento': desc, 'home_team': home_team, 'away_team': away_team, 'esito': esito,
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


def empty_prepared_df():
    return pd.DataFrame(columns=['match_id', 'api_day', 'timestamp', 'timestamp_str', 'orario', 'giornata', 'codice_palinsesto', 'codice_avvenimento', 'descrizione_avventimento', 'home_team', 'away_team', 'esito', 'sort_timestamp', 'cycle_id', 'group_key', 'group_label', 'match_nel_blocco'])


def prepare_matches_df(matches):
    if not matches:
        return empty_prepared_df()
    df = pd.DataFrame(matches).copy()
    df['timestamp'] = pd.to_datetime(df['timestamp'], errors='coerce')
    df['timestamp_str'] = df['timestamp_str'].fillna('').astype(str)
    fallback_time = pd.to_datetime(df['timestamp_str'], dayfirst=True, errors='coerce')
    df['sort_timestamp'] = df['timestamp'].fillna(fallback_time)
    df['orario'] = df['orario'].fillna('').astype(str).str[:5]
    df['codice_avvenimento'] = df['codice_avvenimento'].fillna('').astype(str)
    df['giornata'] = df['giornata'].apply(normalize_giornata_value)
    df = df.dropna(subset=['giornata']).copy()
    if df.empty:
        return empty_prepared_df()
    df['giornata'] = df['giornata'].astype(int)
    df = df.sort_values(['sort_timestamp', 'giornata', 'orario', 'codice_avvenimento', 'match_id'], kind='stable').reset_index(drop=True)
    cycle_ids = []
    current_cycle = 1
    prev_g = None
    for g in df['giornata'].tolist():
        if prev_g is not None and g < prev_g:
            current_cycle += 1
        cycle_ids.append(current_cycle)
        prev_g = g
    df['cycle_id'] = cycle_ids
    df['group_key'] = df['cycle_id'].astype(str) + '-' + df['giornata'].astype(str)
    df['group_label'] = df.apply(lambda r: f"Ciclo {int(r['cycle_id'])} · Giornata {int(r['giornata'])}", axis=1)
    df['match_nel_blocco'] = df.groupby('group_key', sort=False).cumcount() + 1
    return df


def get_valid_matches_df(df):
    if df is None or df.empty:
        return empty_prepared_df()
    return df[df['esito'].isin(['GOL', 'NO GOL'])].copy()


def build_blocks(df):
    valid_df = get_valid_matches_df(df)
    cols = ['cycle_id', 'giornata', 'partite', 'GG', 'NO_GOL', '% sul totale', 'orario_inizio', 'orario_fine', 'group_label', 'completa']
    if valid_df.empty:
        return pd.DataFrame(columns=cols)
    grouped = valid_df.groupby(['group_key', 'cycle_id', 'giornata', 'group_label'], dropna=False).agg(
        partite=('esito', 'count'), GG=('esito', lambda x: int((x == 'GOL').sum())), NO_GOL=('esito', lambda x: int((x == 'NO GOL').sum())),
        orario_inizio=('orario', 'first'), orario_fine=('orario', 'last'), last_ts=('sort_timestamp', 'max')
    ).reset_index()
    grouped['% sul totale'] = ((grouped['GG'] / grouped['partite']) * 100).round(2)
    grouped['completa'] = grouped['partite'] >= MATCHES_PER_BLOCK
    grouped = grouped.sort_values(['last_ts', 'cycle_id', 'giornata'], ascending=[False, False, False], kind='stable')
    return grouped[cols].reset_index(drop=True)


def build_forecast(df):
    blocks = build_blocks(df)
    if blocks.empty:
        return {'weighted_rate': 0.0, 'next_block_expected': 0.0, 'next_block_rounded': 0, 'next_3_blocks_expected': 0.0, 'range_min': 0, 'range_max': 0, 'details': pd.DataFrame(columns=['finestra', 'percentuale_GG'])}
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
    return {
        'weighted_rate': weighted_rate,
        'next_block_expected': next_block_expected,
        'next_block_rounded': int(round(next_block_expected)),
        'next_3_blocks_expected': round(next_block_expected * 3, 2),
        'range_min': max(0, int(next_block_expected // 1)),
        'range_max': min(MATCHES_PER_BLOCK, int(next_block_expected // 1) + 1),
        'details': pd.DataFrame([
            {'finestra': 'Ultimi 5 blocchi', 'percentuale_GG': round(rate_5 * 100, 2)},
            {'finestra': 'Ultimi 10 blocchi', 'percentuale_GG': round(rate_10 * 100, 2)},
            {'finestra': 'Ultimi 20 blocchi', 'percentuale_GG': round(rate_20 * 100, 2)},
            {'finestra': 'Media pesata finale', 'percentuale_GG': round(weighted_rate * 100, 2)},
        ])
    }


def build_backtest(df):
    valid_df = get_valid_matches_df(df)
    empty = pd.DataFrame(columns=['group_label', 'actual_GG', 'predicted_GG', 'error', 'abs_error'])
    if valid_df.empty:
        return {'mae_10': 0.0, 'mae_20': 0.0, 'bias': 0.0, 'table': empty}
    grouped = valid_df.groupby(['group_key', 'cycle_id', 'giornata', 'group_label'], dropna=False).agg(GG=('esito', lambda x: int((x == 'GOL').sum())), totale=('esito', 'count'), last_ts=('sort_timestamp', 'max')).reset_index()
    grouped = grouped.sort_values(['last_ts', 'cycle_id', 'giornata'], ascending=[True, True, True], kind='stable').reset_index(drop=True)
    if len(grouped) < 2:
        return {'mae_10': 0.0, 'mae_20': 0.0, 'bias': 0.0, 'table': empty}
    grouped['rate'] = grouped['GG'] / grouped['totale']
    preds = []
    for i in range(1, len(grouped)):
        hist = grouped.iloc[:i].copy()
        def mean_rate(n):
            sub = hist.tail(n)
            return float(sub['rate'].mean()) if not sub.empty else 0.0
        weighted_rate = max(0.0, min(1.0, (0.5 * mean_rate(5)) + (0.3 * mean_rate(10)) + (0.2 * mean_rate(20))))
        pred = round(weighted_rate * MATCHES_PER_BLOCK, 2)
        actual = int(grouped.iloc[i]['GG'])
        preds.append({'group_label': grouped.iloc[i]['group_label'], 'actual_GG': actual, 'predicted_GG': pred, 'error': round(pred - actual, 2), 'abs_error': round(abs(pred - actual), 2)})
    backtest_df = pd.DataFrame(preds).iloc[::-1].reset_index(drop=True)
    return {'mae_10': round(float(backtest_df.head(10)['abs_error'].mean()), 2), 'mae_20': round(float(backtest_df.head(20)['abs_error'].mean()), 2), 'bias': round(float(backtest_df['error'].mean()), 2), 'table': backtest_df}


def build_probabilities(df):
    forecast = build_forecast(df)
    p = forecast['weighted_rate']
    def at_least(k):
        return sum(comb(MATCHES_PER_BLOCK, i) * (p ** i) * ((1 - p) ** (MATCHES_PER_BLOCK - i)) for i in range(k, MATCHES_PER_BLOCK + 1))
    p_ge4 = at_least(4)
    p_ge5 = at_least(5)
    p_eq6 = p ** MATCHES_PER_BLOCK
    level = 'low'
    if forecast['next_block_expected'] >= 4.5 or p_eq6 >= 0.12:
        level = 'high'
    elif forecast['next_block_expected'] >= 3.8 or p_ge4 >= 0.55:
        level = 'medium'
    return {'p_ge4': p_ge4, 'p_ge5': p_ge5, 'p_eq6': p_eq6, 'alert_level': level}


def build_trend_visual_df(df):
    blocks = build_blocks(df)
    if blocks.empty:
        return pd.DataFrame(columns=['group_label', 'label_chart', 'GG', '% sul totale', 'gg_ma_3', 'gg_ma_5', 'cycle_id', 'giornata', 'orario_inizio'])
    chart_df = blocks.copy().sort_values(['cycle_id', 'giornata'], ascending=[True, True], kind='stable').reset_index(drop=True)
    chart_df['orario_inizio'] = chart_df['orario_inizio'].fillna('').astype(str).str[:5]
    chart_df['label_chart'] = chart_df.apply(lambda r: f"Giornata {int(r['giornata'])} · {r['orario_inizio'] if r['orario_inizio'] else '--:--'}", axis=1)
    chart_df['gg_ma_3'] = chart_df['GG'].rolling(3, min_periods=1).mean().round(2)
    chart_df['gg_ma_5'] = chart_df['GG'].rolling(5, min_periods=1).mean().round(2)
    return chart_df


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
    out = out.rename(columns={'orario_inizio': 'orario', '% sul totale': '%GG'})
    out['orario'] = out['orario'].fillna('').astype(str).str[:5]
    return out.sort_values(['giornata', 'orario'], ascending=[True, True], kind='stable').reset_index(drop=True)


def build_trend_status(df):
    chart_df = build_trend_visual_df(df)
    if chart_df.empty:
        return {'label': 'Nessun dato', 'delta_short_vs_long': 0.0, 'momentum': 0, 'stress': 0, 'last_gg': 0, 'last_vs_ma5': 0.0}
    short_ma = float(chart_df['gg_ma_3'].iloc[-1])
    long_ma = float(chart_df['gg_ma_5'].iloc[-1])
    delta = round(short_ma - long_ma, 2)
    if delta >= 0.5:
        label = 'Trend in crescita'
    elif delta <= -0.5:
        label = 'Trend in discesa'
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
    last_vs_ma5 = round(last_gg - long_ma, 2)
    return {'label': label, 'delta_short_vs_long': delta, 'momentum': momentum, 'stress': stress, 'last_gg': last_gg, 'last_vs_ma5': last_vs_ma5}


def build_model_diagnostics(df):
    backtest = build_backtest(df)
    blocks = build_blocks(df)
    if blocks.empty or backtest['table'].empty:
        return {'hit_rate': 0.0, 'error_proxy': 0.0, 'hot_blocks_rate': 0.0, 'cold_blocks_rate': 0.0, 'brier_proxy': 0.0}
    bt = backtest['table'].copy()
    bt['pred_prob_proxy'] = (bt['predicted_GG'] / MATCHES_PER_BLOCK).clip(0, 1)
    bt['actual_prob_proxy'] = (bt['actual_GG'] / MATCHES_PER_BLOCK).clip(0, 1)
    bt['brier_component'] = (bt['pred_prob_proxy'] - bt['actual_prob_proxy']) ** 2
    bt['hit'] = (bt['predicted_GG'].round() == bt['actual_GG']).astype(int)
    return {
        'hit_rate': round(float(bt['hit'].mean() * 100), 2),
        'error_proxy': round(float((backtest['mae_10'] + backtest['mae_20']) / 2), 2),
        'hot_blocks_rate': round(float((blocks['GG'] >= 4).mean() * 100), 2),
        'cold_blocks_rate': round(float((blocks['GG'] <= 2).mean() * 100), 2),
        'brier_proxy': round(float(bt['brier_component'].mean()), 4)
    }


def team_recent_form(df, team_code, max_matchdays=10):
    valid_df = get_valid_matches_df(df)
    if valid_df.empty:
        return pd.DataFrame(columns=['group_label', 'esito'])
    subset = valid_df[(valid_df['home_team'] == team_code) | (valid_df['away_team'] == team_code)].copy()
    if subset.empty:
        return pd.DataFrame(columns=['group_label', 'esito'])
    group_order = subset.groupby('group_key', dropna=False).agg(last_ts=('sort_timestamp', 'max'), group_label=('group_label', 'first')).reset_index(drop=True).sort_values('last_ts', ascending=False, kind='stable')
    keep_labels = group_order.head(max_matchdays)['group_label'].tolist()
    subset = subset[subset['group_label'].isin(keep_labels)].copy().drop_duplicates(subset=['group_label', 'home_team', 'away_team', 'esito'])
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
    return float((subset['esito'] == 'GOL').sum() / matches), matches


def get_team_trend_5_10(df, team_code):
    team_df = team_recent_form(df, team_code, 10)
    r5, m5 = rate_from_last_matchdays(team_df, 5)
    r10, m10 = rate_from_last_matchdays(team_df, 10)
    return {'rate_5': r5, 'rate_10': r10, 'trend_score': (0.6 * r5) + (0.4 * r10), 'matches_5': m5, 'matches_10': m10}


def get_global_trend_score(df):
    blocks = build_blocks(df)
    if blocks.empty:
        return {'trend_score': 0.0}
    blocks = blocks.copy()
    blocks['rate'] = blocks['GG'] / blocks['partite']
    r5 = float(blocks.head(5)['rate'].mean()) if not blocks.head(5).empty else 0.0
    r10 = float(blocks.head(10)['rate'].mean()) if not blocks.head(10).empty else 0.0
    return {'trend_score': (0.6 * r5) + (0.4 * r10)}


def clean_decimal(val):
    try:
        return float(str(val).replace(',', '.').strip())
    except Exception:
        return None


def implied_probability_from_odds(odds):
    if odds is None or odds <= 1:
        return None
    return 1 / odds


def compute_ev_percent(model_prob, odds):
    if model_prob is None or odds is None or odds <= 1:
        return None
    return round(((model_prob * odds) - 1) * 100, 2)


def suggested_stake_units(edge, confidence_score):
    if edge is None:
        return 0.0
    if edge >= 0.10 and confidence_score >= 0.68:
        return 2.0
    if edge >= 0.06 and confidence_score >= 0.60:
        return 1.5
    if edge >= 0.03 and confidence_score >= 0.55:
        return 1.0
    if edge > 0:
        return 0.5
    return 0.0


def confidence_band(score):
    if score >= 0.68:
        return 'Alta'
    if score >= 0.58:
        return 'Media'
    return 'Bassa'


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
            rows.append({'match': normalize_match_name(match_name), 'home_team': home_team, 'away_team': away_team, 'quota_gg': clean_decimal(m.group(2)), 'rank_home': int(m.group(3)), 'rank_away': int(m.group(4))})
    return pd.DataFrame(rows)


def build_match_scores(input_df, history_df):
    if input_df.empty:
        return pd.DataFrame(columns=['match', 'score_finale'])
    global_trend = get_global_trend_score(history_df)
    rows = []
    for _, r in input_df.iterrows():
        home_trend = get_team_trend_5_10(history_df, r['home_team'])
        away_trend = get_team_trend_5_10(history_df, r['away_team'])
        prob_mercato = implied_probability_from_odds(r['quota_gg']) or 0.0
        team_trend_avg = (home_trend['trend_score'] + away_trend['trend_score']) / 2
        bonus_classifica = ranking_bonus(r['rank_home'], r['rank_away'])
        score_finale = (0.50 * team_trend_avg) + (0.25 * global_trend['trend_score']) + (0.20 * prob_mercato) + (0.05 * bonus_classifica)
        score_finale = max(0.0, min(0.95, score_finale))
        edge = score_finale - prob_mercato
        rows.append({
            'match': r['match'], 'home_team': r['home_team'], 'away_team': r['away_team'], 'quota_gg': r['quota_gg'],
            'prob_mercato': prob_mercato, 'home_rate_5': home_trend['rate_5'], 'home_rate_10': home_trend['rate_10'],
            'away_rate_5': away_trend['rate_5'], 'away_rate_10': away_trend['rate_10'], 'team_trend_avg': team_trend_avg,
            'global_trend': global_trend['trend_score'], 'bonus_classifica': bonus_classifica, 'score_finale': score_finale,
            'edge': edge, 'ev_pct': compute_ev_percent(score_finale, r['quota_gg']), 'stake_units': suggested_stake_units(edge, score_finale),
            'confidence_band': confidence_band(score_finale), 'home_matches_5': home_trend['matches_5'], 'home_matches_10': home_trend['matches_10'],
            'away_matches_5': away_trend['matches_5'], 'away_matches_10': away_trend['matches_10'],
        })
    return pd.DataFrame(rows).sort_values(['score_finale', 'edge'], ascending=False).reset_index(drop=True)


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


def build_score_bands_table(pred_df):
    if pred_df.empty:
        return pd.DataFrame(columns=['fascia', 'partite', 'GG_predetti', 'stake_medio'])
    work = pred_df.copy()
    bins = [0.0, 0.55, 0.62, 0.70, 1.0]
    labels = ['Bassa', 'Media', 'Buona', 'Alta']
    work['fascia'] = pd.cut(work['score_finale'], bins=bins, labels=labels, include_lowest=True)
    out = work.groupby('fascia', observed=False).agg(partite=('match', 'count'), GG_predetti=('prediction', lambda x: int((x == 'GG').sum())), stake_medio=('stake_units', 'mean')).reset_index()
    out['stake_medio'] = out['stake_medio'].fillna(0).round(2)
    return out


def add_predictions_to_history(pred_df):
    if pred_df.empty:
        return
    hist = st.session_state.get('prediction_history', pd.DataFrame())
    now_str = local_now().strftime('%d-%m-%Y %H:%M:%S')
    add = pred_df[['match', 'prediction', 'score_finale', 'edge', 'ev_pct', 'stake_units', 'confidence_band']].copy()
    add['created_at'] = now_str
    add['actual_result'] = ''
    add['profit_units'] = 0.0
    hist = pd.concat([hist, add], ignore_index=True)
    st.session_state['prediction_history'] = hist


def update_history_results(history_df, result_map, assumed_odds=1.95):
    if history_df.empty:
        return history_df
    out = history_df.copy()
    profits = []
    for _, row in out.iterrows():
        actual = result_map.get(row['match'], row.get('actual_result', ''))
        pred = row['prediction']
        stake = float(row.get('stake_units', 0) or 0)
        profit = 0.0
        if actual in ['GG', 'NG'] and stake > 0:
            if pred == actual:
                profit = round(stake * (assumed_odds - 1), 2)
            else:
                profit = round(-stake, 2)
        profits.append((actual, profit))
    out['actual_result'] = [x[0] for x in profits]
    out['profit_units'] = [x[1] for x in profits]
    return out


def build_roi_by_band(history_df):
    if history_df.empty:
        return pd.DataFrame(columns=['Confidenza', 'Giocate', 'Stake tot', 'Profitto', 'ROI %'])
    hist = history_df.copy()
    hist = hist[hist['actual_result'].isin(['GG', 'NG'])].copy()
    if hist.empty:
        return pd.DataFrame(columns=['Confidenza', 'Giocate', 'Stake tot', 'Profitto', 'ROI %'])
    out = hist.groupby('confidence_band', dropna=False).agg(giocate=('match', 'count'), stake_tot=('stake_units', 'sum'), profitto=('profit_units', 'sum')).reset_index()
    out['ROI %'] = out.apply(lambda r: round((r['profitto'] / r['stake_tot']) * 100, 2) if r['stake_tot'] else 0.0, axis=1)
    out.columns = ['Confidenza', 'Giocate', 'Stake tot', 'Profitto', 'ROI %']
    return out


def build_bankroll_summary(history_df, starting_bankroll):
    if history_df.empty:
        return {'bankroll_finale': starting_bankroll, 'profitto_totale': 0.0, 'drawdown_max': 0.0}
    hist = history_df.copy()
    hist = hist[hist['actual_result'].isin(['GG', 'NG'])].copy()
    if hist.empty:
        return {'bankroll_finale': starting_bankroll, 'profitto_totale': 0.0, 'drawdown_max': 0.0}
    bankroll = starting_bankroll
    peak = starting_bankroll
    drawdown_max = 0.0
    for p in hist['profit_units'].tolist():
        bankroll += p
        peak = max(peak, bankroll)
        dd = peak - bankroll
        drawdown_max = max(drawdown_max, dd)
    return {'bankroll_finale': round(bankroll, 2), 'profitto_totale': round(bankroll - starting_bankroll, 2), 'drawdown_max': round(drawdown_max, 2)}


def df_to_csv_bytes(df):
    return df.to_csv(index=False).encode('utf-8')


def build_position_history(df):
    valid_df = get_valid_matches_df(df)
    if valid_df.empty:
        return pd.DataFrame(columns=['group_label', 'giornata', 'position', 'esito'])
    out = valid_df[['group_label', 'giornata', 'match_nel_blocco', 'esito']].copy()
    out = out.rename(columns={'match_nel_blocco': 'position'})
    out = out[out['position'].between(1, 6)].copy()
    return out.sort_values(['giornata', 'position'], kind='stable').reset_index(drop=True)


def build_position_pattern_suggestions(df, target_gg):
    pos_df = build_position_history(df)
    fallback = {
        'repeat_combo': [],
        'repeat_note': 'Storico insufficiente.',
        'cold_combo': [],
        'cold_note': 'Storico insufficiente.',
        'top_combos_df': pd.DataFrame(columns=['combo', 'frequenza'])
    }
    if pos_df.empty:
        return fallback

    if target_gg <= 0:
        target_gg = 3

    block_sizes = pos_df.groupby('group_label')['position'].max().reset_index(name='max_pos')
    valid_labels = block_sizes[block_sizes['max_pos'] >= 6]['group_label'].tolist()
    if valid_labels:
        pos_df = pos_df[pos_df['group_label'].isin(valid_labels)].copy()
    if pos_df.empty:
        return fallback

    block_patterns = []
    for label, g in pos_df.groupby('group_label', sort=False):
        gg_positions = sorted(g.loc[g['esito'] == 'GOL', 'position'].astype(int).tolist())
        if len(gg_positions) == target_gg:
            block_patterns.append(' '.join(map(str, gg_positions)))

    freq_df = pd.Series(block_patterns).value_counts().reset_index() if block_patterns else pd.DataFrame(columns=['combo', 'frequenza'])
    if not freq_df.empty:
        freq_df.columns = ['combo', 'frequenza']
        repeat_combo = freq_df.iloc[0]['combo'].split()
        repeat_note = f"Pattern ricorrente più frequente su blocchi da {target_gg} GG."
        top_combos_df = freq_df.head(10).copy()
    else:
        all_patterns = []
        for label, g in pos_df.groupby('group_label', sort=False):
            gg_positions = sorted(g.loc[g['esito'] == 'GOL', 'position'].astype(int).tolist())
            if gg_positions:
                all_patterns.append(' '.join(map(str, gg_positions)))
        if all_patterns:
            freq_df = pd.Series(all_patterns).value_counts().reset_index()
            freq_df.columns = ['combo', 'frequenza']
            top_combos_df = freq_df.head(10).copy()
            repeat_combo = freq_df.iloc[0]['combo'].split()
            repeat_note = f"Nessun pattern esatto da {target_gg} GG: mostrato il pattern storico più frequente in assoluto."
        else:
            repeat_combo = []
            repeat_note = 'Nessun pattern storico utile trovato.'
            top_combos_df = pd.DataFrame(columns=['combo', 'frequenza'])

    pos_summary = pos_df.groupby('position').agg(gg_rate=('esito', lambda x: float((x == 'GOL').mean())), total=('esito', 'count')).reset_index()
    last_seen = []
    ordered_blocks = list(dict.fromkeys(pos_df['group_label'].tolist()))[::-1]
    for pos in range(1, 7):
        streak = 0
        seen_any = False
        for label in ordered_blocks:
            row = pos_df[(pos_df['group_label'] == label) & (pos_df['position'] == pos)]
            if row.empty:
                continue
            seen_any = True
            esito = row.iloc[0]['esito']
            if esito == 'GOL':
                break
            streak += 1
        if not seen_any:
            streak = 0
        last_seen.append({'position': pos, 'cold_streak': streak})
    cold_df = pd.DataFrame(last_seen).merge(pos_summary, on='position', how='left')
    cold_df['gg_rate'] = cold_df['gg_rate'].fillna(0)
    cold_df['cold_score'] = cold_df['cold_streak'] + ((1 - cold_df['gg_rate']) * 2)
    cold_df = cold_df.sort_values(['cold_score', 'cold_streak', 'position'], ascending=[False, False, True], kind='stable')
    cold_combo = cold_df.head(min(target_gg, 6))['position'].astype(int).sort_values().astype(str).tolist()
    cold_note = 'Pattern freddo: posizioni che non fanno GG da più blocchi o che storicamente hanno resa più bassa.'

    return {
        'repeat_combo': repeat_combo,
        'repeat_note': repeat_note,
        'cold_combo': cold_combo,
        'cold_note': cold_note,
        'top_combos_df': top_combos_df
    }


if st.button('Aggiorna risultati', type='primary'):
    try:
        matches, api_day = fetch_matches_snai()
        st.session_state['matches'] = matches
        st.session_state['last_update'] = local_now().strftime('%d-%m-%Y %H:%M:%S')
        st.session_state['api_day_used'] = api_day
        st.success(f'Partite trovate: {len(matches)}')
    except Exception as e:
        st.error(f'Errore API: {e}')

matches = st.session_state.get('matches', [])
df = prepare_matches_df(matches)
last_update = st.session_state.get('last_update', '-')
api_day_used = st.session_state.get('api_day_used', get_operational_datetime().strftime('%d-%m-%Y'))

if not df.empty:
    st.markdown(f'**Ultimo aggiornamento (locale):** {last_update}')
    st.caption(f'Data API usata: {api_day_used}')
    s1, s2, s3 = st.columns(3)
    s1.metric('Partite uniche caricate', int(df['match_id'].nunique()))
    s2.metric('Blocchi distinti rilevati', int(df['group_key'].nunique()))
    s3.metric('Ultima giornata rilevata', int(df['giornata'].max()))

    st.subheader('Forecast e trend')
    forecast = build_forecast(df)
    prob = build_probabilities(df)
    backtest = build_backtest(df)
    diagnostics = build_model_diagnostics(df)
    fc1, fc2, fc3, fc4 = st.columns(4)
    fc1.metric('GG attesi prossimo blocco', forecast['next_block_expected'])
    fc2.metric('GG attesi prossimi 3 blocchi', forecast['next_3_blocks_expected'])
    fc3.metric('Prob. 4+ GG', f"{prob['p_ge4']:.1%}")
    fc4.metric('Brier proxy', diagnostics['brier_proxy'])
    st.caption(f"Range stimato prossimo blocco: {forecast['range_min']} - {forecast['range_max']} GG")
    st.dataframe(forecast['details'], use_container_width=True, hide_index=True)

    trend_chart_df = build_trend_visual_df(df)
    trend_status = build_trend_status(df)

    tr1, tr2, tr3, tr4 = st.columns(4)
    tr1.metric('Stato trend', trend_status['label'])
    tr2.metric('Delta MA3 vs MA5', trend_status['delta_short_vs_long'])
    tr3.metric('Momento 4+ GG', trend_status['momentum'])
    tr4.metric('Stress <=2 GG', trend_status['stress'])
    st.caption(f"Ultimo blocco: {trend_status['last_gg']} GG | Scostamento vs MA5: {trend_status['last_vs_ma5']}")

    st.markdown('#### GG per blocco con medie mobili')
    if not trend_chart_df.empty:
        gg_chart = trend_chart_df[['label_chart', 'GG', 'gg_ma_3', 'gg_ma_5']].copy()
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
        fig.update_yaxes(range=[0, 6], dtick=1)
        fig.update_xaxes(type='category', categoryorder='array', categoryarray=gg_chart['label_chart'].tolist(), tickangle=-90)
        st.plotly_chart(fig, use_container_width=True)

    with st.expander('Partite giorno per giorno', expanded=False):
        storico_df = df[['cycle_id', 'giornata', 'group_label', 'match_nel_blocco', 'orario', 'codice_avvenimento', 'descrizione_avventimento', 'esito', 'group_key', 'sort_timestamp']].copy()
        block_order = storico_df.groupby('group_key', dropna=False).agg(last_ts=('sort_timestamp', 'max'), group_label=('group_label', 'first')).reset_index(drop=True).sort_values('last_ts', ascending=False, kind='stable')
        ordered_labels = block_order['group_label'].tolist()
        for label in ordered_labels:
            blocco_g = storico_df[storico_df['group_label'] == label].copy().sort_values(['match_nel_blocco', 'orario', 'codice_avvenimento'], kind='stable').reset_index(drop=True)
            gg_count = int((blocco_g['esito'] == 'GOL').sum())
            ng_count = int((blocco_g['esito'] == 'NO GOL').sum())
            giornata_value = int(blocco_g['giornata'].iloc[0]) if not blocco_g.empty else 0
            with st.expander(f'Giornata {giornata_value} · Partite {len(blocco_g)} · GG {gg_count} · NG {ng_count}', expanded=False):
                st.dataframe(
                    blocco_g[['match_nel_blocco', 'orario', 'giornata', 'codice_avvenimento', 'descrizione_avventimento', 'esito']].rename(columns={'match_nel_blocco': 'n_match'}),
                    use_container_width=True,
                    hide_index=True
                )

    st.subheader('Pattern combinazioni per posizione')
    target_pattern_gg = st.slider('Numero GG da usare per i pattern posizione', min_value=1, max_value=6, value=3, step=1)
    pattern_info = build_position_pattern_suggestions(df, target_pattern_gg)
    p1, p2 = st.columns(2)
    with p1:
        combo_repeat = ' '.join(pattern_info['repeat_combo']) if pattern_info['repeat_combo'] else '-'
        st.metric('Pattern ricorrente', combo_repeat)
        st.caption(pattern_info['repeat_note'])
    with p2:
        combo_cold = ' '.join(pattern_info['cold_combo']) if pattern_info['cold_combo'] else '-'
        st.metric('Pattern freddo', combo_cold)
        st.caption(pattern_info['cold_note'])
    if not pattern_info['top_combos_df'].empty:
        st.markdown('#### Combinazioni storiche più frequenti')
        st.dataframe(pattern_info['top_combos_df'], use_container_width=True, hide_index=True)

    st.subheader('Centro decisionale')
    d1, d2, d3, d4 = st.columns(4)
    d1.metric('Hit rate backtest', f"{diagnostics['hit_rate']:.1f}%")
    d2.metric('Errore medio proxy', diagnostics['error_proxy'])
    d3.metric('Blocchi caldi 4+ GG', f"{diagnostics['hot_blocks_rate']:.1f}%")
    d4.metric('Blocchi freddi <=2 GG', f"{diagnostics['cold_blocks_rate']:.1f}%")

    st.subheader('Backtest dettagli')
    st.dataframe(backtest['table'], use_container_width=True, hide_index=True)

else:
    st.info("Premi 'Aggiorna risultati' per caricare i dati storici del giorno.")

st.divider()
st.subheader('Predict finale GG / NG con logica Top-N')
st.caption('Formato input: NomePartita quotaGG rankCasa rankTrasferta')
filter_only_positive_ev = st.checkbox('Mostra solo partite con edge positivo', value=False)
minimum_score = st.slider('Score minimo da mostrare', min_value=0.40, max_value=0.90, value=0.52, step=0.01)
raw_text = st.text_area('Inserimento manuale partite', height=220, placeholder='GEN NAP 1.98 10 4\nUDI MIL 1.96 2 12\nINT ROM 1.97 7 9')
parsed_df = parse_text_lines(raw_text) if raw_text.strip() else pd.DataFrame(columns=['match', 'home_team', 'away_team', 'quota_gg', 'rank_home', 'rank_away'])

if raw_text.strip() and not parsed_df.empty:
    score_df = build_match_scores(parsed_df, df)
    if filter_only_positive_ev:
        score_df = score_df[score_df['edge'] > 0].copy()
    score_df = score_df[score_df['score_finale'] >= minimum_score].copy()
    pred_df, expected_gg_total, gg_slots = assign_topn_predictions(score_df)
    score_bands_df = build_score_bands_table(pred_df)

    g1, g2, g3, g4 = st.columns(4)
    g1.metric('GG attesi totali', round(expected_gg_total, 2))
    g2.metric('Partite previste GG', int((pred_df['prediction'] == 'GG').sum()) if not pred_df.empty else 0)
    g3.metric('Partite +EV', int((pred_df['edge'] > 0).sum()) if not pred_df.empty else 0)
    g4.metric('Stake totale suggerito', round(float(pred_df['stake_units'].sum()), 2) if not pred_df.empty else 0.0)

    st.markdown('### Predict completa match per match')
    for c, default in [('prediction','NG'), ('score_finale',0.0), ('edge',0.0), ('ev_pct',0.0), ('stake_units',0.0), ('confidence_band','Bassa')]:
        if c not in pred_df.columns:
            pred_df[c] = default
    predict_cols = ['match', 'prediction', 'score_finale', 'edge', 'ev_pct', 'stake_units', 'confidence_band']
    predict_list = pred_df[predict_cols].copy()
    predict_list['score_finale'] = (pd.to_numeric(predict_list['score_finale'], errors='coerce').fillna(0) * 100).round(2)
    predict_list['edge'] = (pd.to_numeric(predict_list['edge'], errors='coerce').fillna(0) * 100).round(2)
    predict_list['ev_pct'] = pd.to_numeric(predict_list['ev_pct'], errors='coerce').fillna(0).round(2)
    predict_list['stake_units'] = pd.to_numeric(predict_list['stake_units'], errors='coerce').fillna(0).round(2)
    predict_list.columns = ['Match', 'Previsione', 'Score %', 'Edge %', 'EV %', 'Stake', 'Confidenza']
    st.dataframe(predict_list, use_container_width=True, hide_index=True)

    ex1, ex2 = st.columns(2)
    with ex1:
        st.download_button('Scarica ranking CSV', data=df_to_csv_bytes(predict_list), file_name='ranking_predict.csv', mime='text/csv')
    with ex2:
        st.download_button('Scarica tabella tecnica CSV', data=df_to_csv_bytes(pred_df), file_name='ranking_tecnico.csv', mime='text/csv')

    if st.button('Salva queste previsioni nello storico'):
        add_predictions_to_history(pred_df)
        st.success('Previsioni salvate nello storico.')

    st.markdown('### Fasce di qualità del ranking')
    st.dataframe(score_bands_df, use_container_width=True, hide_index=True)

history_df = st.session_state.get('prediction_history', pd.DataFrame())
if not history_df.empty:
    st.divider()
    st.subheader('Storico pronostici e controllo risultati')
    st.caption('Inserisci risultati reali nel formato: NOMEPARTITA GG oppure NOMEPARTITA NG')
    actual_text = st.text_area('Aggiorna esiti reali storico', height=140, placeholder='GEN NAP GG\nUDI MIL NG')
    assumed_odds = st.number_input('Quota standard per calcolo profitto', min_value=1.01, max_value=10.0, value=1.95, step=0.01)
    result_map = {}
    for line in actual_text.splitlines():
        line = ' '.join(line.strip().split())
        if not line:
            continue
        if line.upper().endswith(' GG'):
            result_map[line[:-3].strip().upper()] = 'GG'
        elif line.upper().endswith(' NG'):
            result_map[line[:-3].strip().upper()] = 'NG'
    if st.button('Aggiorna storico con esiti reali'):
        st.session_state['prediction_history'] = update_history_results(history_df, result_map, assumed_odds)
        st.success('Storico aggiornato.')
        history_df = st.session_state.get('prediction_history', pd.DataFrame())

    history_df = st.session_state.get('prediction_history', pd.DataFrame())
    roi_band_df = build_roi_by_band(history_df)
    starting_bankroll = st.number_input('Bankroll iniziale', min_value=1.0, value=100.0, step=1.0)
    bankroll = build_bankroll_summary(history_df, starting_bankroll)
    b1, b2, b3 = st.columns(3)
    b1.metric('Bankroll finale', bankroll['bankroll_finale'])
    b2.metric('Profitto totale', bankroll['profitto_totale'])
    b3.metric('Drawdown max', bankroll['drawdown_max'])

    st.markdown('### ROI per fascia score/confidenza')
    st.dataframe(roi_band_df, use_container_width=True, hide_index=True)
    st.download_button('Scarica storico pronostici CSV', data=df_to_csv_bytes(history_df), file_name='storico_pronostici.csv', mime='text/csv')
    st.dataframe(history_df, use_container_width=True, hide_index=True)
