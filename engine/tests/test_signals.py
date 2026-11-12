"""Deterministic forecast fixtures; live verification is separate, not a test dependency."""
import copy
from datetime import datetime, timezone
import importlib
from statistics import NormalDist
import unittest
from urllib.parse import parse_qs, urlsplit

try:
    weather = importlib.import_module("signals")
except ModuleNotFoundError:
    weather = None

NOW = datetime(2026, 11, 11, 12, tzinfo=timezone.utc)


def market(kind="greater", floor=66, cap=None):
    condition = {"greater": f"greater than {floor}°", "less": f"less than {cap}°",
                 "between": f"between {floor}-{cap}°"}[kind]
    result = {
        "ticker": "KXHIGHNY-26NOV11-T66", "event_ticker": "KXHIGHNY-26NOV11",
        "status": "active", "market_type": "binary", "strike_type": kind,
        "open_time": "2026-11-10T15:00:00Z", "close_time": "2026-11-12T06:00:00Z",
        "custom_strike": {"City": "New York City", "structured_city_id": "afac07e5-3006-4027-a9ac-f417ce55e0db"},
        "rules_primary": f"If the maximum temperature recorded at New York City (CLINYC) for Nov 11, 2026, is {condition} fahrenheit according to The Weather Company, then the market resolves to Yes.",
    }
    if floor is not None:
        result["floor_strike"] = floor
    if cap is not None:
        result["cap_strike"] = cap
    return result


def om(mean=62.5):
    return {"latitude": 40.78858, "longitude": -73.9661,
            "timezone": "America/New_York", "utc_offset_seconds": -18000,
            "generationtime_ms": 0.1,
            "daily_units": {"time": "iso8601", "temperature_2m_max": "°F"},
            "daily": {"time": ["2026-11-11", "2026-11-12", "2026-11-13"],
                      "temperature_2m_max": [mean, 59.0, 47.0]}}


def nws(mean=63):
    return {"creationDate": "2026-11-11T01:20:28-05:00",
            "location": {"latitude": "40.78", "longitude": "-73.97"},
            "time": {"startValidTime": ["2026-11-11T07:00:00-05:00", "2026-11-11T18:00:00-05:00", "2026-11-12T06:00:00-05:00"],
                     "tempLabel": ["High", "Low", "High"]},
            "data": {"temperature": [str(mean), "51", "59"]}}


class WeatherSignalsTests(unittest.TestCase):
    def build(self, m=None, o=None, n=None, now=NOW):
        self.calls = []
        def fetch(url):
            self.calls.append(url)
            return copy.deepcopy(o if "open-meteo.com" in url else n)
        return weather.build_weather_signals([m if m is not None else market()], fetch, now)

    def test_timezone_naive_forecast_valid_time_abstains(self):
        n=nws()
        n['time']['startValidTime']=[stamp[:19] for stamp in n['time']['startValidTime']]
        result,errors=self.build(o=om(),n=n)
        self.assertEqual(result,[])
        self.assertTrue(errors)

    def test_nonfinite_station_coordinates_are_rejected(self):
        for bad in ('NaN','Infinity','-Infinity'):
            for provider in ('om','nws'):
                with self.subTest(value=bad,provider=provider):
                    o,n=om(),nws()
                    location=o if provider=='om' else n['location']
                    location.update(latitude=bad,longitude=bad)
                    result,errors=self.build(o=o,n=n)
                    self.assertEqual(result,[])
                    self.assertTrue(errors)

    def test_new_airport_stations_use_verified_coordinates_and_local_cutoff(self):
        configs = {
            'KXHIGHCHI': ('Chicago','CLIMDW','fdc0eba5-7557-46d8-ab9a-d6af5c53ccf9',41.78417,-87.75528,'America/Chicago'),
            'KXHIGHDEN': ('Denver','CLIDEN','5eed802e-f291-498c-bb6b-9fbd6377c0d7',39.84658,-104.65622,'America/Denver'),
            'KXHIGHLAX': ('Los Angeles','CLILAX','165dc78f-afb7-4201-8e8c-982fade5aa23',33.93806,-118.38889,'America/Los_Angeles'),
            'KXHIGHAUS': ('Austin','CLIAUS','36f8343a-6a94-4955-a3d4-2734d9cf13c2',30.18304,-97.67987,'America/Chicago'),
        }
        for series,(city,marker,city_id,lat,lon,tz) in configs.items():
            with self.subTest(series=series):
                m=market()
                m['event_ticker']=series+'-26NOV11';m['ticker']=m['event_ticker']+'-T66'
                m['rules_primary']=m['rules_primary'].replace('New York City (CLINYC)',city+' ('+marker+')')
                m['custom_strike']={'City':city,'structured_city_id':city_id}
                o=om();o.update(latitude=lat,longitude=lon,timezone=tz)
                n=nws();n['location'].update(latitude=str(lat),longitude=str(lon))
                s,err=self.build(m,o,n,NOW.replace(hour=13))
                self.assertEqual(err,[])
                self.assertEqual(len(s),1)
                self.assertEqual(weather.SERIES[series]['latitude'],lat)
                self.assertEqual(weather.SERIES[series]['longitude'],lon)

    def test_untrusted_contract_or_forecast_abstains_instead_of_pricing(self):
        cases = []
        for changes in [
            {'rules_primary': 'Some other settlement rules'},
            {'custom_strike': {'City': 'Other City'}},
            {'event_ticker': 'KXHIGHNY-26NOV10'},
            {'cap_strike': 60, 'floor_strike': 65, 'strike_type': 'between'},
            {'status': 'closed'},
        ]:
            cases.append((dict(market(), **changes), om(), nws(), NOW))
        for changes in [
            {'daily_units': {'temperature_2m_max': '°C'}},
            {'daily': {'time': ['2026-11-11'], 'temperature_2m_max': [float('nan')]}},
            {'timezone': 'UTC'},
        ]:
            cases.append((market(), dict(om(), **changes), nws(), NOW))
        cases.append((market(), om(), dict(nws(), creationDate='2026-11-08T01:00:00-05:00'), NOW))
        cases.append((market(), om(), nws(80), NOW))
        cases.append((market(), om(), nws(), NOW.replace(hour=15)))
        for m, o, n, when in cases:
            with self.subTest(m=m, o=o, n=n, when=when):
                values, errors = self.build(m, o, n, when)
                self.assertEqual(values, [])
                self.assertTrue(errors)

    def test_between_is_inclusive_integer_range_not_greater_than_floor(self):
        signals, errors = self.build(m=market('between', 61, 62), o=om(), n=nws())
        self.assertEqual(errors, [])
        d = NormalDist(62.75, 3)
        self.assertAlmostEqual(signals[0]['p_yes'], d.cdf(62.5) - d.cdf(60.5))

    def test_less_uses_exclusive_cap_rounding_boundary(self):
        signals, errors = self.build(m=market("less", floor=None, cap=63), o=om(), n=nws())
        self.assertEqual(errors, [])
        self.assertAlmostEqual(signals[0]["p_yes"], NormalDist(62.75, 3).cdf(62.5))

    def test_live_shaped_greater_signal_has_probability_and_provenance(self):
        self.assertIsNotNone(weather, "signals module must exist")
        signals, errors = self.build(o=om(), n=nws())
        self.assertEqual(errors, [])
        self.assertEqual(len(signals), 1)
        s = signals[0]
        self.assertEqual(s["ticker"], market()["ticker"])
        self.assertEqual(s["event_ticker"], "KXHIGHNY-26NOV11")
        self.assertEqual(s["forecast_mean_f"], 62.75)
        self.assertEqual(s["sigma_f"], 3)
        self.assertAlmostEqual(s["p_yes"], 1 - NormalDist(62.75, 3).cdf(66.5))
        self.assertEqual(s["model_version"], "weather-gaussian-v1-unvalidated")
        self.assertEqual(s["forecast_date"], "2026-11-11")
        self.assertEqual(s["source_observed_at"], NOW.isoformat())
        self.assertEqual(len(s["sources"]), 2)
        self.assertIn("The Weather Company", s["warning"])
        self.assertIn("not a known edge", s["warning"])
        for url in self.calls:
            query = parse_qs(urlsplit(url).query)
            self.assertEqual(float(query.get("latitude", query.get("lat"))[0]), 40.7829)
            self.assertEqual(float(query.get("longitude", query.get("lon"))[0]), -73.9654)


if __name__ == "__main__":
    unittest.main()
