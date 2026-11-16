'use strict';
/* Public snapshot v1 (the adapter must map core.snapshot(), not publish it verbatim).
 * CommonJS API: render(document, snapshot, nowEpochMs=Date.now());
 * mount(document, window, fetcher, clock=Date.now) -> {sync(), async load()}.
 * Browser mount reads only GET ./forex-data.json, with no credentials or redirects.
 * Required envelope: schema_version:1, paper_only:true, live_trading_enabled:false.
 * generated_at and every time field: ISO UTC with seconds, Z or +00:00; null if unknown.
 * Decimal financial values are finite base-10 STRINGS, never JSON floating-point numbers.
 * USD: account.{initial_balance,cash,equity,realized_pnl,unrealized_pnl,used_margin,
 * available_margin,capital_at_risk}; statistics.{average_win,average_loss,total_costs}.
 * statistics.{closed_trades,wins,losses}: nonnegative JSON integer counts.
 * statistics.{win_rate,max_drawdown,net_return}: decimal FRACTIONS (0.01 -> 1%);
 * profit_factor: dimensionless decimal string or null, never Infinity.
 * periods.{day,week,month}: {realized_pnl,costs,trades}; not core's dated period maps.
 * positions.{open,closed}: arrays of records using pair,currency,side,entry_price,
 * exit_price,stop_loss,take_profit,units,opened_at,closed_at,duration (readable text),
 * costs (total USD),spread_cost,slippage_cost,commission_cost,financing (signed USD
 * cash flow),gross_pnl,net_pnl,unrealized_pnl,strategy,strategy_version,reason.
 * performance.points: [{time,equity,realized_pnl,unrealized_pnl}], observed only.
 * research.strategies: [{id,name,state,version,note,closed_trades,realized_pnl}].
 * research.{market_conditions,opportunities,findings,decisions,events}: record arrays
 * with time,type,title,summary,note,reason,action,strategy,pair,state,source/source_url
 * (HTTPS URLs). Events additionally: scheduled_at,actual,previous,revision,forecast.
 * Original release and revision remain separate time-stamped records in backend.
 * research.data_coverage: [{pair,timeframe,status,start,end,count,note,provenance}].
 * reports.{daily,weekly}: text or {period,generated_at,summary,starting_equity,
 * ending_equity,realized_pnl,unrealized_pnl,costs,trades,opened_trades,closed_trades,
 * wins,losses,largest_gain,largest_loss,blocked_trades,note}.
 * health: {status,data_status,last_data_at,last_research_at,emergency_stop,errors,
 * warnings}; errors/warnings: strings or {code,message}; limitations: strings or
 * {note,message}. Omitted/null metrics stay unavailable; never fill unknowns with 0.
 * No secrets or arbitrary nested metadata: only the allowlisted fields are rendered.
 * Freshness warning: >15 minutes old, unknown timestamp, or >60 seconds in future.
 */
// Isolated namespace: the legacy Kalshi renderer and its financial state stay untouched.
(function () {
const object = value => value && typeof value === 'object' && !Array.isArray(value) ? value : {};
const list = value => Array.isArray(value) ? value : [];
const scalar = value => typeof value === 'string' ? (/^[+-]?(?:NaN|Infinity)$/i.test(value.trim()) ? 'Unavailable' : value.slice(0, 4000)) : typeof value === 'number' && Number.isFinite(value) ? String(value) : typeof value === 'boolean' ? String(value) : 'Unavailable';
function decimal(value) {
  if (typeof value !== 'string' || !/^-?\d+(?:\.\d+)?$/.test(value)) return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}
function money(value) {
  const n = decimal(value);
  return n === null ? 'Unavailable' : new Intl.NumberFormat('en-US', {style:'currency',currency:'USD'}).format(n);
}
function percent(value, bounded=false) {
  const n=decimal(value);
  return n===null || (bounded && (n<0 || n>1)) ? 'Unavailable' : new Intl.NumberFormat('en-US',{style:'percent',maximumFractionDigits:6}).format(n);
}
const count = value => Number.isSafeInteger(value) && value>=0 ? String(value) : 'Unavailable';
function date(value) {
  // UTC with an explicit zone only: Date.parse alone accepts ambiguous dates and normalizes invalid days.
  if(typeof value!=='string' || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)$/.test(value)) return 'Unavailable';
  const n=Date.parse(value);
  if(!Number.isFinite(n))return 'Unavailable';
  const iso=new Date(n).toISOString();
  return iso.slice(0,19)===value.slice(0,19) ? iso : 'Unavailable';
}
function freshness(value, now) {
  const time=date(value)==='Unavailable' ? NaN : Date.parse(value);
  if(!Number.isFinite(time) || !Number.isFinite(now) || time>now+60000) return 'Snapshot freshness unverified: missing, invalid, or future generation time. Do not treat this as current market data.';
  return now-time>15*60*1000 ? 'Stale snapshot: generated over 15 minutes ago. Refresh before interpreting these observations; no current prices are inferred.' : '';
}
function render(doc, input, now=Date.now()) {
  const data = object(input);
  if (data.schema_version !== 1 || data.paper_only !== true || data.live_trading_enabled !== false) throw new Error('Unsupported paper-only Forex snapshot');
  const root = doc.getElementById('forex-content');
  root.replaceChildren();
  const el = (tag, text, cls) => { const n=doc.createElement(tag); if(text !== undefined)n.textContent=text; if(cls)n.className=cls; return n; };
  const section = title => {const n=el('section',undefined,'section panel');n.append(el('h2',title));root.append(n);return n;};
  const sourceLink = value => {
    if (typeof value !== 'string' || !/^https:\/\//i.test(value) || /[\s\\]/.test(value) || value.length>4000) return el('span','Unavailable');
    try {
      const url=new URL(value);
      if(url.protocol!=='https:' || !url.hostname || url.username || url.password) return el('span','Unavailable');
      const a=el('a',value,'fx-source');
      a.setAttribute('href',url.href);a.setAttribute('rel','noopener noreferrer');a.setAttribute('referrerpolicy','no-referrer');
      return a;
    } catch (_) {return el('span','Unavailable');}
  };
  const fields = (parent, record, schema) => {
    const row=object(record), dl=el('dl',undefined,'fx-fields');
    for(const [key,label,format] of schema) {
      const box=el('div'), value=el('dd');
      if(format===sourceLink)value.append(sourceLink(row[key]));else value.textContent=(format || scalar)(row[key]);
      box.append(el('dt',label),value);dl.append(box);
    }
    parent.append(dl);
  };
  const disclosure = (parent, label) => {const d=el('details',undefined,'values-disclosure');d.append(el('summary',label));parent.append(d);return d;};
  const records = (title, values, schema) => {
    const rows=list(values), s=disclosure(root,`${title} (${rows.length})`);
    if(!rows.length)s.append(el('p','No records available.'));
    for(const row of rows) {const card=el('article',undefined,'fx-record');if(typeof row==='string')card.append(el('p',scalar(row)));else fields(card,row,schema);s.append(card);}
  };
  const account=section('Independent Forex account');
  account.append(el('p',`Snapshot generated: ${date(data.generated_at)}`,'muted'));
  const warning=freshness(data.generated_at,now);
  if(warning)account.append(el('p',warning,'notice fx-stale'));
  fields(account,data.account,[['initial_balance','Initial simulated balance',money],['cash','Cash',money],['equity','Equity',money],['realized_pnl','Realized P&L',money],['unrealized_pnl','Unrealized P&L · unknown marks remain unavailable',money],['used_margin','Used margin',money],['available_margin','Available margin',money],['capital_at_risk','Capital at risk',money]]);
  const stats=object(data.statistics), measured=typeof stats.closed_trades==='number' && Number.isInteger(stats.closed_trades) && stats.closed_trades>0;
  const statSection=section('Performance statistics');
  const measurable=format => value => measured ? format(value) : 'Not yet measurable';
  fields(statSection,stats,[['closed_trades','Closed trades',count],['wins','Wins',measurable(count)],['losses','Losses',measurable(count)]]);
  fields(statSection,stats,[['win_rate','Win rate',measurable(v=>percent(v,true))],['average_win','Average winning trade',measurable(money)],['average_loss','Average losing trade',measurable(money)],['max_drawdown','Maximum drawdown',measurable(v=>percent(v,true))],['profit_factor','Profit factor',measurable(v=>decimal(v)!==null && decimal(v)>=0 ? scalar(v) : 'Unavailable')],['net_return','Net return after costs',measurable(percent)],['total_costs','Total costs',money]]);
  const periods=section('Calendar period totals');
  for(const key of ['day','week','month']) {const p=el('article');p.append(el('h3',{day:'Today',week:'This week',month:'This month'}[key]));fields(p,object(data.periods)[key],[['realized_pnl','Realized P&L',money],['costs','Costs',money],['trades','Trades']]);periods.append(p);}
  const positionFields=[['pair','Currency pair'],['currency','Currency'],['side','Side'],['entry_price','Entry price'],['exit_price','Exit price'],['stop_loss','Stop loss'],['take_profit','Take profit'],['units','Units'],['opened_at','Opened',date],['closed_at','Closed',date],['duration','Duration'],['costs','Trading costs',money],['spread_cost','Spread cost',money],['slippage_cost','Slippage cost',money],['commission_cost','Commission cost',money],['financing','Financing · signed cash flow',money],['gross_pnl','Gross P&L · before costs',money],['net_pnl','Net P&L',money],['unrealized_pnl','Unrealized P&L',money],['strategy','Strategy'],['strategy_version','Strategy version'],['reason','Entry / exit reason']];
  records('Open positions',object(data.positions).open,positionFields);
  records('Closed positions',object(data.positions).closed,positionFields);
  records('Observed performance values · no interpolated prices',object(data.performance).points,[['time','Observed time',date],['equity','Equity',money],['realized_pnl','Realized P&L',money],['unrealized_pnl','Unrealized P&L',money]]);
  const research=object(data.research);
  records('Strategies · experimental, not live permissions',research.strategies,[['id','ID'],['name','Strategy'],['state','State'],['version','Version'],['note','Note'],['closed_trades','Closed trades'],['realized_pnl','Realized P&L',money]]);
  const researchFields=[['time','Time',date],['type','Record type'],['title','Title'],['summary','Summary'],['note','Note'],['reason','Reason / rejection'],['action','Action'],['strategy','Strategy'],['pair','Currency pair'],['state','State'],['source','Source',sourceLink],['source_url','Source URL',sourceLink]];
  records('Market conditions · reported observations',research.market_conditions,researchFields);
  records('Opportunities · hypotheses, not executed trades',research.opportunities,researchFields);
  records('Research findings',research.findings,researchFields);
  records('Decisions and rejection reasons',research.decisions,researchFields);
  const events=disclosure(root,'Economic events: schedule ≠ actual release ≠ revision ≠ news');
  events.append(el('p','A scheduled release is not an actual result. Preserve original observations and subsequent revisions as separate timestamped records; do not overwrite the original or use later revisions in earlier decisions. News is context, not a verified economic release.'));
  for(const row of list(research.events))fields(events,row,[...researchFields,['scheduled_at','Scheduled time',date],['actual','Actual released value'],['previous','Previous'],['revision','Revision'],['forecast','Forecast']]);
  records('Data coverage',research.data_coverage,[['pair','Currency pair'],['timeframe','Timeframe'],['status','Status'],['start','Start',date],['end','End',date],['count','Observations'],['note','Note'],['provenance','Provenance']]);
  const reports=object(data.reports);
  for(const key of ['daily','weekly']) {const report=disclosure(root,`${key==='daily'?'Daily':'Weekly'} report`);if(typeof reports[key]==='string')report.append(el('p',scalar(reports[key])));else fields(report,reports[key],[['period','Period'],['generated_at','Generated',date],['summary','Summary'],['starting_equity','Starting equity',money],['ending_equity','Ending equity',money],['realized_pnl','Realized P&L',money],['unrealized_pnl','Unrealized P&L',money],['costs','Costs',money],['trades','Trades',count],['opened_trades','Trades opened',count],['closed_trades','Trades closed',count],['wins','Wins',count],['losses','Losses',count],['largest_gain','Largest gain',money],['largest_loss','Largest loss',money],['blocked_trades','Blocked trades',count],['note','Note']]);}
  const health=object(data.health), h=section('System health · reported, not independently verified');
  fields(h,health,[['status','Engine status'],['data_status','Market-data status'],['last_data_at','Last successful data',date],['last_research_at','Last completed research',date],['emergency_stop','Local emergency-stop state']]);
  h.append(el('p','Unavailable or missing timestamps do not mean healthy. Browser controls cannot change engine state.'));
  records('Errors',health.errors,[['code','Code'],['message','Message']]);
  records('Warnings',health.warnings,[['code','Code'],['message','Message']]);
  records('Limitations',data.limitations,[['note','Note'],['message','Message']]);
}
function mount(doc, win, fetcher, clock=Date.now) {
  let wasActive=false, pending=false;
  const status=doc.getElementById('forex-status'), retry=doc.getElementById('forex-retry'), content=doc.getElementById('forex-content');
  const load=async () => {
    if(pending)return;
    pending=true;retry.hidden=true;content.setAttribute('aria-busy','true');status.setAttribute('data-state','loading');status.textContent='Loading read-only Forex snapshot…';
    try {
      const response=await fetcher('./forex-data.json',{method:'GET',credentials:'omit',mode:'same-origin',redirect:'error',cache:'no-store'});
      if(!response.ok)throw new Error('Snapshot unavailable');
      const data=await response.json(), now=clock();
      render(doc,data,now);
      const warning=freshness(data.generated_at,now);
      status.setAttribute('data-state',warning?'stale':'ready');
      status.textContent=warning || 'Paper-only snapshot loaded. Check timestamps and warnings below; this is not a live broker connection.';
    } catch (_) {
      content.replaceChildren();status.setAttribute('data-state','error');
      status.textContent='Forex data unavailable or malformed. Waiting for secure practice configuration / a valid public snapshot. No prices or returns inferred.';
    } finally {pending=false;retry.hidden=false;content.setAttribute('aria-busy','false');}
  };
  const sync=()=>{
    const active=win.location.hash==='#forex';
    doc.getElementById('kalshi-panel').hidden=active;doc.getElementById('forex').hidden=!active;
    doc.getElementById('nav-kalshi').setAttribute('aria-current',active?'false':'page');
    doc.getElementById('nav-forex').setAttribute('aria-current',active?'page':'false');
    if(active&&!wasActive)void load();
    // The browser may resolve a fragment while the Kalshi panel is still hidden.
    if(!active && wasActive && win.location.hash) {
      const target=doc.getElementById(win.location.hash.slice(1));
      if(target && typeof target.scrollIntoView==='function')target.scrollIntoView();
    }
    wasActive=active;
  };
  retry.addEventListener('click',load);win.addEventListener('hashchange',sync);sync();return {sync,load};
}
if(typeof module!=='undefined' && module.exports)module.exports={mount,render,money};
if(typeof document!=='undefined' && typeof window!=='undefined')mount(document,window,window.fetch.bind(window));
})();
