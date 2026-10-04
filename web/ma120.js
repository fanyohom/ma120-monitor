const $ = (id) => document.getElementById(id);
const number = (value, digits = 2) => value == null ? '—' : Number(value).toLocaleString('zh-CN', {minimumFractionDigits:digits, maximumFractionDigits:digits});
const amount = (value, currency = 'CNY') => value == null ? '—' : `${currency === 'HKD' ? 'HK$' : '¥'}${number(value)}`;
const signed = (value, suffix = '%') => value == null ? '—' : `${value >= 0 ? '+' : ''}${number(value)}${suffix}`;
let dashboard = null;
let scope = 'all';

function cell(row, value, className = '') {
  const td = document.createElement('td');
  td.textContent = value;
  if (className) td.className = className;
  row.append(td);
  return td;
}

function chip(item) {
  const span = document.createElement('span');
  span.className = `stock-status ${item.error ? 'unavailable' : item.signal || item.zone || ''}`;
  span.textContent = item.status || '—';
  return span;
}

function emptyRow(body, text) {
  const tr = document.createElement('tr');
  const td = cell(tr, text, 'empty-row');
  td.colSpan = 9;
  body.append(tr);
}

function notice(text, error = false) {
  const box = $('stockNotice');
  box.textContent = text;
  box.className = `notice${error ? ' error' : ''}`;
  box.hidden = !text;
}

function refreshFeedback(message, error = false) {
  const status = $('stockRefreshFeedback');
  status.textContent = message;
  status.className = `refresh-feedback${error ? ' error' : ''}`;
  status.hidden = !message;
}

async function request(path, post = false) {
  const response = await fetch(path, post ? {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'} : {});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || '读取失败');
  return result;
}

function rowsForScope() {
  if (!dashboard) return [];
  const held = dashboard.portfolio.map((row) => ({...row, section:'portfolio'}));
  const watched = dashboard.watchlist.map((row) => ({...row, section:'watchlist'}));
  const rows = scope === 'portfolio' ? held : scope === 'watchlist' ? watched : [...held, ...watched];
  const query = $('stockSearch').value.trim().toLowerCase();
  const filter = $('stockFilter').value;
  return rows.filter((row) => {
    if (query && !`${row.name} ${row.code}`.toLowerCase().includes(query)) return false;
    if (filter === 'all') return true;
    if (filter === 'unavailable') return Boolean(row.error);
    if (filter === 'buy' || filter === 'sell') return row.signal === filter;
    return row.zone === filter;
  }).sort((left, right) => {
    const priority = (row) => row.signal === 'buy' ? 0 : row.signal === 'sell' ? 1 :
      row.zone === 'below_buy' ? 2 : row.zone === 'above_sell' ? 3 : row.error ? 5 : 4;
    return priority(left) - priority(right) || left.code.localeCompare(right.code);
  });
}

function renderSummary() {
  const summary = dashboard.summary;
  $('stockPositionSummary').hidden = summary.portfolio_count === 0;
  $('portfolioCount').textContent = summary.portfolio_count;
  $('watchlistCount').textContent = summary.watchlist_count;
  $('buySignals').textContent = summary.buy_signals;
  $('sellSignals').textContent = summary.sell_signals;
  const currencyTotals = summary.currency_totals || {};
  const currencies = Object.entries(currencyTotals).filter(([, totals]) => totals.pnl_coverage > 0);
  if (Object.keys(currencyTotals).length > 1) {
    const byCurrency = (field) => Object.entries(currencyTotals).map(([currency, totals]) => `${currency} ${amount(totals[field], currency)}`).join(' / ');
    $('totalPnl').textContent = byCurrency('unrealized_pnl');
    $('totalValue').textContent = byCurrency('market_value');
    $('totalBasis').textContent = byCurrency('cost_basis');
    $('pnlCoverage').textContent = `已计成本 ${summary.pnl_coverage} 只 · 人民币与港币不合计`;
  } else {
    const currency = currencies[0]?.[0] || 'CNY';
    $('totalPnl').textContent = summary.pnl_coverage ? `${summary.unrealized_pnl >= 0 ? '+' : '-'}${amount(Math.abs(summary.unrealized_pnl), currency)}` : '—';
    $('totalPnl').className = summary.unrealized_pnl > 0 ? 'stock-pnl-positive' : summary.unrealized_pnl < 0 ? 'stock-pnl-negative' : '';
    $('totalValue').textContent = summary.pnl_coverage ? amount(summary.market_value, currency) : '—';
    $('totalBasis').textContent = summary.pnl_coverage ? amount(summary.cost_basis, currency) : '—';
    $('pnlCoverage').textContent = summary.pnl_coverage ? `已计成本 ${summary.pnl_coverage} / ${summary.portfolio_count} 只 · ${signed(summary.unrealized_pnl_pct)}` : '尚无有效持仓成本与份额';
  }
  $('stockUpdated').textContent = `最近测算 ${dashboard.generated_at.replace('T', ' ').slice(0, 19)} · 未下单`;
  if (dashboard.source_error) notice(`股票清单读取异常：${dashboard.source_error}`, true);
  else notice(summary.data_unavailable ? `${summary.data_unavailable} 只股票的行情或均线数据待补；其余结果可查看。` : '');
}

function renderRows() {
  const body = $('stockRows');
  body.replaceChildren();
  const rows = rowsForScope();
  const total = dashboard.portfolio.length + dashboard.watchlist.length;
  $('stockCountLabel').textContent = `显示 ${rows.length} / ${total} 只 · 买入区 ${dashboard.summary.buy_zone} · 卖出区 ${dashboard.summary.sell_zone}`;
  for (const item of rows) {
    const tr = document.createElement('tr');
    tr.dataset.code = item.code;
    const nameCell = document.createElement('td');
    const name = document.createElement('button');
    name.type = 'button';
    name.className = 'stock-name';
    name.textContent = item.name;
    name.addEventListener('click', (event) => { event.stopPropagation(); showDetail(item); });
    const code = document.createElement('span');
    code.className = 'stock-code';
    code.textContent = item.code;
    const section = document.createElement('span');
    section.className = 'stock-section';
    section.textContent = item.section === 'portfolio' ? '持仓' : '关注';
    nameCell.append(name, code, section);
    tr.append(nameCell);
    const priceCell = cell(tr, item.price == null ? '—' : number(item.price), 'stock-number');
    if (item.price_date) {
      const source = document.createElement('span');
      source.className = 'stock-sub';
      source.textContent = item.price_source === 'realtime' ? `${item.quote_time || ''} 实时` : `${item.price_date} 收盘`;
      priceCell.append(source);
    }
    cell(tr, number(item.ma120), 'stock-number');
    cell(tr, signed(item.ma120_pct), item.ma120_pct == null ? '' : item.ma120_pct >= 0 ? 'stock-pnl-positive' : 'stock-pnl-negative');
    cell(tr, number(item.buy_line));
    cell(tr, number(item.sell_line));
    const status = document.createElement('td');
    status.append(chip(item));
    tr.append(status);
    cell(tr, item.unrealized_pnl == null ? '—' : `${item.unrealized_pnl >= 0 ? '+' : '-'}${amount(Math.abs(item.unrealized_pnl), item.currency)}`,
      item.unrealized_pnl == null ? '' : item.unrealized_pnl >= 0 ? 'stock-pnl-positive' : 'stock-pnl-negative');
    cell(tr, '›', 'stock-arrow');
    tr.addEventListener('click', () => showDetail(item));
    body.append(tr);
  }
  if (!rows.length) emptyRow(body, dashboard.portfolio.length || dashboard.watchlist.length ? '当前筛选没有匹配的股票' : dashboard.source_error ? '股票清单读取失败' : '清单中暂无股票');
}

function showDetail(item) {
  $('stockDetailName').textContent = item.name;
  $('stockDetailCode').textContent = `${item.code} · ${item.section === 'portfolio' ? '持仓' : '关注池'}`;
  $('stockDetailStatus').replaceWith(Object.assign(chip(item), {id:'stockDetailStatus'}));
  $('stockDetailPrice').textContent = amount(item.price, item.currency);
  $('stockDetailMa').textContent = amount(item.ma120, item.currency);
  $('stockDetailDeviation').textContent = signed(item.ma120_pct);
  $('stockDetailBuy').textContent = item.buy_line == null ? '—' : amount(item.buy_line, item.currency);
  $('stockDetailSell').textContent = item.sell_line == null ? '—' : amount(item.sell_line, item.currency);
  $('stockDetailShares').textContent = item.shares == null ? '—' : `${number(item.shares, 0)} 股`;
  $('stockDetailBasis').textContent = amount(item.cost_basis, item.currency);
  $('stockDetailPnl').textContent = item.unrealized_pnl == null ? '—' : `${item.unrealized_pnl >= 0 ? '+' : '-'}${amount(Math.abs(item.unrealized_pnl), item.currency)} (${signed(item.unrealized_pnl_pct)})`;
  $('stockDetailSource').textContent = item.price_date ? `${item.price_source === 'realtime' ? '实时' : '日 K 收盘'} · ${item.price_date}${item.quote_time ? ` ${item.quote_time}` : ''}` : '—';
  $('stockDetailError').textContent = item.error || '';
  $('stockDetailError').hidden = !item.error;
  const range = document.querySelector('.stock-range');
  range.hidden = item.price == null || item.buy_line == null || item.sell_line == null;
  if (!range.hidden) {
    const relative = (item.price - item.buy_line) / (item.sell_line - item.buy_line) * 100;
    $('stockCurrentMarker').style.left = `${Math.min(100, Math.max(0, relative))}%`;
  }
  $('stockDialog').showModal();
}

function render(next) {
  dashboard = next;
  renderSummary();
  renderRows();
}

async function refresh() {
  const button = $('stockRefresh');
  button.disabled = true;
  button.textContent = '正在获取...';
  refreshFeedback('正在重新读取股票日 K 和报价...');
  try {
    const result = await request('/api/ma120/refresh', true);
    render(result.snapshot);
    const rows = [...result.snapshot.portfolio, ...result.snapshot.watchlist];
    const latest = rows.map((row) => row.price_date).filter(Boolean).sort().at(-1);
    const summary = result.snapshot.summary;
    refreshFeedback(`已更新 ${rows.length} 只股票${latest ? `（最新行情 ${latest}）` : ''}；今日买入信号 ${summary.buy_signals}、卖出信号 ${summary.sell_signals}${summary.data_unavailable ? `，${summary.data_unavailable} 只数据待补` : ''}。只读测算，未推送或下单。`);
  } catch (error) {
    notice(`刷新失败：${error.message}`, true);
    refreshFeedback(`刷新失败：${error.message}`, true);
  } finally {
    button.disabled = false;
    button.textContent = '↻ 刷新行情';
  }
}

document.querySelectorAll('[data-scope]').forEach((button) => button.addEventListener('click', () => {
  scope = button.dataset.scope;
  document.querySelectorAll('[data-scope]').forEach((tab) => {
    const selected = tab === button;
    tab.classList.toggle('active', selected);
    tab.setAttribute('aria-selected', String(selected));
  });
  if (dashboard) renderRows();
}));
$('stockSearch').addEventListener('input', () => { if (dashboard) renderRows(); });
$('stockFilter').addEventListener('change', () => { if (dashboard) renderRows(); });
$('stockRefresh').addEventListener('click', refresh);
$('stockDialogClose').addEventListener('click', () => $('stockDialog').close());
$('stockDialog').addEventListener('click', (event) => { if (event.target === $('stockDialog')) $('stockDialog').close(); });
request('/api/ma120').then((result) => { if (result.snapshot) render(result.snapshot); else refresh(); })
  .catch((error) => notice(`读取失败：${error.message}`, true));
