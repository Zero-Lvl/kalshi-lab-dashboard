"""Uncalibrated daily-high Gaussian estimates, never a claim of trading edge.

Forecast grids are proxies, not the official The Weather Company settlement.
The Open-Meteo response has no model initialization timestamp: generationtime_ms
is execution duration, NOT forecast issuance. Record that limitation explicitly.
"""
from datetime import datetime
import math
import re
from statistics import NormalDist
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

MODEL_VERSION = "weather-gaussian-v1-unvalidated"
SERIES = {
    "KXHIGHNY": {
        "city": "New York City", "station": "Central Park", "station_id": "KNYC",
        "station_marker": "CLINYC", "latitude": 40.7829, "longitude": -73.9654,
        "timezone": "America/New_York",
        "structured_city_id": "afac07e5-3006-4027-a9ac-f417ce55e0db",
    },
    'KXHIGHCHI': {'city':'Chicago','station':'Midway Airport','station_id':'KMDW','station_marker':'CLIMDW',
                  'latitude':41.78417,'longitude':-87.75528,'timezone':'America/Chicago',
                  'structured_city_id':'fdc0eba5-7557-46d8-ab9a-d6af5c53ccf9'},
    'KXHIGHDEN': {'city':'Denver','station':'Denver International Airport','station_id':'KDEN','station_marker':'CLIDEN',
                  'latitude':39.84658,'longitude':-104.65622,'timezone':'America/Denver',
                  'structured_city_id':'5eed802e-f291-498c-bb6b-9fbd6377c0d7'},
    'KXHIGHLAX': {'city':'Los Angeles','station':'Los Angeles International Airport','station_id':'KLAX','station_marker':'CLILAX',
                  'latitude':33.93806,'longitude':-118.38889,'timezone':'America/Los_Angeles',
                  'structured_city_id':'165dc78f-afb7-4201-8e8c-982fade5aa23'},
    'KXHIGHAUS': {'city':'Austin','station':'Austin-Bergstrom International Airport','station_id':'KAUS','station_marker':'CLIAUS',
                  'latitude':30.18304,'longitude':-97.67987,'timezone':'America/Chicago',
                  'structured_city_id':'36f8343a-6a94-4955-a3d4-2734d9cf13c2'},
}
WARNING = ("Unvalidated probability, not a known edge. Open-Meteo and NWS forecast grids "
           "are not official The Weather Company settlement observations; station/grid "
           "and rounding differences remain. Open-Meteo model issuance time unavailable; "
           "retrieval time is not model generation time. Same-day pre-09:00 forecasts "
           "do not incorporate an observed daily maximum.")


def build_weather_signals(markets: list, fetch_json: callable, now: datetime) -> tuple[list, list]:
    """Return estimates and explicit abstention errors using an injected JSON fetcher."""
    signals, errors, cache = [], [], {}
    for market in markets:
        try:
            event = market["event_ticker"]
            cfg = SERIES[event.split("-")[0]]
            if now.tzinfo is None:
                raise ValueError('timezone required')
            day = datetime.strptime(event.split("-")[1], "%y%b%d").date()
            target = day.isoformat()
            local = now.astimezone(ZoneInfo(cfg['timezone']))
            if not 0 <= (day-local.date()).days <= 2 or (day == local.date() and local.hour >= 9):
                raise ValueError('abstain: past/out-of-horizon date or same-day forecast after 09:00 local')
            if market.get('status') not in ('active', 'open') or market.get('market_type') != 'binary':
                raise ValueError('not an active binary contract')
            if not market['ticker'].startswith(event+'-'):
                raise ValueError('ticker/event mismatch')
            if datetime.fromisoformat(market['close_time'].replace('Z','+00:00')) <= now:
                raise ValueError('market closed')
            if market.get('custom_strike', {}).get('structured_city_id') != cfg['structured_city_id']:
                raise ValueError('unverified settlement station')
            kind = market['strike_type']
            bounds = [market[k] for k in ('floor_strike','cap_strike') if k in market]
            if not bounds or any(not isinstance(x,(int,float)) or not math.isfinite(x) or int(x)!=x for x in bounds):
                raise ValueError('noninteger/invalid temperature bounds')
            if kind == 'between':
                if market['floor_strike'] > market['cap_strike']:
                    raise ValueError('reversed bounds')
                condition = f"between {market['floor_strike']:g}-{market['cap_strike']:g}°"
            elif kind == 'less':
                condition = f"less than {market['cap_strike']:g}°"
            elif kind == 'greater':
                condition = f"greater than {market['floor_strike']:g}°"
            else:
                raise ValueError('unsupported strike type')
            date_text = f"{day.strftime('%b')} {day.day}, {day.year}"
            expected = (f"If the maximum temperature recorded at {cfg['city']} ({cfg['station_marker']}) "
                        f"for {date_text}, is {condition} fahrenheit according to The Weather Company, "
                        "then the market resolves to Yes.")
            if ' '.join(market.get('rules_primary','').split()) != expected:
                raise ValueError('unrecognized or changed settlement rules')
            om_url = "https://api.open-meteo.com/v1/forecast?" + urlencode({
                "latitude": cfg["latitude"], "longitude": cfg["longitude"],
                "daily": "temperature_2m_max", "temperature_unit": "fahrenheit",
                "timezone": cfg["timezone"], "forecast_days": 3,
            })
            nws_url = "https://forecast.weather.gov/MapClick.php?" + urlencode({
                "lat": cfg["latitude"], "lon": cfg["longitude"], "FcstType": "json",
            })
            for url in (om_url,nws_url):
                if url not in cache:
                    cache[url] = fetch_json(url)
            o, n = cache[om_url], cache[nws_url]
            if o.get('daily_units',{}).get('temperature_2m_max') != '°F' or o.get('timezone') != cfg['timezone']:
                raise ValueError('forecast unit/timezone mismatch')
            issued = datetime.fromisoformat(n['creationDate'].replace('Z','+00:00'))
            if issued.tzinfo is None or not -900 <= (now-issued).total_seconds() <= 86400:
                raise ValueError('stale or future NWS issuance')
            for response, latkey, lonkey in ((o,'latitude','longitude'),(n['location'],'latitude','longitude')):
                lat, lon = float(response[latkey]), float(response[lonkey])
                if not math.isfinite(lat) or not math.isfinite(lon) or abs(lat-cfg['latitude']) > .2 or abs(lon-cfg['longitude']) > .2:
                    raise ValueError('forecast grid/station mismatch')
            oi = o["daily"]["time"].index(target)
            om_mean = float(o["daily"]["temperature_2m_max"][oi])
            valid_times = [datetime.fromisoformat(stamp.replace('Z','+00:00')) for stamp in n['time']['startValidTime']]
            if any(stamp.tzinfo is None for stamp in valid_times):
                raise ValueError('timezone required for forecast valid time')
            ni = next(i for i, (stamp, label) in enumerate(zip(
                valid_times, n["time"]["tempLabel"]))
                if label == "High" and stamp.astimezone(
                    ZoneInfo(cfg["timezone"])).date().isoformat() == target)
            nws_mean = float(n["data"]["temperature"][ni])
            if any(not math.isfinite(x) or not -100 < x < 150 for x in (om_mean,nws_mean)) or abs(om_mean-nws_mean) > 8:
                raise ValueError('invalid or divergent forecast temperatures')
            mean = (om_mean + nws_mean) / 2
            sigma = max(3.0, abs(om_mean - nws_mean) * 1.5)
            distribution = NormalDist(mean, sigma)
            if market["strike_type"] == "less":
                probability = distribution.cdf(market["cap_strike"] - 0.5)
            elif market["strike_type"] == "between":
                probability = (distribution.cdf(market["cap_strike"] + 0.5)
                               - distribution.cdf(market["floor_strike"] - 0.5))
            elif market["strike_type"] == "greater":
                probability = 1 - distribution.cdf(market["floor_strike"] + 0.5)
            else:
                raise ValueError('unsupported strike type')
            observed = now.isoformat()
            sources = [
                {"name": "Open-Meteo", "url": om_url, "observed_at": observed,
                 "forecast_date": target, "mean_f": om_mean, "issued_at": None,
                 "generationtime_ms": o.get("generationtime_ms"),
                 "grid_latitude": o.get("latitude"), "grid_longitude": o.get("longitude")},
                {"name": "NWS MapClick", "url": nws_url, "observed_at": observed,
                 "forecast_date": target, "mean_f": nws_mean,
                 "issued_at": n["creationDate"], "valid_time": n["time"]["startValidTime"][ni]},
            ]
            signals.append({
                "ticker": market["ticker"], "event_ticker": event, "p_yes": probability,
                "model_version": MODEL_VERSION, "forecast_mean_f": mean, "sigma_f": sigma,
                "source_url": om_url, "source_observed_at": observed, "warning": WARNING,
                "forecast_date": target, "sources": sources,
            })
        except Exception as exc:
            errors.append({"ticker": market.get("ticker"), "error": str(exc) or type(exc).__name__})
    return signals, errors
