"""Read-only public-market scanner and conservative, local paper ledger.

No authenticated endpoints, orders, or real-money execution exist in this module.
All dollar amounts in the ledger are decimal strings, never binary floats.
"""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path
import json
import sqlite3
from scanner import scan

FEE_MODEL_VERSION = 'assumed_quadratic_0.07_ceilcent_v1'

ROOT = Path(__file__).resolve().parent
CENT = Decimal('.01')
INITIAL_CASH = Decimal('1000.00')
TRADE_CAP = Decimal('20.00')
OPEN_CAP = Decimal('100.00')
RESERVE = Decimal('.08')
MIN_EDGE = Decimal('.03')
EXPLORATION_CAP = Decimal('2.00')
EXPLORATION_CATEGORY_CAP = Decimal('10.00')
CATEGORY_CAP = Decimal('40.00')


def decimal(value):
    value = Decimal(str(value))
    if not value.is_finite():
        raise ValueError('nonfinite number')
    return value


def timestamp(value):
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('timezone required')
    return result.astimezone(timezone.utc)


def trading_fee(qty, price, multiplier):
    return (Decimal('.07') * multiplier * qty * price * (1-price)).quantize(CENT, rounding=ROUND_CEILING)


def evaluate_candidate(market, signal, series, now, fetched_at, budget=TRADE_CAP):
    """Return executable best side, or None; missing data never implies liquidity."""
    try:
        if signal['ticker'] != market['ticker'] or signal['event_ticker'] != market['event_ticker']:
            return None
        if market.get('status') not in ('active', 'open') or timestamp(market['close_time']) <= now:
            return None
        age = (now - timestamp(fetched_at)).total_seconds()
        source_age = (now - timestamp(signal['source_observed_at'])).total_seconds()
        if not 0 <= age <= 120 or not 0 <= source_age <= 120:
            return None
        if series.get('fee_type') != 'quadratic':
            return None
        multiplier = decimal(series['fee_multiplier'])
        if multiplier < 0:
            return None
        p_yes = decimal(signal['p_yes'])
        if not 0 <= p_yes <= 1 or not signal.get('model_version'):
            return None
        ask = decimal(market['yes_ask_dollars'])
        bid = decimal(market['yes_bid_dollars'])
        noask = decimal(market['no_ask_dollars'])
        if not 0 <= bid <= ask <= 1 or ask-bid > Decimal('.10') or noask != 1-bid:
            return None
        if decimal(market.get('volume_24h_fp', market.get('volume_24h'))) < 100:
            return None
        exploratory = signal.get('entry_tier') == 'exploratory'
        if exploratory:
            if signal.get('category') not in ('Technology','Politics','Economics') or not signal.get('strategy_id') or not signal.get('decision_reason'):
                return None
            if p_yes != (ask+bid)/2 or ask-bid > Decimal('.04') or decimal(market.get('volume_24h_fp','0')) < 1000:
                return None
            favorite = max(p_yes,1-p_yes)
            if not Decimal('.60') <= favorite <= Decimal('.85') or (timestamp(market['close_time'])-now).total_seconds() > 45*86400:
                return None
            budget = min(decimal(budget),EXPLORATION_CAP)
        candidates = []
        for side, quote, prob, size_key in (
            ('yes', ask, p_yes, 'yes_ask_size_fp'),
            ('no', noask, 1-p_yes, 'yes_bid_size_fp')):
            if exploratory and side != ('yes' if p_yes >= Decimal('.5') else 'no'):
                continue
            if market.get(size_key) is None:
                continue
            size = int(decimal(market[size_key]).to_integral_value(rounding=ROUND_FLOOR))
            price = quote + CENT  # explicit conservative paper slippage
            if not 0 < price < 1 or size <= 0:
                continue
            qty = min(size, int((min(decimal(budget), TRADE_CAP)/price).to_integral_value(rounding=ROUND_FLOOR)))
            while qty > 0:
                fee = trading_fee(qty, price, multiplier)
                cost = price*qty+fee
                if cost <= min(decimal(budget), TRADE_CAP):
                    break
                qty -= 1
            if qty <= 0:
                continue
            margin = prob-price-fee/qty-RESERVE
            if not exploratory and margin < MIN_EDGE:
                continue
            candidates.append({'ticker': market['ticker'], 'event_ticker': market['event_ticker'],
                'side': side, 'qty': qty, 'entry_price': price, 'fee': fee, 'cost': cost,
                'p_yes': p_yes, 'baseline_p_yes': (ask+bid)/2,
                'model_version': signal['model_version'],
                'signal': dict(signal, market_title=market.get('title',''), rules_primary=market.get('rules_primary',''),
                               fee_multiplier=str(multiplier)),
                'net_margin': margin, 'fee_multiplier': multiplier,
                'potential_net_win': Decimal(qty)-cost, 'potential_net_loss': -cost})
        return max(candidates, key=lambda c: c['net_margin']) if candidates else None
    except (KeyError, ValueError, TypeError, InvalidOperation, OverflowError):
        return None


def dumps(value):
    return json.dumps(value, default=lambda v: str(v) if isinstance(v, (Decimal, Path)) else v.isoformat(),
                      sort_keys=True, allow_nan=False)


class Ledger:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY, ticker TEXT NOT NULL UNIQUE,
                event_ticker TEXT NOT NULL UNIQUE, side TEXT NOT NULL CHECK(side IN ('yes','no')),
                qty INTEGER NOT NULL CHECK(qty>0), entry_price TEXT NOT NULL,
                fee TEXT NOT NULL, cost TEXT NOT NULL, p_yes TEXT NOT NULL,
                baseline_p_yes TEXT NOT NULL, model_version TEXT NOT NULL,
                fee_model_version TEXT NOT NULL, signal_json TEXT NOT NULL,
                opened_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
                result TEXT, payout TEXT, net TEXT, settled_at TEXT);
            CREATE TRIGGER IF NOT EXISTS immutable_entry BEFORE UPDATE OF
                ticker,event_ticker,side,qty,entry_price,fee,cost,p_yes,baseline_p_yes,
                model_version,fee_model_version,signal_json,opened_at ON positions
                BEGIN SELECT RAISE(ABORT,'immutable paper entry'); END;
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY, run_id INTEGER, ticker TEXT NOT NULL,
                observed_at TEXT NOT NULL, p_yes TEXT NOT NULL, baseline_p_yes TEXT,
                model_version TEXT NOT NULL, signal_json TEXT NOT NULL);
            CREATE TRIGGER IF NOT EXISTS immutable_signal BEFORE UPDATE ON signals
                BEGIN SELECT RAISE(ABORT,'immutable forecast'); END;
            CREATE TRIGGER IF NOT EXISTS no_delete_signal BEFORE DELETE ON signals
                BEGIN SELECT RAISE(ABORT,'immutable forecast'); END;
            CREATE INDEX IF NOT EXISTS signals_ticker ON signals(ticker,id);
            CREATE TABLE IF NOT EXISTS settlements (
                ticker TEXT PRIMARY KEY, result TEXT NOT NULL CHECK(result IN ('yes','no')),
                observed_at TEXT NOT NULL, market_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS decision_log (
                id INTEGER PRIMARY KEY, run_id INTEGER, observed_at TEXT NOT NULL,
                ticker TEXT, event_ticker TEXT, strategy TEXT, action TEXT NOT NULL,
                reason TEXT NOT NULL, payload_json TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS decision_log_time ON decision_log(observed_at);
            CREATE TRIGGER IF NOT EXISTS immutable_decision BEFORE UPDATE ON decision_log
                BEGIN SELECT RAISE(ABORT,'immutable decision log'); END;
            CREATE TRIGGER IF NOT EXISTS no_delete_decision BEFORE DELETE ON decision_log
                BEGIN SELECT RAISE(ABORT,'immutable decision log'); END;
        ''')

    def log_decision(self, action, reason, now, *, ticker=None, event_ticker=None,
                     strategy=None, payload=None, run_id=None):
        return self.db.execute('''INSERT INTO decision_log
            (run_id,observed_at,ticker,event_ticker,strategy,action,reason,payload_json)
            VALUES (?,?,?,?,?,?,?,?)''',(run_id,now.isoformat(),ticker,event_ticker,
                strategy,action,reason,dumps(payload or {}))).lastrowid

    def observe_signal(self, signal, market, now, run_id=None):
        known = self.db.execute('''SELECT 1 FROM settlements WHERE ticker=?
            OR json_extract(market_json,'$.event_ticker')=? LIMIT 1''',
            (signal['ticker'],signal['event_ticker'])).fetchone()
        if known:
            return False
        p = decimal(signal['p_yes'])
        if not 0 <= p <= 1:
            raise ValueError('probability outside [0,1]')
        baseline = None
        try:
            ask, bid = decimal(market['yes_ask_dollars']), decimal(market['yes_bid_dollars'])
            if 0 <= bid <= ask <= 1:
                baseline = str((ask+bid)/2)
        except (ValueError, KeyError, TypeError, InvalidOperation):
            pass
        self.db.execute('''INSERT INTO signals
            (run_id,ticker,observed_at,p_yes,baseline_p_yes,model_version,signal_json)
            VALUES (?,?,?,?,?,?,?)''', (run_id, signal['ticker'], now.isoformat(), str(p),
            baseline, signal['model_version'], dumps(signal)))

    def forecasts(self):
        return [dict(r) for r in self.db.execute('SELECT * FROM signals ORDER BY id')]

    def calibration(self, trades=False):
        if trades:
            rows = self.db.execute('''SELECT p_yes,baseline_p_yes,result FROM positions
                                     WHERE status='settled' ''').fetchall()
        else:
            rows = self.db.execute('''SELECT s.p_yes,s.baseline_p_yes,z.result FROM signals s
                JOIN settlements z ON z.ticker=s.ticker WHERE s.id=(
                    SELECT MIN(s2.id) FROM signals s2 WHERE s2.ticker=s.ticker)''').fetchall()
        if not rows:
            return {'n': 0, 'brier': None, 'baseline_n': 0, 'baseline_brier': None}
        brier = sum(((decimal(r['p_yes'])-int(r['result']=='yes'))**2 for r in rows), Decimal(0))/len(rows)
        baseline = [(decimal(r['baseline_p_yes'])-int(r['result']=='yes'))**2
                    for r in rows if r['baseline_p_yes'] is not None]
        return {'n': len(rows), 'brier': brier, 'baseline_n': len(baseline),
                'baseline_brier': sum(baseline, Decimal(0))/len(baseline) if baseline else None}

    def settle(self, market, now):
        if market.get('status') not in ('settled', 'finalized') or market.get('result') not in ('yes','no'):
            return False
        self.db.execute('BEGIN IMMEDIATE')
        try:
            changed = self.db.execute('INSERT OR IGNORE INTO settlements VALUES (?,?,?,?)',
                (market['ticker'], market['result'], now.isoformat(), dumps(market))).rowcount
            official = self.db.execute('SELECT result FROM settlements WHERE ticker=?',
                (market['ticker'],)).fetchone()['result']
            for p in self.db.execute("SELECT * FROM positions WHERE ticker=? AND status='open'", (market['ticker'],)).fetchall():
                payout = Decimal(p['qty']) if p['side'] == official else Decimal(0)
                self.db.execute('''UPDATE positions SET status='settled', result=?, payout=?, net=?,
                    settled_at=? WHERE id=?''', (official, str(payout), str(payout-decimal(p['cost'])),
                    now.isoformat(), p['id']))
                changed = 1
            if changed:
                self.log_decision('settlement','official final binary resolution',now,
                    ticker=market['ticker'],event_ticker=market.get('event_ticker'),payload=market)
            self.db.execute('COMMIT')
            return bool(changed)
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def close(self):
        self.db.close()

    def positions(self, status=None):
        query = 'SELECT * FROM positions'
        args = ()
        if status:
            query += ' WHERE status=?'
            args = (status,)
        rows = []
        for row in self.db.execute(query+' ORDER BY id', args):
            item = dict(row)
            item['signal'] = json.loads(item.pop('signal_json'))
            rows.append(item)
        return rows

    def cash(self):
        return INITIAL_CASH - sum((decimal(p['cost']) for p in self.positions()), Decimal(0)) + sum(
            (decimal(p['payout']) for p in self.positions('settled')), Decimal(0))

    def open_cost(self):
        return sum((decimal(p['cost']) for p in self.positions('open')), Decimal(0))

    def enter(self, candidate, now):
        """Reserve cash atomically. A settled event can never be entered again."""
        c = candidate
        self.db.execute('BEGIN IMMEDIATE')
        try:
            known = self.db.execute('''SELECT 1 FROM settlements WHERE ticker=?
                OR json_extract(market_json,'$.event_ticker')=? LIMIT 1''',
                (c['ticker'],c['event_ticker'])).fetchone()
            if known:
                self.db.execute('ROLLBACK')
                return False
            cost = decimal(c['cost'])
            sig = c['signal']
            category = sig.get('category') or ('Weather' if c['ticker'].startswith('KXHIGH') else 'Other')
            open_positions = self.positions('open')
            category_cost = sum((decimal(p['cost']) for p in open_positions
                if (p['signal'].get('category') or ('Weather' if p['ticker'].startswith('KXHIGH') else 'Other')) == category),Decimal(0))
            control_cost = sum((decimal(p['cost']) for p in open_positions
                if p['signal'].get('entry_tier')=='exploratory' and p['signal'].get('category')==category),Decimal(0))
            if category != 'Other' and category_cost+cost > CATEGORY_CAP:
                self.db.execute('ROLLBACK')
                return False
            if sig.get('entry_tier')=='exploratory' and (cost > EXPLORATION_CAP or control_cost+cost > EXPLORATION_CATEGORY_CAP):
                self.db.execute('ROLLBACK')
                return False
            if cost <= 0 or cost > TRADE_CAP or cost > self.cash() or self.open_cost()+cost > OPEN_CAP:
                self.db.execute('ROLLBACK')
                return False
            self.db.execute('''INSERT INTO positions
                (ticker,event_ticker,side,qty,entry_price,fee,cost,p_yes,baseline_p_yes,
                 model_version,fee_model_version,signal_json,opened_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (c['ticker'], c['event_ticker'], c['side'], c['qty'], str(c['entry_price']),
                 str(c['fee']), str(cost), str(c['p_yes']), str(c['baseline_p_yes']),
                 c['model_version'], FEE_MODEL_VERSION, dumps(c['signal']), now.isoformat()))
            self.log_decision('entry',sig.get('decision_reason','model margin and risk filters passed'),now,
                ticker=c['ticker'],event_ticker=c['event_ticker'],
                strategy=sig.get('strategy_id',c['model_version']),payload=c)
            self.db.execute('COMMIT')
            return True
        except sqlite3.IntegrityError:
            self.db.execute('ROLLBACK')
            return False
        except BaseException:
            self.db.execute('ROLLBACK')
            raise


if __name__ == '__main__':
    from cli import main
    raise SystemExit(main())

