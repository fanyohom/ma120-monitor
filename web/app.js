const $ = (id) => document.getElementById(id);
const money = (value) => `¥${Number(value || 0).toLocaleString('zh-CN', {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
const signedMoney = (value) => `${value >= 0 ? '+' : '-'}${money(Math.abs(value))}`;
const quantity = (value) => Number(value || 0).toLocaleString('zh-CN');
const statusNames = {buy:'计划买入', pause:'暂停定投', wait:'未到定投日', recorded:'本期已买入', market_closed:'无当日交易行情', blocked:'数据待补', small:'不足一手'};
let data = null;
let selectedId = null;
let activeTab = 'plans';
let currentEstimate = null;
const historyCache = new Map();
const estimateCache = new Map();

function routePlanId() {
  const match = location.pathname.match(/^\/etf\/([a-zA-Z0-9_-]+)$/);
  return match?.[1] || null;
}

function showTab(name) {
  activeTab = name;
  document.querySelectorAll('.tab').forEach((tab) => {
    const selected = tab.dataset.tab === name;
    tab.classList.toggle('active', selected);
    tab.setAttribute('aria-selected', String(selected));
  });
  $('plansPanel').hidden = name !== 'plans';
  $('historyPanel').hidden = name !== 'history';
}

function applyRoute() {
  const planId = routePlanId();
  const isDetail = Boolean(planId && data?.plans.some((plan) => plan.id === planId));
  document.body.classList.toggle('detail-mode', isDetail);
  if (isDetail) {
    selectedId = planId;
    $('plansPanel').hidden = false;
    $('historyPanel').hidden = true;
  } else {
    if (location.pathname !== '/') history.replaceState({}, '', '/');
    showTab(activeTab);
  }
  return isDetail;
}

function navigate(planId) {
  history.pushState({}, '', planId ? `/etf/${encodeURIComponent(planId)}` : '/');
  if (!planId) activeTab = 'plans';
  applyRoute();
  renderPlans();
  renderDetail();
  window.scrollTo(0, 0);
}

function cell(row, value, className = '') {
  const td = document.createElement('td');
  td.textContent = value;
  if (className) td.className = className;
  row.append(td);
  return td;
}

function emptyRow(body, columns, message) {
  const row = document.createElement('tr');
  const td = cell(row, message, 'empty-row');
  td.colSpan = columns;
  body.append(row);
}

function statusBadge(status) {
  const span = document.createElement('span');
  span.className = `status ${status || ''}`;
  span.textContent = statusNames[status] || '待刷新';
  return span;
}

function showNotice(message, error = false) {
  const notice = $('notice');
  notice.textContent = message;
  notice.className = `notice${error ? ' error' : ''}`;
  notice.hidden = !message;
}

function reasonText(reason) {
  if (reason?.startsWith('Missing valuation CSV:')) return reason.replace('Missing valuation CSV:', '缺少估值历史：');
  return reason || '—';
}

async function request(path, payload) {
  const options = payload === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)};
  const response = await fetch(path, options);
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || '请求失败');
  return result;
}

function renderPlans() {
  const body = $('plansBody');
  body.replaceChildren();
  const reportRows = data.report?.rows || [];
  $('planMeta').textContent = `${data.plans.length} 只 ETF`;
  for (const plan of data.plans) {
    const active = reportRows.find((row) => row.plan_id === plan.id && row.selected);
    const tr = document.createElement('tr');
    tr.dataset.id = plan.id;
    tr.classList.toggle('selected', plan.id === selectedId);
    const nameCell = document.createElement('td');
    const name = document.createElement('a');
    name.className = 'etf-name';
    name.href = `/etf/${encodeURIComponent(plan.id)}`;
    name.textContent = plan.name;
    name.addEventListener('click', (event) => { event.preventDefault(); event.stopPropagation(); navigate(plan.id); });
    const code = document.createElement('span');
    code.className = 'etf-code';
    code.textContent = plan.definition ? `${plan.code} · ${plan.definition.category}` : plan.code;
    nameCell.append(name, code);
    tr.append(nameCell);
    const strategyCell = document.createElement('td');
    const strategyName = document.createElement('span');
    strategyName.className = 'strategy-tag';
    strategyName.textContent = data.strategies[plan.strategy];
    strategyCell.append(strategyName);
    if (active?.market?.ma != null) {
      const maValue = document.createElement('span');
      maValue.className = 'strategy-sub';
      maValue.textContent = `MA${active.market.ma_days} ${Number(active.market.ma).toFixed(3)}`;
      strategyCell.append(maValue);
    }
    tr.append(strategyCell);
    cell(tr, active?.market ? Number(active.market.price).toFixed(3) : '—');
    cell(tr, active ? `${quantity(active.holdings)} 份` : '—');
    cell(tr, active ? money(active.action_amount) : '—', 'amount');
    const statusCell = document.createElement('td');
    statusCell.append(statusBadge(active?.status));
    tr.append(statusCell);
    cell(tr, '›', 'row-arrow');
    tr.addEventListener('click', () => navigate(plan.id));
    body.append(tr);
  }
  if (!data.plans.length) emptyRow(body, 7, '暂无计划');
}

function renderDefinition(plan) {
  const definition = plan.definition;
  $('etfDefinition').hidden = !definition;
  if (!definition) return;
  $('definitionCategory').textContent = definition.category;
  $('definitionSummary').textContent = definition.summary;
  $('definitionFund').textContent = definition.fund_name;
  $('definitionManager').textContent = definition.manager;
  $('definitionIndex').textContent = definition.index_name;
  $('definitionMarket').textContent = definition.market;
  $('definitionNote').textContent = definition.note;
  const sources = $('definitionSources');
  sources.replaceChildren();
  for (const source of definition.sources) {
    const link = document.createElement('a');
    link.href = source.url;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    link.textContent = source.label;
    sources.append(link);
  }
}

function renderDetail() {
  if (!document.body.classList.contains('detail-mode')) { $('detail').hidden = true; return; }
  const plan = data.plans.find((item) => item.id === selectedId);
  if (!plan) { $('detail').hidden = true; return; }
  $('detail').hidden = false;
  $('detailTradeBtn').disabled = data.sample;
  const rows = (data.report?.rows || []).filter((row) => row.plan_id === plan.id);
  const active = rows.find((row) => row.selected);
  $('detailName').textContent = `${plan.name} · ${plan.code}`;
  renderDefinition(plan);
  const frequency = {daily:'每日', weekly:'每周', monthly:'每月'}[plan.frequency];
  $('detailMeta').textContent = `${frequency} · 基础金额 ${money(plan.base_amount)} · 单期上限 ${money(plan.max_amount)}`;
  $('detailStatus').replaceWith(Object.assign(statusBadge(active?.status), {id:'detailStatus'}));
  $('detailPrice').textContent = active?.market ? money(active.market.price) : '—';
  $('detailQuote').textContent = active?.market ? `行情 ${active.market.quote_at.replace('T', ' ').slice(0, 19)}` : '等待行情';
  $('detailHoldings').textContent = active ? `${quantity(active.holdings)} 份` : '—';
  $('detailCost').textContent = active?.holdings ? `平均成本 ${money(active.cost)}` : '尚无持仓记录';
  $('detailReference').textContent = active ? money(active.reference_amount) : '—';
  $('detailShares').textContent = active ? `参考 ${quantity(active.reference_shares)} 份` : '刷新后查看';
  $('maLegend').textContent = active?.market?.ma_days ? `前复权 MA${active.market.ma_days}` : '前复权 MA';
  const body = $('compareBody');
  body.replaceChildren();
  for (const row of rows) {
    const tr = document.createElement('tr');
    if (row.selected) tr.className = 'active';
    cell(tr, `${data.strategies[row.strategy]}${row.selected ? ' · 生效' : ''}`, 'strategy-tag');
    cell(tr, money(row.reference_amount));
    cell(tr, `${quantity(row.reference_shares)} 份`);
    cell(tr, reasonText(row.reason));
    body.append(tr);
  }
  if (!rows.length) emptyRow(body, 4, '刷新行情后查看策略测算');
  loadPlanHistory(plan.id);
}

function svgElement(name, attributes = {}) {
  const element = document.createElementNS('http://www.w3.org/2000/svg', name);
  for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, String(value));
  return element;
}

function chartText(svg, x, y, value, anchor = 'start') {
  const label = svgElement('text', {x, y, 'text-anchor':anchor, fill:'#819095', 'font-size':11});
  label.textContent = value;
  svg.append(label);
}

function drawChart(history) {
  const shell = $('historyChart');
  shell.replaceChildren();
  const points = history.series;
  if (!points.length) { shell.textContent = '暂无已完成的历史日 K'; return; }
  const width = Math.max(shell.clientWidth, 320);
  const height = shell.clientHeight || 300;
  const pad = {left:49, right:19, top:22, bottom:32};
  const plotWidth = width - pad.left - pad.right;
  const plotHeight = height - pad.top - pad.bottom;
  const values = points.flatMap((point) => point.chart_ma == null ? [point.chart_price] : [point.chart_price, point.chart_ma]);
  const low = Math.min(...values);
  const high = Math.max(...values);
  const margin = Math.max((high - low) * .09, high * .025);
  const min = low - margin;
  const max = high + margin;
  const x = (index) => pad.left + index / Math.max(points.length - 1, 1) * plotWidth;
  const y = (value) => pad.top + (max - value) / (max - min) * plotHeight;
  const svg = svgElement('svg', {viewBox:`0 0 ${width} ${height}`, width:'100%', height:'100%', 'aria-hidden':'true'});
  for (let step = 0; step <= 4; step++) {
    const value = min + (max - min) * step / 4;
    const lineY = y(value);
    svg.append(svgElement('line', {x1:pad.left, y1:lineY, x2:width-pad.right, y2:lineY, stroke:'#e8eeeb', 'stroke-width':1}));
    chartText(svg, pad.left - 8, lineY + 4, value.toFixed(2), 'end');
  }
  for (const index of [0, Math.floor((points.length - 1) / 2), points.length - 1]) chartText(svg, x(index), height - 7, points[index].date, index === 0 ? 'start' : index === points.length - 1 ? 'end' : 'middle');
  const pricePath = points.map((point, index) => `${index ? 'L' : 'M'}${x(index).toFixed(1)},${y(point.chart_price).toFixed(1)}`).join(' ');
  svg.append(svgElement('path', {d:pricePath, fill:'none', stroke:'#225c65', 'stroke-width':2, 'stroke-linejoin':'round'}));
  let maPath = '';
  points.forEach((point, index) => { if (point.chart_ma != null) maPath += `${maPath ? 'L' : 'M'}${x(index).toFixed(1)},${y(point.chart_ma).toFixed(1)} `; });
  if (maPath) svg.append(svgElement('path', {d:maPath, fill:'none', stroke:'#d49a3b', 'stroke-width':1.8, 'stroke-dasharray':'5 4'}));

  const dateIndex = new Map(points.map((point, index) => [point.date, index]));
  const markers = [
    ...history.reference_buys.map((point) => ({...point, kind:'reference', label:'B'})),
    ...history.trades.filter((trade) => trade.on_chart).map((trade) => ({...trade, kind:trade.side, label:trade.side === 'buy' ? 'B' : 'S'})),
  ];
  const colors = {reference:'#168573', buy:'#138852', sell:'#cd5b4c'};
  const compactReference = history.reference_buys.length > 30;
  for (const marker of markers) {
    const index = dateIndex.get(marker.date);
    if (index === undefined) continue;
    const close = points[index].chart_price;
    const group = svgElement('g');
    const compact = marker.kind === 'reference' && compactReference;
    group.append(svgElement('circle', {cx:x(index), cy:y(close), r:compact ? 4.5 : 9,
      fill:marker.kind === 'reference' ? '#fff' : colors[marker.kind], stroke:colors[marker.kind], 'stroke-width':1.8}));
    if (!compact) {
      const label = svgElement('text', {x:x(index), y:y(close)+3.4, 'text-anchor':'middle',
        fill:marker.kind === 'reference' ? colors[marker.kind] : '#fff', 'font-size':9, 'font-weight':700});
      label.textContent = marker.label;
      group.append(label);
    }
    svg.append(group);
  }

  const guide = svgElement('g', {'pointer-events':'none', visibility:'hidden'});
  const guideLine = svgElement('line', {y1:pad.top, y2:height-pad.bottom, stroke:'#9aaca8', 'stroke-width':1, 'stroke-dasharray':'3 3'});
  const priceDot = svgElement('circle', {r:5, fill:'#225c65', stroke:'#fff', 'stroke-width':1.5});
  const maDot = svgElement('circle', {r:4, fill:'#d49a3b', stroke:'#fff', 'stroke-width':1.5});
  guide.append(guideLine, priceDot, maDot);
  svg.append(guide);

  const tooltip = document.createElement('div');
  tooltip.className = 'chart-tooltip';
  tooltip.setAttribute('role', 'tooltip');
  tooltip.hidden = true;
  shell.append(svg, tooltip);
  shell.tabIndex = 0;
  let hoveredIndex = -1;

  function tooltipField(label, value) {
    const row = document.createElement('div');
    row.className = 'chart-tooltip-row';
    const name = document.createElement('span');
    name.textContent = label;
    const number = document.createElement('strong');
    number.textContent = value;
    row.append(name, number);
    tooltip.append(row);
  }

  function showPoint(index) {
    if (index === hoveredIndex) return;
    hoveredIndex = index;
    const point = points[index];
    const pointX = x(index);
    const priceY = y(point.chart_price);
    guideLine.setAttribute('x1', pointX);
    guideLine.setAttribute('x2', pointX);
    priceDot.setAttribute('cx', pointX);
    priceDot.setAttribute('cy', priceY);
    maDot.setAttribute('cx', pointX);
    maDot.setAttribute('cy', point.chart_ma == null ? priceY : y(point.chart_ma));
    maDot.style.display = point.chart_ma == null ? 'none' : '';
    guide.setAttribute('visibility', 'visible');

    tooltip.replaceChildren();
    const heading = document.createElement('div');
    heading.className = 'chart-tooltip-date';
    heading.textContent = point.date;
    tooltip.append(heading);
    tooltipField('前复权收盘', `¥${point.chart_price.toFixed(3)}`);
    tooltipField('当日场内收盘', `¥${point.price.toFixed(3)}`);
    tooltipField(`MA${history.ma_days}`, point.chart_ma == null ? '—' : `¥${point.chart_ma.toFixed(3)}`);
    const deviation = point.chart_ma == null ? null : (point.chart_price / point.chart_ma - 1) * 100;
    tooltipField('偏离均线', deviation == null ? '—' : `${deviation >= 0 ? '+' : ''}${deviation.toFixed(2)}%`);
    const prior = points[index - 1];
    const daily = prior ? (point.chart_price / prior.chart_price - 1) * 100 : null;
    tooltipField('当日涨跌', daily == null ? '—' : `${daily >= 0 ? '+' : ''}${daily.toFixed(2)}%`);
    const reference = history.reference_buys.find((buy) => buy.date === point.date);
    if (reference) tooltipField('定投参考 B', `${money(reference.amount)} · ${quantity(reference.shares)} 份`);
    for (const trade of history.trades.filter((fill) => fill.date === point.date && fill.on_chart)) {
      tooltipField(`成交 ${trade.side === 'buy' ? 'B' : 'S'}`, `¥${trade.price.toFixed(3)} · ${quantity(trade.shares)} 份`);
    }
    tooltip.hidden = false;
    const px = pointX * shell.clientWidth / width;
    const py = priceY * shell.clientHeight / height;
    const left = px < shell.clientWidth / 2 ? px + 14 : px - tooltip.offsetWidth - 14;
    tooltip.style.left = `${Math.max(8, Math.min(left, shell.clientWidth - tooltip.offsetWidth - 8))}px`;
    tooltip.style.top = `${Math.max(8, Math.min(py - tooltip.offsetHeight - 12, shell.clientHeight - tooltip.offsetHeight - 8))}px`;
    shell.setAttribute('aria-label', `${point.date}，前复权收盘 ${point.chart_price.toFixed(3)}，MA${history.ma_days} ${point.chart_ma?.toFixed(3) ?? '无数据'}`);
  }

  function hidePoint() {
    hoveredIndex = -1;
    guide.setAttribute('visibility', 'hidden');
    tooltip.hidden = true;
  }

  function nearestIndex(event) {
    const bounds = svg.getBoundingClientRect();
    const chartX = (event.clientX - bounds.left) * width / bounds.width;
    return Math.max(0, Math.min(points.length - 1,
      Math.round((chartX - pad.left) / plotWidth * (points.length - 1))));
  }

  svg.addEventListener('pointermove', (event) => {
    if (event.pointerType !== 'touch') showPoint(nearestIndex(event));
  });
  svg.addEventListener('pointerdown', (event) => showPoint(nearestIndex(event)));
  svg.addEventListener('pointerleave', (event) => { if (event.pointerType !== 'touch') hidePoint(); });
  shell.onkeydown = (event) => {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
    event.preventDefault();
    const next = hoveredIndex < 0 ? points.length - 1 : hoveredIndex + (event.key === 'ArrowRight' ? 1 : -1);
    showPoint(Math.max(0, Math.min(points.length - 1, next)));
  };
  shell.onblur = hidePoint;
}

function displayedHistory(history) {
  if (currentEstimate?.supported && currentEstimate.plan_id === selectedId &&
      currentEstimate.start_date === $('estimateStart').value) {
    return {...history, reference_buys:currentEstimate.buys};
  }
  return history;
}

function renderPlanHistory(history) {
  const shown = displayedHistory(history);
  $('chartRange').textContent = `${history.window_start} 至 ${history.window_end} · 已完成日 K`;
  const buys = history.trades.filter((trade) => trade.side === 'buy').length;
  const sells = history.trades.filter((trade) => trade.side === 'sell').length;
  $('chartCount').textContent = `定投参考 B ${shown.reference_buys.length} · 成交 B ${buys} / S ${sells}`;
  if (history.replay_supported) {
    $('chartNote').textContent = `图中价格和 MA${history.ma_days} 均为前复权并换算到最新价格尺度；每个历史日的均线只用之前 ${history.ma_days} 根已完成日 K。定投参考 B 未计费用或真实持仓；成交点按日期落在前复权收盘线，实际成交价以事件表为准。`;
  } else {
    $('chartNote').textContent = '图中价格和均线均为前复权并换算到最新价格尺度。当前策略没有可验证的定投参考 B；仅标记已登记的成交 B/S，实际成交价以事件表为准。';
  }
  drawChart(shown);
  const events = [
    ...shown.reference_buys.map((point) => ({date:point.date, type:'定投参考 B', source:'MA 回放', price:point.price, shares:point.shares, detail:point.reason})),
    ...history.trades.map((trade) => ({date:trade.date, type:trade.side === 'buy' ? '成交 B' : '成交 S', source:'成交账本', price:trade.price,
      shares:trade.shares, detail:`${trade.fill_id}${trade.on_chart ? '' : ' · 图表范围外'}`})),
  ].sort((a, b) => b.date.localeCompare(a.date) || a.type.localeCompare(b.type));
  $('eventCount').textContent = `${events.length} 条`;
  const body = $('eventsBody');
  body.replaceChildren();
  for (const event of events.slice(0, 40)) {
    const tr = document.createElement('tr');
    cell(tr, event.date);
    cell(tr, event.type, event.type.endsWith('S') ? 'event-sell' : 'event-buy');
    cell(tr, event.source);
    cell(tr, money(event.price));
    cell(tr, `${quantity(event.shares)} 份`);
    cell(tr, event.detail);
    body.append(tr);
  }
  if (!events.length) emptyRow(body, 6, '当前窗口没有买卖事件');
}

function renderEstimate(result) {
  $('estimateMetrics').hidden = !result.supported;
  $('estimateMessage').hidden = result.supported && result.buy_count > 0;
  if (!result.supported) {
    $('estimateMessage').textContent = result.reason;
    return;
  }
  $('estimateMessage').textContent = '所选区间内没有可买整手的计划日';
  $('estimateInvested').textContent = money(result.invested);
  $('estimateValue').textContent = money(result.estimated_value);
  $('estimateProfit').textContent = signedMoney(result.profit);
  $('estimateProfit').className = result.profit >= 0 ? 'positive' : 'negative';
  $('estimateReturn').textContent = result.return_pct == null ? '—' : `${result.return_pct >= 0 ? '+' : ''}${result.return_pct.toFixed(2)}%`;
  $('estimateReturn').className = result.return_pct == null ? '' : result.return_pct >= 0 ? 'positive' : 'negative';
  $('estimateBuys').textContent = `${result.buy_count} 次整手买入`;
}

async function loadEstimate(planId, startDate) {
  const key = `${planId}|${startDate}`;
  $('estimateMessage').textContent = '正在计算历史收益...';
  $('estimateMessage').hidden = false;
  $('estimateMetrics').hidden = true;
  try {
    const result = estimateCache.get(key) || await request(`/api/estimate?plan_id=${encodeURIComponent(planId)}&start_date=${startDate}`);
    estimateCache.set(key, result);
    if (selectedId !== planId || $('estimateStart').value !== startDate || !document.body.classList.contains('detail-mode')) return;
    currentEstimate = {...result, plan_id:planId};
    renderEstimate(result);
    const history = historyCache.get(planId);
    if (history) renderPlanHistory(history);
  } catch (error) {
    if (selectedId === planId && $('estimateStart').value === startDate) {
      $('estimateMessage').textContent = `估算失败：${error.message}`;
      $('estimateMetrics').hidden = true;
    }
  }
}

function prepareEstimate(planId, history) {
  const input = $('estimateStart');
  if (!history.replay_supported || !history.simulation_start) {
    input.disabled = true;
    input.value = '';
    $('estimateWindow').textContent = '当前策略暂无可回放的收益区间';
    renderEstimate({supported:false, reason:'当前仅支持 MA 定投的历史收益估算'});
    return;
  }
  input.min = history.simulation_start;
  input.max = history.window_end;
  if (input.dataset.planId !== planId || input.value < input.min || input.value > input.max) {
    input.value = input.min;
  }
  input.dataset.planId = planId;
  input.disabled = false;
  $('estimateWindow').textContent = `可选 ${input.min} 至 ${input.max}`;
  loadEstimate(planId, input.value);
}

async function loadPlanHistory(planId) {
  if (!data?.report) {
    $('historyChart').textContent = '刷新行情后查看历史回放';
    $('estimateStart').disabled = true;
    return;
  }
  if (historyCache.has(planId)) {
    renderPlanHistory(historyCache.get(planId));
    prepareEstimate(planId, historyCache.get(planId));
    return;
  }
  $('historyChart').textContent = '正在加载历史行情...';
  $('chartRange').textContent = '正在读取历史日 K';
  $('chartCount').textContent = '—';
  $('eventsBody').replaceChildren();
  emptyRow($('eventsBody'), 6, '正在加载历史记录...');
  try {
    const history = await request(`/api/history?plan_id=${encodeURIComponent(planId)}`);
    historyCache.set(planId, history);
    if (selectedId === planId && document.body.classList.contains('detail-mode')) {
      renderPlanHistory(history);
      prepareEstimate(planId, history);
    }
  } catch (error) {
    if (selectedId === planId) {
      $('historyChart').textContent = `历史行情读取失败：${error.message}`;
      $('chartRange').textContent = '暂无历史数据';
      $('eventsBody').replaceChildren();
      emptyRow($('eventsBody'), 6, '无法生成回溯事件');
    }
  }
}

function renderHistory() {
  const history = $('historyBody');
  history.replaceChildren();
  for (const report of data.history) {
    const tr = document.createElement('tr');
    cell(tr, report.generated_at.replace('T', ' ').slice(0, 19));
    cell(tr, money(report.total_action_amount), 'amount');
    cell(tr, report.run_id.slice(0, 12));
    history.append(tr);
  }
  if (!data.history.length) emptyRow(history, 3, '暂无运行记录');
  const fills = $('fillsBody');
  fills.replaceChildren();
  for (const fill of data.fills) {
    const tr = document.createElement('tr');
    cell(tr, fill.trade_date);
    cell(tr, data.plans.find((plan) => plan.id === fill.plan_id)?.name || fill.plan_id);
    cell(tr, fill.side === 'buy' ? '买入' : '卖出');
    cell(tr, `${quantity(fill.shares)} 份`);
    cell(tr, money(fill.price));
    cell(tr, money(fill.fee));
    cell(tr, fill.fill_id);
    fills.append(tr);
  }
  if (!data.fills.length) emptyRow(fills, 7, '暂无成交记录');
}

function planFrequency(plan) {
  if (plan.frequency === 'daily') return '每日';
  if (plan.frequency === 'weekly') return `每周${['一', '二', '三', '四', '五'][plan.weekday] || ''}`;
  return `每月 ${plan.monthday || '—'} 日`;
}

function openPlanDialog() {
  if (!data) return;
  $('planSource').textContent = data.sample ? '示例配置 · etf_plans.example.toml' : '本地配置 · etf_plans.toml';
  $('planModeNote').textContent = data.sample
    ? '当前金额、策略和持仓为示例。刷新会展示最新测算，但不保存运行记录或成交，也不会下单。个人计划需先在项目根目录建立并编辑 etf_plans.toml。'
    : '刷新会保存本次建议；只有登记真实成交后才更新持仓与本期已买入状态。页面不会下单。';
  const body = $('planSettingsBody');
  body.replaceChildren();
  for (const plan of data.plans) {
    const tr = document.createElement('tr');
    cell(tr, `${plan.name} · ${plan.code}`);
    cell(tr, data.strategies[plan.strategy]);
    cell(tr, planFrequency(plan));
    cell(tr, money(plan.base_amount));
    cell(tr, money(plan.max_amount));
    body.append(tr);
  }
  $('planDialog').showModal();
}

function refreshResultMessage(next) {
  const rows = (next.report?.rows || []).filter((row) => row.selected);
  const dates = rows.map((row) => row.market?.quote_at?.slice(0, 10)).filter(Boolean).sort();
  const latest = dates.at(-1);
  const buying = rows.filter((row) => row.status === 'buy').length;
  const blocked = rows.filter((row) => row.status === 'blocked').length;
  let result;
  if (buying) result = `${buying} 只 ETF 生成本期买入建议`;
  else if (rows.length && rows.every((row) => row.status === 'market_closed')) result = '当前无当日交易行情，本期没有可执行买入';
  else if (blocked) result = `${blocked} 只生效计划的数据待补`;
  else result = '本期没有新增买入建议';
  return `已重新获取行情${latest ? `（最新报价 ${latest}）` : ''}；${result}。${next.sample ? '示例运行不留档' : '建议已留档'}，未下单。`;
}

function showRefreshFeedback(message, error = false) {
  const feedback = $('refreshFeedback');
  feedback.textContent = message;
  feedback.className = `refresh-feedback${error ? ' error' : ''}`;
  feedback.hidden = !message;
}

function render(next) {
  data = next;
  if (!selectedId || !data.plans.some((plan) => plan.id === selectedId)) selectedId = data.plans[0]?.id;
  applyRoute();
  $('modeBadge').textContent = data.sample ? '示例计划 ⌄' : '本地计划 ⌄';
  $('modeBadge').classList.toggle('sample', data.sample);
  $('modeBadge').disabled = false;
  $('tradeBtn').disabled = data.sample;
  const rows = (data.report?.rows || []).filter((row) => row.selected);
  $('planCount').textContent = data.plans.length;
  $('actionAmount').textContent = data.report ? money(data.report.total_action_amount) : '—';
  $('buyCount').textContent = data.report ? rows.filter((row) => row.status === 'buy').length : '—';
  $('blockedCount').textContent = data.report ? rows.filter((row) => row.status === 'blocked').length : '—';
  $('updatedAt').textContent = data.report ? `最近测算 ${data.report.generated_at.replace('T', ' ').slice(0, 19)} · 建议未下单` : '尚无测算快照';
  showNotice(data.sample ? '正在使用四只 ETF 的示例计划。行情可以查看；如需保存运行记录或登记真实成交，请在项目根目录建立 etf_plans.toml。' : '');
  renderPlans();
  renderDetail();
  renderHistory();
  const select = $('tradePlan');
  select.replaceChildren();
  for (const plan of data.plans) {
    const option = document.createElement('option');
    option.value = plan.id;
    option.textContent = `${plan.name} (${plan.code})`;
    select.append(option);
  }
}

async function refresh() {
  const button = $('refreshBtn');
  button.disabled = true;
  button.textContent = '正在获取...';
  showRefreshFeedback('正在获取行情并重新计算建议...');
  try {
    const next = await request('/api/refresh', {});
    historyCache.clear();
    estimateCache.clear();
    currentEstimate = null;
    render(next);
    showRefreshFeedback(refreshResultMessage(next));
  }
  catch (error) {
    showNotice(`刷新失败：${error.message}`, true);
    showRefreshFeedback(`刷新失败：${error.message}`, true);
  }
  finally { button.disabled = false; button.textContent = '↻ 刷新行情'; }
}

document.querySelectorAll('.tab').forEach((button) => button.addEventListener('click', () => showTab(button.dataset.tab)));
$('backBtn').addEventListener('click', () => navigate(null));
$('refreshBtn').addEventListener('click', refresh);
$('modeBadge').addEventListener('click', openPlanDialog);
$('closePlanDialog').addEventListener('click', () => $('planDialog').close());
$('donePlanDialog').addEventListener('click', () => $('planDialog').close());
$('planDialog').addEventListener('click', (event) => { if (event.target === $('planDialog')) $('planDialog').close(); });
function openTradeDialog() {
  $('tradeError').hidden = true;
  $('tradePlan').value = selectedId;
  const parts = new Intl.DateTimeFormat('en-US', {timeZone:'Asia/Shanghai', year:'numeric', month:'2-digit', day:'2-digit'}).formatToParts(new Date());
  const datePart = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  $('tradeDate').value = `${datePart.year}-${datePart.month}-${datePart.day}`;
  $('tradeDialog').showModal();
}
$('tradeBtn').addEventListener('click', openTradeDialog);
$('detailTradeBtn').addEventListener('click', openTradeDialog);
$('estimateStart').addEventListener('change', () => {
  const input = $('estimateStart');
  if (input.value) loadEstimate(selectedId, input.value);
});
$('closeDialog').addEventListener('click', () => $('tradeDialog').close());
$('cancelTrade').addEventListener('click', () => $('tradeDialog').close());
$('tradeForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector('button[type=submit]');
  const values = Object.fromEntries(new FormData(form));
  values.shares = Number(values.shares);
  values.price = Number(values.price);
  values.fee = Number(values.fee);
  button.disabled = true;
  try {
    await request('/api/trades', values);
    $('tradeDialog').close();
    form.reset();
    await refresh();
    if (!document.body.classList.contains('detail-mode')) showTab('history');
  } catch (error) {
    $('tradeError').textContent = error.message;
    $('tradeError').hidden = false;
  } finally { button.disabled = false; }
});

request('/api/dashboard').then((next) => { render(next); if (!next.report) refresh(); })
  .catch((error) => showNotice(`读取失败：${error.message}`, true));
window.addEventListener('popstate', () => {
  if (!data) return;
  applyRoute();
  renderPlans();
  renderDetail();
  window.scrollTo(0, 0);
});
window.addEventListener('resize', () => {
  const history = historyCache.get(selectedId);
  if (history && !$('detail').hidden) drawChart(displayedHistory(history));
});
