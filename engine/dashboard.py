"""Offline static HTML; no server, remote code, trackers, or unescaped feed HTML."""
from html import escape
from pathlib import Path
from urllib.parse import urlsplit
import json


def build_dashboard(report):
    def e(v): return escape(str(v),quote=True)
    def link(url,title):
        try:
            p = urlsplit(url)
            valid = p.scheme in ('https','http') and p.hostname and not p.username
        except ValueError:
            valid = False
        return f'<a rel="noreferrer noopener" href="{e(url)}">{e(title)}</a>' if valid else e(title)
    s = report.get('summary',{})
    cards = ''.join(f'<div class="metric"><span>{e(label)}</span><strong>{e(s.get(key,"—"))}</strong></div>'
                    for key,label in [('cash','Simulated cash ($)'),('open_cost','Open entry cost ($)'),
                                      ('realized_net','Realized net ($)'),('wins','Settled wins'),('losses','Settled losses')])
    sections = []
    for title,key in [('Open & settled paper positions','positions'),('Ranked market/news watchlist — not trade recommendations','watchlist'),
                      ('Forecasts — experimental, not calibrated','signals')]:
        items = []
        for row in report.get(key,[])[:60]:
            items.append('<pre>'+e(json.dumps(row,ensure_ascii=False,indent=2,default=str))+'</pre>')
        sections.append(f'<details><summary>{e(title)} ({len(report.get(key,[]))})</summary>'+(''.join(items) or '<p>None yet.</p>')+'</details>')
    articles = report.get('news',{}).get('articles',[])
    sections.append('<details><summary>Source headlines and summaries ('+str(len(articles))+')</summary><ul>'+
                    ''.join('<li>'+link(a.get('url',''),a.get('title',''))+'<small>'+e(a.get('source',''))+' · '+e(a.get('published_at',''))+
                            '</small><p>'+e(a.get('summary',''))+'</p></li>' for a in articles)+'</ul></details>')
    notices = report.get('warnings',[]) + [json.dumps(x,ensure_ascii=False) for x in report.get('errors',[]) + report.get('abstentions',[])]
    return '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>Kalshi Lab · Paper research</title><style>
:root{color-scheme:dark}*{box-sizing:border-box}body{font:18px/1.6 system-ui,sans-serif;background:#10151b;color:#e1e7ee;max-width:820px;margin:auto;padding:24px 18px}h1{font-size:28px;line-height:1.25}h2,summary{font-size:19px}small,span{display:block;color:#aab7c5}a{color:#9fc5ee;overflow-wrap:anywhere}.badge{color:#e1c88a}.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(165px,1fr));gap:12px}.metric,details{background:#18212b;border:1px solid #293a49;border-radius:10px;padding:16px;margin:12px 0}.metric strong{font-size:23px}summary{cursor:pointer}pre{font:14px/1.55 ui-monospace,monospace;white-space:pre-wrap;overflow-wrap:anywhere;border-bottom:1px solid #354353;padding:12px 0}li{margin:20px 0}footer{color:#aab7c5;margin-top:28px}button{font:inherit}p{max-width:65ch}</style></head><body>
<h1>Kalshi Lab</h1><p class="badge">PAPER ONLY · No money wagered</p><p>Data observed: '''+e(report.get('observed_at','No completed scan'))+'''</p>
<section class="metrics">'''+cards+'</section><h2>Results stay separate from estimates</h2><p>Open positions are unresolved. News matches identify related topics—not causal evidence or price direction. Model probabilities and assumed fills are unvalidated.</p>'+''.join(sections)+ '<details><summary>Coverage, limitations and data warnings</summary><ul>'+''.join('<li>'+e(n)+'</li>' for n in notices)+'</ul><pre>'+e(json.dumps(report.get('coverage',[]),indent=2))+'</pre></details><footer>Local offline report. No account connection, trade execution, remote scripts, or tracking.</footer></body></html>'


def render_dashboard(report, output):
    output = Path(output)
    output.parent.mkdir(parents=True,exist_ok=True)
    html = build_dashboard(report)
    output.write_text(html,encoding='utf-8')
    return str(output)
