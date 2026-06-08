import re
from datetime import datetime, timedelta, timezone
from math import comb

import pandas as pd
import plotly.express as px
import requests
import streamlit as st

st.set_page_config(page_title='FAS League Tracker', layout='wide')

st.title('FAS League Tracker')
st.caption(
    "Archivio risultati Sisal con statistiche complete, grafici full width, elenco partite "
    "giorno per giorno in ordine cronologico, forecast su massimo 10 blocchi, "
    "predict Top-N e reset giornaliero dopo l'1:00"
)

LOCAL_TZ_OFFSET_HOURS = 1


def local_now() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=LOCAL_TZ_OFFSET_HOURS)


TEAM_NAME_MAP = {
    'GEN': 'GEN', 'NAP': 'NAP', 'UDI': 'UDI', 'MIL': 'MIL', 'INT': 'INT', 'ROM': 'ROM',
    'FIO': 'FIO', 'LAZ': 'LAZ', 'SAM': 'SAM', 'ATA': 'ATA', 'VER': 'VER', 'JUV': 'JUV'
}


# -------------------------
# Utility tempo / reset
# -------------------------
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


def fetch_matches():
    operational_dt = get_operational_datetime()
    date_str = operational_dt.strftime('%d-%m-%Y')
    api_url = f'https://betting.sisal.it/api/vrol-api/vrol/archivio/getArchivioGareCampionato/1/3/6/{date_str}'
    r = requests.get(api_url, timeout=30, headers={
        'User-Agent': 'Mozilla/5.0',
        'Accept': 'application/json',
        'Referer': 'https://www.sisal.it/'
    })
    r.raise_for_status()
    data = r.json()
    matches = []

    if not isinstance(data, list):
        return matches, date_str

    for giornata_block in data:
        giornata = giornata_block.get('giornata')
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

                    matches.append({
                        'match_id': f'{date_str}-{codice_palinsesto}-{codice_avvenimento}',
                        'timestamp': f'{date_str} {data_ora}',
                        'orario': data_ora,
                        'giornata': giornata,
                        'codice_avvenimento': codice_avvenimento,
                        'descrizione_avventimento': desc,
                        'home_team': home_team,
                        'away_team': away_team,
                        'esito': esito,
                    })

    dedup = {}
    for m in matches:
        dedup[m['match_id']] = m
    results = list(dedup.values())
    results.sort(key=lambda x: x['timestamp'], reverse=True)
    return results, date_str


def build_blocks(df):
    valid_df = df[df['esito'].isin(['GOL', 'NO GOL'])].copy()
    if valid_df.empty:
        return pd.DataFrame(columns=['giornata', 'orario', 'GOL', '% sul totale', 'label_x', 'fascia_colore'])

    grouped = valid_df.groupby(['giornata', 'orario']).agg(
        totale=('esito', 'count'),
        GOL=('esito', lambda x: (x == 'GOL').sum())
    ).reset_index()

    grouped['% sul totale'] = ((grouped['GOL'] / grouped['totale']) * 100).round(2)
    grouped = grouped.sort_values(['giornata', 'orario'], ascending=[True, True])
    grouped['label_x'] = grouped.apply(lambda r: f"Giornata {int(r['giornata'])} - {r['orario']}", axis=1)
    grouped['fascia_colore'] = grouped['GOL'].apply(
        lambda v: 'GG <=2' if v <= 2 else ('GG =3' if v == 3 else 'GG >3')
    )
    return grouped


def build_trend_metrics(df):
    valid_df = df[df['esito'].isin(['GOL', 'NO GOL'])].copy()
    if valid_df.empty:
        return {'last5': 0, 'prev5': 0, 'last10': 0, 'prev10': 0, 'latest_block_pct': 0.0}
    grouped = valid_df.groupby('orario').agg(
        totale=('esito', 'count'),
        gol=('esito', lambda x: (x == 'GOL').sum())
    ).reset_index().sort_values('orario', ascending=False)
    grouped['pct'] = ((grouped['gol'] / grouped['totale']) * 100).round(2)
    return {
        'last5': int(grouped.head(5)['gol'].sum()),
        'prev5': int(grouped.iloc[5:10]['gol'].sum()) if len(grouped) > 5 else 0,
        'last10': int(grouped.head(10)['gol'].sum()),
        'prev10': int(grouped.iloc[10:20]['gol'].sum()) if len(grouped) > 10 else 0,
        'latest_block_pct': float(grouped.iloc[0]['pct']) if not grouped.empty else 0.0,
    }


def build_all_gg_stats(df):
    valid_df = df[df['esito'].isin(['GOL', 'NO GOL'])].copy()
    if valid_df.empty:
        return {'total_all_gg_blocks': 0, 'latest_streak': 0,
                'blocks_table': pd.DataFrame(columns=['orario', 'GG', 'totale', 'all_gg_6su6'])}
    grouped = valid_df.groupby('orario').agg(
        totale=('esito', 'count'),
        GG=('esito', lambda x: (x == 'GOL').sum())
    ).reset_index().sort_values('orario', ascending=False)
    grouped['all_gg_6su6'] = (grouped['totale'] == 6) & (grouped['GG'] == 6)
    streak = 0
    for value in grouped['all_gg_6su6'].tolist():
        if value:
            streak += 1
        else:
            break
    return {
        'total_all_gg_blocks': int(grouped['all_gg_6su6'].sum()),
        'latest_streak': streak,
        'blocks_table': grouped[['orario', 'GG', 'totale', 'all_gg_6su6']]
    }


def build_forecast(df):
    valid_df = df[df['esito'].isin(['GOL', 'NO GOL'])].copy()
    if valid_df.empty:
        return {
            'rate_5': 0.0, 'rate_10': 0.0, 'weighted_rate': 0.0,
            'next_block_expected': 0.0, 'next_block_rounded': 0, 'next_3_blocks_expected': 0.0,
            'range_min': 0, 'range_max': 0,
            'details': pd.DataFrame(columns=['finestra', 'percentuale_GG'])
        }

    grouped = valid_df.groupby('orario').agg(
        totale=('esito', 'count'),
        GG=('esito', lambda x: (x == 'GOL').sum())
    ).reset_index().sort_values('orario', ascending=False)
    grouped = grouped[grouped['totale'] > 0].copy()
    grouped['rate'] = grouped['GG'] / grouped['totale']

    def mean_rate(n):
        subset = grouped.head(n)
        return float(subset['rate'].mean()) if not subset.empty else 0.0

    rate_5 = mean_rate(5)
    rate_10 = mean_rate(10)
    weighted_rate = max(0.0, min(1.0, (0.6 * rate_5) + (0.4 * rate_10)))
    next_block_expected = round(weighted_rate * 6, 2)
    next_block_rounded = int(round(next_block_expected))
    next_3_blocks_expected = round(next_block_expected * 3, 2)
    range_min = max(0, int(round(next_block_expected - 1)))
    range_max = min(6, int(round(next_block_expected + 1)))

    details = pd.DataFrame([
        {'finestra': 'Ultimi 5 blocchi', 'percentuale_GG': round(rate_5 * 100, 2)},
        {'finestra': 'Ultimi 10 blocchi', 'percentuale_GG': round(rate_10 * 100, 2)},
        {'finestra': 'Media pesata finale', 'percentuale_GG': round(weighted_rate * 100, 2)},
    ])
    return {
        'rate_5': rate_5, 'rate_10': rate_10, 'weighted_rate': weighted_rate,
        'next_block_expected': next_block_expected, 'next_block_rounded': next_block_rounded,
        'next_3_blocks_expected': next_3_blocks_expected,
        'range_min': range_min, 'range_max': range_max,
        'details': details
    }


def build_backtest(df):
    valid_df = df[df['esito'].isin(['GOL', 'NO GOL'])].copy()
    cols = ['orario', 'actual_GG', 'predicted_GG', 'error']
    empty = pd.DataFrame(columns=cols)
    if valid_df.empty:
        return {'mae_10': 0.0, 'mae_20': 0.0, 'bias': 0.0, 'table': empty}

    grouped = valid_df.groupby('orario').agg(
        totale=('esito', 'count'),
        GG=('esito', lambda x: (x == 'GOL').sum())
    ).reset_index().sort_values('orario', ascending=True)
    grouped = grouped[grouped['totale'] > 0].copy()
    grouped['rate'] = grouped['GG'] / grouped['totale']

    preds = []
    for i in range(1, len(grouped)):
        hist = grouped.iloc[:i]

        def mean_rate(n):
            sub = hist.tail(n)
            return float(sub['rate'].mean()) if not sub.empty else 0.0

        rate_5 = mean_rate(5)
        rate_10 = mean_rate(10)
        weighted_rate = max(0.0, min(1.0, (0.6 * rate_5) + (0.4 * rate_10)))
        predicted = round(weighted_rate * 6, 2)
        actual = int(grouped.iloc[i]['GG'])
        preds.append({
            'orario': grouped.iloc[i]['orario'],
            'actual_GG': actual,
            'predicted_GG': predicted,
            'error': round(predicted - actual, 2),
            'abs_error': round(abs(predicted - actual), 2),
        })

    if not preds:
        return {'mae_10': 0.0, 'mae_20': 0.0, 'bias': 0.0, 'table': empty}

    backtest_df = pd.DataFrame(preds).sort_values('orario', ascending=False)
    return {
        'mae_10': round(float(backtest_df.head(10)['abs_error'].mean()), 2),
        'mae_20': round(float(backtest_df.head(20)['abs_error'].mean()), 2),
        'bias': round(float(backtest_df['error'].mean()), 2),
        'table': backtest_df[['orario', 'actual_GG', 'predicted_GG', 'error']]
    }


def build_probabilities(df):
    forecast = build_forecast(df)
    p = forecast['weighted_rate']

    def binom_prob_at_least(k, n=6, p=0.0):
        return sum(comb(n, i) * (p ** i) * ((1 - p) ** (n - i)) for i in range(k, n + 1))

    p_ge4 = binom_prob_at_least(4, 6, p)
    p_ge5 = binom_prob_at_least(5, 6, p)
    p_eq6 = p ** 6

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
    if df.empty:
        return pd.DataFrame(columns=['giornata', 'partite', 'GG', 'NO_GOL', 'pct_gg'])
    valid_df = df[df['esito'].isin(['GOL', 'NO GOL'])].copy()
    if valid_df.empty:
        return pd.DataFrame(columns=['giornata', 'partite', 'GG', 'NO_GOL', 'pct_gg'])
    grouped = valid_df.groupby('giornata').agg(
        partite=('esito', 'count'),
        GG=('esito', lambda x: (x == 'GOL').sum()),
        NO_GOL=('esito', lambda x: (x == 'NO GOL').sum())
    ).reset_index().sort_values('giornata', ascending=False)
    grouped['pct_gg'] = ((grouped['GG'] / grouped['partite']) * 100).round(2)
    return grouped


# -------------------------
# Predict Top-N
# -------------------------
def team_recent_form(df, team_code, max_matchdays=10):
    valid_df = df[df['esito'].isin(['GOL', 'NO GOL'])].copy() if not df.empty else pd.DataFrame()
    if valid_df.empty:
        return pd.DataFrame(columns=['giornata', 'esito'])

    subset = valid_df[(valid_df['home_team'] == team_code) | (valid_df['away_team'] == team_code)].copy()
    if subset.empty:
        return pd.DataFrame(columns=['giornata', 'esito'])

    subset = subset.sort_values(['giornata', 'orario'], ascending=[False, False])
    unique_days = []
    for g in subset['giornata'].dropna().tolist():
        if g not in unique_days:
            unique_days.append(g)
    keep_days = unique_days[:max_matchdays]
    subset = subset[subset['giornata'].isin(keep_days)].copy()
    subset = subset.drop_duplicates(subset=['giornata', 'home_team', 'away_team', 'esito'])
    return subset[['giornata', 'esito']]


def rate_from_last_matchdays(team_df, n_days):
    if team_df.empty:
        return 0.0, 0
    ordered_days = []
    for g in team_df['giornata'].dropna().tolist():
        if g not in ordered_days:
            ordered_days.append(g)
    keep_days = ordered_days[:n_days]
    subset = team_df[team_df['giornata'].isin(keep_days)].copy()
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
    valid_df = df[df['esito'].isin(['GOL', 'NO GOL'])].copy() if not df.empty else pd.DataFrame()
    if valid_df.empty:
        return {'rate_5': 0.0, 'rate_10': 0.0, 'trend_score': 0.0}

    grouped = valid_df.groupby('orario').agg(
        totale=('esito', 'count'),
        GG=('esito', lambda x: (x == 'GOL').sum())
    ).reset_index().sort_values('orario', ascending=False)
    grouped = grouped[grouped['totale'] > 0].copy()
    grouped['rate'] = grouped['GG'] / grouped['totale']

    def mean_rate(n):
        subset = grouped.head(n)
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

df = pd.DataFrame(matches) if matches else pd.DataFrame(
    columns=['orario', 'timestamp', 'esito', 'giornata', 'home_team', 'away_team']
)

if not df.empty:
    df = df.sort_values(['orario', 'timestamp'], ascending=False)
    st.markdown(f'**Ultimo aggiornamento (locale):** {last_update}')
    st.caption(
        f"Giorno dati attivo: {st.session_state.get('active_data_day')} | "
        f"Data API usata: {api_day_used}"
    )

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
    blocks_df = build_blocks(df)

    st.markdown('#### GG per blocco con etichette Giornata + orario')
    if not blocks_df.empty:
        fig_gg = px.bar(
            blocks_df,
            x='label_x',
            y='GOL',
            color='fascia_colore',
            color_discrete_map={
                'GG <=2': '#8ec5ff',
                'GG =3': '#1976d2',
                'GG >3': '#f6b0b7'
            }
        )
        fig_gg.update_layout(
            xaxis_title='Giornata + orario',
            yaxis_title='GG',
            yaxis=dict(range=[0, 6], dtick=1),
            legend_title_text='',
            height=520
        )
        st.plotly_chart(fig_gg, use_container_width=True)
    else:
        st.info('Nessun dato disponibile.')

    st.markdown('#### Percentuale GG per blocco')
    if not blocks_df.empty:
        fig_pct = px.line(blocks_df, x='label_x', y='% sul totale', markers=True)
        fig_pct.update_layout(
            xaxis_title='Giornata + orario',
            yaxis_title='% GG',
            yaxis=dict(range=[0, 100], dtick=10),
            height=420
        )
        st.plotly_chart(fig_pct, use_container_width=True)
    else:
        st.info('Nessun dato disponibile.')

    giornata_summary = build_giornata_summary(df)
    gcol3, gcol4 = st.columns(2)
    with gcol3:
        st.markdown('#### GG per giornata')
        if not giornata_summary.empty:
            st.bar_chart(giornata_summary.set_index('giornata')[['GG']], height=280)
        else:
            st.info('Nessun dato giornata disponibile.')
    with gcol4:
        st.markdown('#### % GG per giornata')
        if not giornata_summary.empty:
            st.line_chart(giornata_summary.set_index('giornata')[['pct_gg']], height=280)
        else:
            st.info('Nessun dato giornata disponibile.')

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

    st.subheader('Partite giorno per giorno')
    storico_df = df[['orario', 'giornata', 'codice_avvenimento', 'descrizione_avventimento', 'esito']].copy()
    storico_df = storico_df.sort_values(
        ['giornata', 'orario', 'codice_avvenimento'],
        ascending=[False, True, True]
    )
    giornate = storico_df['giornata'].dropna().unique().tolist()

    if giornate:
        for g in giornate:
            blocco_g = storico_df[storico_df['giornata'] == g].copy()
            gg_count = int((blocco_g['esito'] == 'GOL').sum())
            ng_count = int((blocco_g['esito'] == 'NO GOL').sum())
            with st.expander(
                f'Giornata {g} · Partite {len(blocco_g)} · GG {gg_count} · NG {ng_count}',
                expanded=False
            ):
                st.dataframe(
                    blocco_g[[
                        'orario', 'giornata', 'codice_avvenimento',
                        'descrizione_avventimento', 'esito'
                    ]],
                    use_container_width=True,
                    hide_index=True
                )
    else:
        st.info('Nessuna giornata disponibile.')

    st.subheader('Blocchi orari')
    st.dataframe(blocks_df[['giornata', 'orario', 'GOL', '% sul totale']], use_container_width=True, hide_index=True)

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
        st.write('Forecast generale prossimo blocco: 60% ultimi 5 blocchi + 40% ultimi 10 blocchi.')
        st.write('Trend squadra = 60% rate ultime 5 giornate + 40% rate ultime 10 giornate.')
        st.write('Trend match = media trend squadra casa e trasferta.')
        st.write('Score finale = 50% trend match + 25% trend globale + 20% probabilità mercato + 5% bonus classifica.')
        st.write('GG attesi totali = somma degli score finali delle partite inserite.')
        st.write('Predict finale = Top-N del ranking, dove N è il numero arrotondato di GG attesi totali.')
        st.write(
            "Gestione mezzanotte: prima dell'1:00 l’API viene interrogata sul giorno operativo precedente, "
            "così vedi tutti i blocchi fino all’ultima ora."
        )
        st.write(
            "Reset giornaliero: dopo l'1:00, se il giorno è cambiato, lo storico viene azzerato automaticamente "
            "alla prima pressione di 'Aggiorna risultati'."
        )
