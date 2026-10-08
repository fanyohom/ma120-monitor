const $ = (id) => document.getElementById(id);
const number = (value, digits = 2) => value == null ? '—' : Number(value).toLocaleString('zh-CN', {minimumFractionDigits:digits, maximumFractionDigits:digits});
const amount = (value, currency = 'CNY') => value == null ? '—' : `${currency === 'HKD' ? 'HK$' : '¥'}${number(value)}`;
const signed = (value, suffix = '%') => value == null ? '—' : `${value >= 0 ? '+' : ''}${number(value)}${suffix}`;
let dashboard = null;
let scope = 'all';
let currentPage = 1;
let activeCode = null;
let historyRequest = 0;
let estimateRequest = 0;
let estimateCode = null;
const stockHistoryCache = new Map();
let watchlistItems = [];
let watchlistBusy = false;

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

async function watchlistRequest(method, path = '/api/ma120/watchlist', body) {
  const options = {method};
  if (body) {
    options.headers = {'Content-Type':'application/json'};
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || '保存失败');
  return result;
}

function watchlistFeedback(message, error = false) {
  const status = $('watchlistFeedback');
  status.textContent = message;
  status.className = `watchlist-feedback${error ? ' error' : ''}`;
  status.hidden = !message;
}

function setWatchlistBusy(busy) {
  watchlistBusy = busy;
  $('watchlistDialog').querySelectorAll('button, input').forEach((control) => { control.disabled = busy; });
}

function watchlistAction(label, handler, danger = false) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = `watchlist-action${danger ? ' danger' : ''}`;
  button.textContent = label;
  button.addEventListener('click', handler);
  return button;
}

function editWatchlistItem(row, item) {
  if (watchlistBusy) return;
  const form = document.createElement('form');
  form.className = 'watchlist-rename';
  const code = document.createElement('span');
  code.className = 'watchlist-code';
  code.textContent = item.code;
  const input = document.createElement('input');
  input.type = 'text';
  input.required = true;
  input.maxLength = 40;
  input.setAttribute('aria-label', `${item.code} 的股票名称`);
  input.value = item.name;
  const save = document.createElement('button');
  save.type = 'submit';
  save.className = 'watchlist-action save';
  save.textContent = '保存';
  const cancel = watchlistAction('取消', () => renderWatchlistItems(watchlistItems));
  form.append(code, input, save, cancel);
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    const name = input.value.trim();
    if (!name) { input.focus(); return; }
    mutateWatchlist('PATCH', `/api/ma120/watchlist?code=${encodeURIComponent(item.code)}`, {name}, `${item.code} 已改名`);
  });
  row.replaceChildren(form);
  input.focus();
  input.select();
}

function renderWatchlistItems(items) {
  watchlistItems = items;
  $('watchlistEditorCount').textContent = `${items.length} 只`;
  const list = $('watchlistItems');
  list.replaceChildren();
  for (const item of items) {
    const row = document.createElement('div');
    row.className = 'watchlist-item';
    const identity = document.createElement('div');
    identity.className = 'watchlist-identity';
    const name = document.createElement('strong');
    name.textContent = item.name;
    const code = document.createElement('span');
    code.className = 'watchlist-code';
    code.textContent = item.code;
    identity.append(name, code);
    const actions = document.createElement('div');
    actions.className = 'watchlist-item-actions';
    actions.append(
      watchlistAction('改名', () => editWatchlistItem(row, item)),
      watchlistAction('移除', () => {
        if (!window.confirm(`从关注池移除 ${item.name}（${item.code}）？`)) return;
        mutateWatchlist('DELETE', `/api/ma120/watchlist?code=${encodeURIComponent(item.code)}`, null, `${item.name} 已移出关注池`);
      }, true),
    );
    row.append(identity, actions);
    list.append(row);
  }
  if (!items.length) {
    const empty = document.createElement('p');
    empty.className = 'watchlist-empty';
    empty.textContent = '关注池暂无股票';
    list.append(empty);
  }
}

async function mutateWatchlist(method, path, body, successMessage) {
  if (watchlistBusy) return;
  setWatchlistBusy(true);
  watchlistFeedback('正在保存...');
  try {
    const result = await watchlistRequest(method, path, body);
    renderWatchlistItems(result.items);
    setWatchlistBusy(true);
    $('watchlistAdd').reset();
    watchlistFeedback(successMessage);
    try {
      const updated = await request('/api/ma120');
      if (updated.snapshot) render(updated.snapshot);
      else watchlistFeedback(`${successMessage}；监控概览稍后更新`);
    } catch (error) {
      watchlistFeedback(`${successMessage}；监控概览更新失败：${error.message}`, true);
    }
  } catch (error) {
    watchlistFeedback(error.message, true);
  } finally {
    setWatchlistBusy(false);
  }
}

async function openWatchlist() {
  const dialog = $('watchlistDialog');
  if (dialog.open) return;
  dialog.showModal();
  watchlistFeedback('正在读取关注池...');
  $('watchlistItems').replaceChildren();
  setWatchlistBusy(true);
  let loaded = false;
  try {
    const result = await watchlistRequest('GET');
    renderWatchlistItems(result.items);
    loaded = true;
    watchlistFeedback('');
  } catch (error) {
    watchlistFeedback(`读取失败：${error.message}`, true);
  } finally {
    setWatchlistBusy(false);
    if (loaded && dialog.open) $('watchlistCode').focus();
  }
}

function rowsForScope() {
  if (!dashboard) return [];
  const held = dashboard.portfolio.map((row) => ({...row, section:'portfolio'}));
  const watched = dashboard.watchlist.map((row) => ({...row, section:'watchlist'}));
  const rows = scope === 'portfolio' ? held : scope === 'watchlist' ? watched : [...held, ...watched];
  const query = $('stockSearch').value.trim().toLowerCase();
  const filter = $('stockFilter').value;
  const sort = $('stockSort').value;
  const ranks = {
    signal: {buy:0, sell:1, below_buy:2, above_sell:3, between:4, unavailable:5},
    buy: {buy:0, below_buy:1, between:2, above_sell:3, sell:4, unavailable:5},
    sell: {sell:0, above_sell:1, between:2, below_buy:3, buy:4, unavailable:5},
  };
  const statusKey = (row) => row.error ? 'unavailable' : row.signal || row.zone || 'between';
  return rows.filter((row) => {
    if (query && !`${row.name} ${row.code}`.toLowerCase().includes(query)) return false;
    if (filter === 'all') return true;
    if (filter === 'unavailable') return Boolean(row.error);
    if (filter === 'buy' || filter === 'sell') return row.signal === filter;
    return row.zone === filter;
  }).sort((left, right) => {
    if (sort === 'code') return left.code.localeCompare(right.code);
    return ranks[sort][statusKey(left)] - ranks[sort][statusKey(right)] || left.code.localeCompare(right.code);
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
  const pageSize = Number($('stockPageSize').value);
  const pageCount = Math.max(1, Math.ceil(rows.length / pageSize));
  currentPage = Math.min(currentPage, pageCount);
  const start = (currentPage - 1) * pageSize;
  const visibleRows = rows.slice(start, start + pageSize);
  $('stockCountLabel').textContent = `本页 ${visibleRows.length} 只 · 筛选 ${rows.length} / 全部 ${total} 只 · 买入区 ${dashboard.summary.buy_zone} · 卖出区 ${dashboard.summary.sell_zone}`;
  $('stockPageInfo').textContent = rows.length
    ? `${start + 1}–${start + visibleRows.length} / ${rows.length} · 第 ${currentPage} / ${pageCount} 页`
    : '0 / 0 · 第 1 / 1 页';
  $('stockPrevPage').disabled = currentPage === 1;
  $('stockNextPage').disabled = currentPage === pageCount;
  for (const item of visibleRows) {
    const tr = document.createElement('tr');
    tr.dataset.code = item.code;
    const nameCell = document.createElement('td');
    const name = document.createElement('a');
    name.href = `/ma120/${encodeURIComponent(item.code)}`;
    name.className = 'stock-name';
    name.textContent = item.name;
    name.addEventListener('click', (event) => {
      event.stopPropagation();
      if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      navigate(item.code);
    });
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
    tr.addEventListener('click', () => navigate(item.code));
    body.append(tr);
  }
  if (!rows.length) emptyRow(body, dashboard.portfolio.length || dashboard.watchlist.length ? '当前筛选没有匹配的股票' : dashboard.source_error ? '股票清单读取失败' : '清单中暂无股票');
}

function routeCode() {
  const match = location.pathname.match(/^\/ma120\/([^/]+)\/?$/);
  if (!match) return null;
  try { return decodeURIComponent(match[1]); }
  catch { return match[1]; }
}

function findStock(code) {
  const held = dashboard.portfolio.find((row) => row.code === code);
  if (held) return {...held, section:'portfolio'};
  const watched = dashboard.watchlist.find((row) => row.code === code);
  return watched ? {...watched, section:'watchlist'} : null;
}

function navigate(code) {
  const path = code ? `/ma120/${encodeURIComponent(code)}` : '/ma120';
  if (location.pathname !== path) window.history.pushState({}, '', path);
  applyRoute();
  window.scrollTo(0, 0);
}

function renderCurrentStock(item) {
  const currency = item?.currency || 'CNY';
  $('stockDetailName').textContent = item?.name || activeCode;
  $('stockDetailCode').textContent = item ? `${item.code} · ${item.section === 'portfolio' ? '持仓' : '关注池'}` : activeCode;
  const status = item ? chip(item) : Object.assign(document.createElement('span'), {className:'stock-status unavailable', textContent:'未在清单中'});
  status.id = 'stockDetailStatus';
  $('stockDetailStatus').replaceWith(status);
  $('stockDetailPrice').textContent = amount(item?.price, currency);
  $('stockDetailMa').textContent = amount(item?.ma120, currency);
  $('stockDetailDeviation').textContent = signed(item?.ma120_pct);
  $('stockDetailBuy').textContent = amount(item?.buy_line, currency);
  $('stockDetailSell').textContent = amount(item?.sell_line, currency);
  $('stockDetailShares').textContent = item?.shares == null ? '—' : `${number(item.shares, 0)} 股`;
  $('stockDetailBasis').textContent = amount(item?.cost_basis, currency);
  $('stockDetailPnl').textContent = item?.unrealized_pnl == null ? '—' : `${item.unrealized_pnl >= 0 ? '+' : '-'}${amount(Math.abs(item.unrealized_pnl), currency)} (${signed(item.unrealized_pnl_pct)})`;
  $('stockDetailSource').textContent = item?.price_date ? `${item.price_source === 'realtime' ? '实时行情' : '日 K 收盘'} · ${item.price_date}${item.quote_time ? ` ${item.quote_time}` : ''}` : '暂无当前行情';
  $('stockDetailError').textContent = item?.error || (!item ? '这只股票不在当前持仓或关注清单中。' : '');
  $('stockDetailError').hidden = !($('stockDetailError').textContent);
}

function svgElement(tag, attributes = {}) {
  const element = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, String(value));
  return element;
}

function drawStockChart(history) {
  const shell = $('stockHistoryChart');
  shell.replaceChildren();
  const points = history.series || [];
  if (!points.length) { shell.textContent = '暂无可回溯的已完成日 K'; return; }
  const width = Math.max(shell.clientWidth, 280);
  const height = shell.clientHeight || 350;
  const pad = {left:54, right:18, top:19, bottom:33};
  const plotWidth = width - pad.left - pad.right;
  const plotHeight = height - pad.top - pad.bottom;
  const values = points.flatMap((point) => [point.close, point.ma120, point.buy_line, point.sell_line]);
  const low = Math.min(...values);
  const high = Math.max(...values);
  const margin = Math.max((high - low) * .06, high * .01);
  const min = low - margin;
  const max = high + margin;
  const x = (index) => pad.left + index / Math.max(points.length - 1, 1) * plotWidth;
  const y = (value) => pad.top + (max - value) / (max - min) * plotHeight;
  const svg = svgElement('svg', {viewBox:`0 0 ${width} ${height}`, width:'100%', height:'100%', 'aria-hidden':'true'});
  for (let step = 0; step <= 4; step++) {
    const value = min + (max - min) * step / 4;
    const lineY = y(value);
    svg.append(svgElement('line', {x1:pad.left, y1:lineY, x2:width-pad.right, y2:lineY, stroke:'#e8eeeb', 'stroke-width':1}));
    const label = svgElement('text', {x:pad.left-9, y:lineY+4, 'text-anchor':'end', fill:'#819095', 'font-size':11});
    label.textContent = value.toFixed(2);
    svg.append(label);
  }
  for (const index of [...new Set([0, Math.floor((points.length - 1) / 2), points.length - 1])]) {
    const label = svgElement('text', {x:x(index), y:height-7,
      'text-anchor':index === 0 ? 'start' : index === points.length-1 ? 'end' : 'middle', fill:'#819095', 'font-size':11});
    label.textContent = points[index].date;
    svg.append(label);
  }
  for (const [key, color, dash, strokeWidth] of [
    ['buy_line', '#64a388', '4 4', 1.4], ['sell_line', '#d18b79', '4 4', 1.4],
    ['ma120', '#d19a3e', '6 4', 1.8], ['close', '#225c65', '', 2.1],
  ]) {
    const path = points.map((point, index) => `${index ? 'L' : 'M'}${x(index).toFixed(1)},${y(point[key]).toFixed(1)}`).join(' ');
    svg.append(svgElement('path', {d:path, fill:'none', stroke:color, 'stroke-width':strokeWidth,
      'stroke-dasharray':dash, 'stroke-linejoin':'round'}));
  }
  const compact = history.signals.length > 40;
  points.forEach((point, index) => {
    if (point.signal !== 'buy' && point.signal !== 'sell') return;
    const buy = point.signal === 'buy';
    const color = buy ? '#138852' : '#cd5b4c';
    const marker = svgElement('g');
    marker.append(svgElement('circle', {cx:x(index), cy:y(point.close), r:compact ? 4.5 : 8.5,
      fill:color, stroke:'#fff', 'stroke-width':1.5}));
    if (!compact) {
      const label = svgElement('text', {x:x(index), y:y(point.close)+3.1, 'text-anchor':'middle',
        fill:'#fff', 'font-size':9, 'font-weight':700});
      label.textContent = buy ? 'B' : 'S';
      marker.append(label);
    }
    svg.append(marker);
  });
  const guide = svgElement('g', {'pointer-events':'none', visibility:'hidden'});
  const guideLine = svgElement('line', {y1:pad.top, y2:height-pad.bottom, stroke:'#8ea7a1', 'stroke-width':1, 'stroke-dasharray':'3 3'});
  const priceDot = svgElement('circle', {r:5, fill:'#225c65', stroke:'#fff', 'stroke-width':1.5});
  guide.append(guideLine, priceDot);
  svg.append(guide);
  const tooltip = document.createElement('div');
  tooltip.className = 'chart-tooltip';
  tooltip.setAttribute('role', 'tooltip');
  tooltip.hidden = true;
  shell.append(svg, tooltip);
  shell.tabIndex = 0;
  let hoveredIndex = -1;

  function field(label, value) {
    const row = document.createElement('div');
    row.className = 'chart-tooltip-row';
    const caption = document.createElement('span');
    caption.textContent = label;
    const data = document.createElement('strong');
    data.textContent = value;
    row.append(caption, data);
    tooltip.append(row);
  }

  function showPoint(index) {
    if (index === hoveredIndex) return;
    hoveredIndex = index;
    const point = points[index];
    const pointX = x(index);
    const priceY = y(point.close);
    guideLine.setAttribute('x1', pointX);
    guideLine.setAttribute('x2', pointX);
    priceDot.setAttribute('cx', pointX);
    priceDot.setAttribute('cy', priceY);
    guide.setAttribute('visibility', 'visible');
    tooltip.replaceChildren();
    const heading = document.createElement('div');
    heading.className = 'chart-tooltip-date';
    heading.textContent = point.date;
    tooltip.append(heading);
    field('收盘价', amount(point.close, history.currency));
    field('MA120', amount(point.ma120, history.currency));
    field('买入线', amount(point.buy_line, history.currency));
    field('卖出线', amount(point.sell_line, history.currency));
    field('偏离 MA120', signed((point.close / point.ma120 - 1) * 100));
    if (index > 0) field('较前日', signed((point.close / points[index - 1].close - 1) * 100));
    field('模拟信号', point.signal === 'buy' ? 'B · 首次跌破买入线' : point.signal === 'sell' ? 'S · 首次突破卖出线' : '无');
    tooltip.hidden = false;
    const px = pointX * shell.clientWidth / width;
    const py = priceY * shell.clientHeight / height;
    const left = px < shell.clientWidth / 2 ? px + 14 : px - tooltip.offsetWidth - 14;
    tooltip.style.left = `${Math.max(8, Math.min(left, shell.clientWidth-tooltip.offsetWidth-8))}px`;
    tooltip.style.top = `${Math.max(8, Math.min(py-tooltip.offsetHeight-12, shell.clientHeight-tooltip.offsetHeight-8))}px`;
    shell.setAttribute('aria-label', `${point.date}，收盘价 ${number(point.close)}，MA120 ${number(point.ma120)}，模拟信号 ${point.signal || '无'}`);
  }

  function nearestIndex(event) {
    const bounds = svg.getBoundingClientRect();
    const chartX = (event.clientX-bounds.left) * width / bounds.width;
    return Math.max(0, Math.min(points.length-1, Math.round((chartX-pad.left) / plotWidth * (points.length-1))));
  }
  svg.addEventListener('pointermove', (event) => { if (event.pointerType !== 'touch') showPoint(nearestIndex(event)); });
  svg.addEventListener('pointerdown', (event) => showPoint(nearestIndex(event)));
  svg.addEventListener('pointerleave', (event) => { if (event.pointerType !== 'touch') { guide.setAttribute('visibility', 'hidden'); tooltip.hidden = true; hoveredIndex = -1; } });
  shell.onkeydown = (event) => {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
    event.preventDefault();
    const next = hoveredIndex < 0 ? points.length-1 : hoveredIndex + (event.key === 'ArrowRight' ? 1 : -1);
    showPoint(Math.max(0, Math.min(points.length-1, next)));
  };
  shell.onblur = () => { guide.setAttribute('visibility', 'hidden'); tooltip.hidden = true; hoveredIndex = -1; };
}

function renderStockHistory(history) {
  $('stockHistoryRange').textContent = `${history.window_start} 至 ${history.window_end} · 已完成日 K`;
  $('stockHistoryCount').textContent = `模拟 B ${history.buy_count} / S ${history.sell_count}`;
  $('stockEventCount').textContent = `${history.signals.length} 条`;
  drawStockChart(history);
  const body = $('stockEvents');
  body.replaceChildren();
  for (const signal of [...history.signals].reverse()) {
    const tr = document.createElement('tr');
    cell(tr, signal.date);
    cell(tr, signal.side === 'buy' ? '模拟 B' : '模拟 S', signal.side === 'buy' ? 'event-buy' : 'event-sell');
    cell(tr, amount(signal.price, history.currency));
    cell(tr, amount(signal.ma120, history.currency));
    cell(tr, amount(signal.threshold, history.currency));
    cell(tr, signal.side === 'buy' ? '首次跌破买入线' : '首次突破卖出线');
    body.append(tr);
  }
  if (!history.signals.length) {
    const tr = document.createElement('tr');
    const td = cell(tr, '这个区间没有模拟 B/S 触发点', 'empty-row');
    td.colSpan = 6;
    body.append(tr);
  }
  const input = $('stockEstimateDate');
  const previous = estimateCode === activeCode ? input.value : '';
  estimateCode = activeCode;
  input.min = history.window_start;
  input.max = history.window_end;
  input.value = previous >= input.min && previous <= input.max ? previous : input.min;
  input.disabled = false;
  loadStockEstimate();
}

function resetStockEstimate(message, error = false) {
  estimateRequest++;
  const status = $('stockEstimateMessage');
  status.textContent = message;
  status.className = `stock-estimate-message${error ? ' error' : ''}`;
  $('stockEstimateMetrics').hidden = true;
  $('stockEstimateTradeCount').textContent = '—';
  const body = $('stockEstimateTrades');
  body.replaceChildren();
  const row = document.createElement('tr');
  const content = cell(row, message, 'empty-row');
  content.colSpan = 5;
  body.append(row);
}

function renderStockEstimate(estimate) {
  if (!estimate.supported) {
    resetStockEstimate(estimate.reason || '这个区间无法测算', true);
    return;
  }
  const currency = estimate.currency;
  $('stockEstimateCapital').textContent = amount(estimate.initial_capital, currency);
  $('stockEstimateValue').textContent = amount(estimate.estimated_value, currency);
  const profit = $('stockEstimateProfit');
  profit.textContent = `${estimate.profit >= 0 ? '+' : '-'}${amount(Math.abs(estimate.profit), currency)}`;
  profit.className = estimate.profit > 0 ? 'positive' : estimate.profit < 0 ? 'negative' : '';
  const totalReturn = $('stockEstimateReturn');
  totalReturn.textContent = signed(estimate.return_pct);
  totalReturn.className = estimate.return_pct > 0 ? 'positive' : estimate.return_pct < 0 ? 'negative' : '';
  $('stockEstimatePosition').textContent = estimate.open_position ? '持仓' : '空仓';
  $('stockEstimateMetrics').hidden = false;
  $('stockEstimateMessage').textContent = `${estimate.start_date} 至 ${estimate.end_date} · 模拟买入 ${estimate.buy_count} 次、卖出 ${estimate.sell_count} 次`;
  $('stockEstimateMessage').className = 'stock-estimate-message';
  $('stockEstimateTradeCount').textContent = `${estimate.trades.length} 笔`;
  const body = $('stockEstimateTrades');
  body.replaceChildren();
  for (const trade of [...estimate.trades].reverse()) {
    const row = document.createElement('tr');
    cell(row, trade.signal_date);
    cell(row, trade.date);
    cell(row, trade.side === 'buy' ? '模拟 B' : '模拟 S', trade.side === 'buy' ? 'event-buy' : 'event-sell');
    cell(row, amount(trade.price, currency));
    cell(row, number(trade.units, 4));
    body.append(row);
  }
  if (!estimate.trades.length) {
    const row = document.createElement('tr');
    const content = cell(row, '起算日期之后没有可执行的 B/S 信号', 'empty-row');
    content.colSpan = 5;
    body.append(row);
  }
}

async function loadStockEstimate() {
  const code = activeCode;
  const start = $('stockEstimateDate').value;
  if (!code || !start) return;
  resetStockEstimate('正在测算...');
  const token = estimateRequest;
  try {
    const result = await request(`/api/ma120/estimate?code=${encodeURIComponent(code)}&start_date=${encodeURIComponent(start)}`);
    if (token === estimateRequest && activeCode === code && $('stockEstimateDate').value === start)
      renderStockEstimate(result);
  } catch (error) {
    if (token === estimateRequest && activeCode === code)
      resetStockEstimate(`测算失败：${error.message}`, true);
  }
}

async function loadStockHistory(code) {
  const token = ++historyRequest;
  resetStockEstimate('正在读取历史日 K...');
  $('stockEstimateDate').disabled = true;
  const cached = stockHistoryCache.get(code);
  if (cached) { renderStockHistory(cached); return; }
  $('stockHistoryChart').textContent = '正在加载历史日 K...';
  $('stockHistoryRange').textContent = '正在读取历史日 K';
  $('stockHistoryCount').textContent = '—';
  $('stockEventCount').textContent = '—';
  $('stockEvents').replaceChildren();
  try {
    const result = await request(`/api/ma120/history?code=${encodeURIComponent(code)}`);
    stockHistoryCache.set(code, result);
    if (token === historyRequest && activeCode === code) renderStockHistory(result);
  } catch (error) {
    if (token !== historyRequest || activeCode !== code) return;
    $('stockHistoryChart').textContent = `历史行情读取失败：${error.message}`;
    $('stockHistoryRange').textContent = '暂无可用历史数据';
    resetStockEstimate(`历史收益测算不可用：${error.message}`, true);
    const tr = document.createElement('tr');
    const td = cell(tr, `历史模拟不可用：${error.message}`, 'empty-row');
    td.colSpan = 6;
    $('stockEvents').append(tr);
  }
}

function applyRoute() {
  activeCode = routeCode();
  const detail = Boolean(activeCode);
  document.body.classList.toggle('stock-detail-mode', detail);
  $('stockOverview').hidden = detail;
  $('stockDetail').hidden = !detail;
  if (!detail) { historyRequest++; document.title = 'MA120 监控 | 投资计划'; return; }
  if (!dashboard) return;
  const item = findStock(activeCode);
  renderCurrentStock(item);
  document.title = `${item?.name || activeCode} · MA120 回溯 | 投资计划`;
  if (item) loadStockHistory(activeCode);
  else {
    historyRequest++;
    resetStockEstimate('这只股票不在当前清单中', true);
    $('stockEstimateDate').disabled = true;
    $('stockHistoryChart').textContent = '这只股票不在当前清单中';
    $('stockHistoryRange').textContent = '暂无历史数据';
    $('stockHistoryCount').textContent = '—';
    $('stockEventCount').textContent = '—';
    $('stockEvents').replaceChildren();
  }
}

function showInitialDetailError(message) {
  if (!activeCode || dashboard) return;
  $('stockDetailName').textContent = activeCode;
  $('stockDetailCode').textContent = '股票清单读取失败';
  $('stockDetailStatus').textContent = '数据待补';
  $('stockDetailStatus').className = 'stock-status unavailable';
  $('stockDetailError').textContent = message;
  $('stockDetailError').hidden = false;
  $('stockHistoryRange').textContent = '暂无可用历史数据';
  $('stockHistoryChart').textContent = `历史模拟不可用：${message}`;
  resetStockEstimate(`历史收益测算不可用：${message}`, true);
  $('stockEstimateDate').disabled = true;
  $('stockEvents').replaceChildren();
}

function render(next) {
  dashboard = next;
  renderSummary();
  renderRows();
  applyRoute();
}

async function refresh() {
  const button = $('stockRefresh');
  button.disabled = true;
  button.textContent = '正在获取...';
  refreshFeedback('正在重新读取股票日 K 和报价...');
  try {
    const result = await request('/api/ma120/refresh', true);
    stockHistoryCache.clear();
    render(result.snapshot);
    const rows = [...result.snapshot.portfolio, ...result.snapshot.watchlist];
    const latest = rows.map((row) => row.price_date).filter(Boolean).sort().at(-1);
    const summary = result.snapshot.summary;
    refreshFeedback(`已更新 ${rows.length} 只股票${latest ? `（最新行情 ${latest}）` : ''}；今日买入信号 ${summary.buy_signals}、卖出信号 ${summary.sell_signals}${summary.data_unavailable ? `，${summary.data_unavailable} 只数据待补` : ''}。只读测算，未推送或下单。`);
  } catch (error) {
    notice(`刷新失败：${error.message}`, true);
    refreshFeedback(`刷新失败：${error.message}`, true);
    showInitialDetailError(error.message);
  } finally {
    button.disabled = false;
    button.textContent = '↻ 刷新行情';
  }
}

document.querySelectorAll('[data-scope]').forEach((button) => button.addEventListener('click', () => {
  scope = button.dataset.scope;
  currentPage = 1;
  document.querySelectorAll('[data-scope]').forEach((tab) => {
    const selected = tab === button;
    tab.classList.toggle('active', selected);
    tab.setAttribute('aria-selected', String(selected));
  });
  if (dashboard) renderRows();
}));
$('stockSearch').addEventListener('input', () => { currentPage = 1; if (dashboard) renderRows(); });
$('stockFilter').addEventListener('change', () => { currentPage = 1; if (dashboard) renderRows(); });
$('stockSort').addEventListener('change', () => { currentPage = 1; if (dashboard) renderRows(); });
$('stockPageSize').addEventListener('change', () => { currentPage = 1; if (dashboard) renderRows(); });
$('stockPrevPage').addEventListener('click', () => { if (currentPage > 1) { currentPage--; renderRows(); } });
$('stockNextPage').addEventListener('click', () => { currentPage++; renderRows(); });
$('stockRefresh').addEventListener('click', refresh);
$('stockEstimateForm').addEventListener('submit', (event) => { event.preventDefault(); loadStockEstimate(); });
$('stockEstimateDate').addEventListener('change', loadStockEstimate);
$('watchlistManage').addEventListener('click', openWatchlist);
$('watchlistClose').addEventListener('click', () => $('watchlistDialog').close());
$('watchlistAdd').addEventListener('submit', (event) => {
  event.preventDefault();
  const code = $('watchlistCode').value.trim().toUpperCase();
  const name = $('watchlistName').value.trim();
  if (!code || !name) return;
  mutateWatchlist('POST', '/api/ma120/watchlist', {code, name}, `${name} 已加入关注池`);
});
$('stockBack').addEventListener('click', (event) => { event.preventDefault(); navigate(null); });
window.addEventListener('popstate', () => { applyRoute(); window.scrollTo(0, 0); });
window.addEventListener('resize', () => {
  const cached = stockHistoryCache.get(activeCode);
  if (cached && !$('stockDetail').hidden) drawStockChart(cached);
});
applyRoute();
request('/api/ma120').then((result) => { if (result.snapshot) render(result.snapshot); else refresh(); })
  .catch((error) => { notice(`读取失败：${error.message}`, true); showInitialDetailError(error.message); });
