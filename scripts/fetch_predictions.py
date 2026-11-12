import os
import json
import requests
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import time
import shutil

API_KEY = os.environ.get('API_FOOTBALL_KEY')
API_URL = "https://v3.football.api-sports.io"
MIN_API_REQUEST_INTERVAL = 6.2  # Free plan allows 10 requests per minute.
_last_api_request_at = 0.0


class RateLimitError(RuntimeError):
    pass


def pace_api_request():
    global _last_api_request_at
    elapsed = time.monotonic() - _last_api_request_at
    if elapsed < MIN_API_REQUEST_INTERVAL:
        time.sleep(MIN_API_REQUEST_INTERVAL - elapsed)
    _last_api_request_at = time.monotonic()

def fetch_api(endpoint, params):
    if not API_KEY:
        raise RuntimeError("API_FOOTBALL_KEY is missing. Set it in the environment or GitHub Actions secrets.")

    headers = {'x-apisports-key': API_KEY}
    for attempt in range(2):
        pace_api_request()
        try:
            response = requests.get(
                f"{API_URL}/{endpoint}",
                headers=headers,
                params=params,
                timeout=(10, 30)
            )
            if response.status_code == 429:
                if attempt == 0:
                    try:
                        retry_after = float(response.headers.get('Retry-After', 60))
                    except (TypeError, ValueError):
                        retry_after = 60
                    time.sleep(max(retry_after, 6.2))
                    continue
                raise RateLimitError(
                    f"API-Football rate limit reached for {endpoint}; try again later."
                )
            response.raise_for_status()
            data = response.json()
            break
        except requests.RequestException as exc:
            raise RuntimeError(f"API-Football request failed for {endpoint}: {exc}") from exc
        except ValueError as exc:
            raise RuntimeError(f"API-Football returned invalid JSON for {endpoint}.") from exc

    errors = data.get('errors')
    if errors and errors not in ({}, []):
        raise RuntimeError(f"API-Football rejected {endpoint}: {errors}")

    results = data.get('response')
    if not isinstance(results, list):
        raise RuntimeError(f"API-Football returned an unexpected response for {endpoint}.")
    return results

def get_fixtures_for_date(date_str):
    # Fetch the day's full fixture list. Prediction lookups below are separately capped.
    return fetch_api("fixtures", {"date": date_str})

def get_kickoff_datetime(fixture):
    try:
        kickoff = datetime.fromisoformat(fixture['fixture']['date'].replace('Z', '+00:00'))
        return kickoff.astimezone(timezone.utc)
    except (KeyError, TypeError, ValueError):
        return None

def get_prediction(fixture_id):
    preds = fetch_api("predictions", {"fixture": fixture_id})
    if preds and len(preds) > 0: return preds[0]
    return None

def get_odds(fixture_id):
    odds_resp = fetch_api("odds", {"fixture": fixture_id})
    if odds_resp and len(odds_resp) > 0:
        bookmakers = odds_resp[0].get('bookmakers', [])
        if not bookmakers: return None
        bookmaker = bookmakers[0]
        bets = bookmaker.get('bets', [])
        for bet in bets:
            if bet['name'] == 'Match Winner' or bet['id'] == 1:
                vals = bet.get('values', [])
                home_odd = next((v['odd'] for v in vals if v['value'] == 'Home'), 'N/A')
                draw_odd = next((v['odd'] for v in vals if v['value'] == 'Draw'), 'N/A')
                away_odd = next((v['odd'] for v in vals if v['value'] == 'Away'), 'N/A')
                return {"home": home_odd, "draw": draw_odd, "away": away_odd}
    return None

def generate_short_tip(win_percent):
    try:
        h = float(str(win_percent.get('home', '33%')).replace('%',''))
        d = float(str(win_percent.get('draw', '33%')).replace('%',''))
        a = float(str(win_percent.get('away', '33%')).replace('%',''))
        
        if h >= 50: return "1"
        if a >= 50: return "2"
        if h > 40 and d > 30: return "1X"
        if a > 40 and d > 30: return "X2"
        if d >= 40: return "X"
        if h > a: return "1X"
        return "X2"
    except:
        return "1X2"

def process_upcoming_fixtures(today, filename, data_dir):
    today_str = today.strftime('%Y-%m-%d')
    now_utc = datetime.now(timezone.utc)
    fixtures = []

    # Search today plus the next three days so late-day runs still find upcoming games.
    for day_offset in range(4):
        date_str = (today + timedelta(days=day_offset)).strftime('%Y-%m-%d')
        for fixture in get_fixtures_for_date(date_str):
            kickoff = get_kickoff_datetime(fixture)
            status = fixture.get('fixture', {}).get('status', {}).get('short')
            if status == 'NS' and kickoff and kickoff > now_utc:
                fixtures.append(fixture)

    fixtures.sort(key=lambda fixture: get_kickoff_datetime(fixture))
    predictions = []
    odds_rate_limited = False
    prediction_rate_limit_message = None
    
    prediction_attempts = 0
    for f in fixtures:
        if len(predictions) >= 5:
            break
        if prediction_attempts >= 15:
            break  # Cap prediction lookups to control API usage.
        
        fix_id = f['fixture']['id']
        print(f"Fetching prediction & odds for {fix_id} at {f['fixture']['date']}...")
        
        try:
            pred_data = get_prediction(fix_id)
        except RateLimitError as exc:
            prediction_rate_limit_message = str(exc)
            print(prediction_rate_limit_message)
            break
        prediction_attempts += 1
        
        if not pred_data: continue
            
        win_percent = pred_data['predictions']['percent']
        advice = pred_data['predictions']['advice']
        
        home_goals_pred = pred_data['predictions']['goals']['home']
        away_goals_pred = pred_data['predictions']['goals']['away']
        home_g = float(str(home_goals_pred).replace('-','0') if home_goals_pred else 0)
        away_g = float(str(away_goals_pred).replace('-','0') if away_goals_pred else 0)
        
        odds_data = None
        if not odds_rate_limited:
            try:
                odds_data = get_odds(fix_id)
            except RateLimitError as exc:
                odds_rate_limited = True
                print(f"Skipping further odds lookups: {exc}")

        status_short = f['fixture']['status']['short']
        is_finished = status_short in ['FT', 'AET', 'PEN']
        home_goals = f['goals']['home']
        away_goals = f['goals']['away']
        
        correct_1x2 = False
        if is_finished and home_goals is not None and away_goals is not None:
            actual_1x2 = "1" if home_goals > away_goals else "2" if away_goals > home_goals else "X"
            short_tip = generate_short_tip(win_percent)
            correct_1x2 = actual_1x2 in short_tip

        predictions.append({
            "fixture_id": fix_id,
            "date": f['fixture']['date'],
            "league": f['league']['name'],
            "home_team": f['teams']['home']['name'],
            "home_logo": f['teams']['home']['logo'],
            "away_team": f['teams']['away']['name'],
            "away_logo": f['teams']['away']['logo'],
            "prediction_1x2": pred_data['predictions']['winner']['comment'],
            "short_tip": generate_short_tip(win_percent),
            "percent_home": win_percent.get('home', '33%'),
            "percent_draw": win_percent.get('draw', '33%'),
            "percent_away": win_percent.get('away', '33%'),
            "prediction_gg": "GG" if home_goals_pred and away_goals_pred and str(home_goals_pred) != "0" and str(away_goals_pred) != "0" else "NG",
            "prediction_ou": "Over 2.5" if home_g + away_g > 2.5 else "Under 2.5",
            "advice": advice,
            "odds": odds_data,
            "status": "Finished" if is_finished else "Pending" if status_short == 'NS' else "In Play",
            "home_goals": home_goals if is_finished else None,
            "away_goals": away_goals if is_finished else None,
            "correct_1x2": correct_1x2
        })
        
    output_file = os.path.join(data_dir, filename)
    result = {"date": today_str, "matches": predictions}
    if prediction_rate_limit_message:
        result["message"] = prediction_rate_limit_message
    if not predictions:
        if prediction_rate_limit_message:
            result["message"] = prediction_rate_limit_message
        elif not fixtures:
            result["message"] = "No upcoming fixtures found for today and the next 3 days."
        else:
            result["message"] = (
                f"No prediction data was returned for the {prediction_attempts} fixtures checked. "
                "Try again later or check the API-Football plan and quota."
            )

    with open(output_file, 'w') as f:
        json.dump(result, f, indent=4)
    print(f"Saved {len(predictions)} predictions to {output_file}")

def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    data_dir = os.path.join(script_dir, '..', 'data')
    os.makedirs(data_dir, exist_ok=True)
    
    today = datetime.now(ZoneInfo('Africa/Casablanca'))
    
    # Generate upcoming predictions for today and the next three days.
    process_upcoming_fixtures(today, 'predictions_today.json', data_dir)
    
    # We also keep predictions.json as an alias for today to not break backward compatibility
    shutil.copyfile(os.path.join(data_dir, 'predictions_today.json'), os.path.join(data_dir, 'predictions.json'))

if __name__ == "__main__":
    main()
