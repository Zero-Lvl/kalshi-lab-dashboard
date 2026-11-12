"""Local command-line interface and report artifacts; no live order capabilities."""
import argparse
import csv
import io
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


def load_report(path):
    from engine import Ledger
    from reporting import make_report, simulate
    if not path.exists():
        raise FileNotFoundError('No paper database yet; run scan first.')
    ledger = Ledger.__new__(Ledger)
    ledger.path = path
    ledger.db = sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
    ledger.db.row_factory = sqlite3.Row
    try:
        tables = {r[0] for r in ledger.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        row = ledger.db.execute('SELECT report_json FROM runs WHERE completed_at IS NOT NULL ORDER BY id DESC LIMIT 1').fetchone() if 'runs' in tables else None
        run = json.loads(row[0]) if row else {}
        report = make_report(ledger,run)
        report['simulation'] = simulate(report['positions'])
        from strategies import strategy_policy
        report['strategy_states'] = list(strategy_policy(ledger,datetime.now(timezone.utc)).values())
        from analytics import build_public_export
        report['public_export'] = build_public_export(ledger,report)
        return report
    finally:
        ledger.close()


def save_artifacts(report, destination):
    from engine import dumps
    from reporting import report_text
    from dashboard import build_dashboard
    destination.mkdir(parents=True,exist_ok=True)
    text = report_text(report)
    detail = text+'\n\n## Every paper position\n'
    for p in report['positions']:
        detail += f"\n- {p['ticker']} | {p['side']} ×{p['qty']} @ ${p['entry_price']} | fee ${p['fee']} | cost ${p['cost']} | {p['status']} | payout {p['payout']} | net {p['net']} | if win ${p['potential_net_win']} / if lose ${p['potential_net_loss']}\n"
    detail += '\n## Coverage and limitations\n'+dumps(report.get('coverage',[]))+'\n'+'\n'.join(report['warnings'])
    detail += '\n\n## Data warnings and model abstentions\n'+dumps(report.get('errors',[])+report.get('abstentions',[]))
    detail += '\n\n## Full structured results\nSee latest.json for every forecast, market snapshot, news URL, and match.\n'
    fields = ['ticker','event_ticker','opened_at','side','qty','entry_price','fee','cost','p_yes',
              'baseline_p_yes','model_version','fee_model_version','status','result','payout','net',
              'settled_at','potential_net_win','potential_net_loss','mark_net']
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream,fields,extrasaction='ignore')
    writer.writeheader()
    for row in report['positions']:
        # CSV safe for spreadsheet viewing even if an external identifier changes.
        writer.writerow({k: ("'"+str(v) if isinstance(v,str) and v.startswith(('=','+','@')) else v) for k,v in row.items() if k in fields})
    for name,body in [('latest.json',dumps(report)),('latest.md',detail),('daily.txt',text),
                      ('index.html',build_dashboard(report)),('positions.csv',stream.getvalue())]:
        temp = destination/('.'+name+'.new')
        temp.write_text(body,encoding='utf-8')
        temp.replace(destination/name)
    from site_builder import build_site
    manifest=build_site(report['public_export'],destination.parent/'site')
    (destination/'site-manifest.json').write_text(dumps(manifest),encoding='utf-8')


def main(argv=None):
    from engine import ROOT
    from scanner import scan
    from reporting import report_text, daily_text
    parser = argparse.ArgumentParser(description='Read-only public data + local paper predictions. No real bets.')
    sub = parser.add_subparsers(dest='command',required=True)
    for command in ('scan','report'):
        p = sub.add_parser(command)
        p.add_argument('--db',type=Path,default=ROOT/'data/paper.db')
        p.add_argument('--reports',type=Path,default=ROOT/'reports')
        p.add_argument('--full',action='store_true',help='Include every daily settlement and every open position as plain text')
        if command=='scan':
            p.add_argument('--no-news',action='store_true')
            p.add_argument('--settlement-only',action='store_true',help='Refresh known forecasts/positions only; never select new entries')
            p.add_argument('--dry-run',action='store_true',help='Record data/forecasts, but do not open new paper positions')
    args = parser.parse_args(argv)
    try:
        if args.command=='scan':
            scan(args.db,no_news=args.no_news,dry_run=args.dry_run,settlement_only=args.settlement_only)
        report = load_report(args.db)
        save_artifacts(report,args.reports)
        print(daily_text(report) if args.full else report_text(report))
        return 0
    except Exception as exc:
        print('Kalshi Lab failed: '+str(exc),file=sys.stderr)
        return 1
