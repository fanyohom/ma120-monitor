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
const chartViews = new Map();

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
  renderTradeAlerts();
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
    if (plan.strategy_reason) {
      const reason = document.createElement('span');
      reason.className = 'plan-strategy-reason';
      reason.textContent = plan.strategy_reason;
      strategyCell.append(reason);
    }
    if (plan.strategy === 'valuation' && active?.valuation) {
      const valuationValue = document.createElement('span');
      valuationValue.className = 'strategy-sub';
      valuationValue.textContent = `PE ${Number(active.valuation.pe_ttm).toFixed(2)} · 分位 ${Number(active.valuation.percentile).toFixed(2)}%`;
      strategyCell.append(valuationValue);
    } else if (plan.strategy === 'ma' && active?.market?.ma != null) {
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

function strategyDescriptions() {
  const settings = data.strategy_settings;
  const maDays = settings.ma_days;
  const low = settings.buy_below;
  const high = settings.high_at;
  return [
    {key:'valuation', title:'估值定投', basis:'比较跟踪指数 PE-TTM 在自身历史中的分位', action:`低于 ${low}% 分位投入；${low}% 至低于 ${high}% 暂停；达到 ${high}% 后高估观察，不自动卖出。`, note:'需真实指数 PE 历史；分位不是 PE 倍数。'},
    {key:'ma', title:'均线定投', basis:`比较场内价格与此前 ${maDays} 个已完成交易日的 MA${maDays}`, action:'低于均线时按偏离档位多投，高于时少投；调节的是每期金额，不是触线即买卖。', note:'比较口径：现价 / 均线 − 1'},
    {key:'drawdown', title:'涨跌幅定投', basis:'比较当前价格与持仓平均成本', action:'浮亏按档多投、浮盈按档少投；没有持仓时首期按基础金额。', note:'历史回溯从零持仓起算，后续用模拟买入的平均成本；不是单日涨跌。'},
  ];
}

function renderStrategyGuide() {
  const container = $('strategyGuideMethods');
  container.replaceChildren();
  for (const strategy of strategyDescriptions()) {
    const count = data.plans.filter((plan) => plan.strategy === strategy.key).length;
    const method = document.createElement('div');
    method.className = `strategy-method${count ? ' active' : ''}`;
    const heading = document.createElement('div');
    heading.className = 'strategy-method-head';
    const name = document.createElement('h3');
    name.textContent = strategy.title;
    const state = document.createElement('span');
    state.textContent = count ? `${count} 只生效` : '备选对比';
    heading.append(name, state);
    const basis = document.createElement('p');
    basis.className = 'strategy-basis';
    basis.textContent = strategy.basis;
    const action = document.createElement('p');
    action.className = 'strategy-action';
    action.textContent = strategy.action;
    const note = document.createElement('small');
    note.textContent = strategy.note;
    method.append(heading, basis, action, note);
    container.append(method);
  }
  $('strategyGuideNote').textContent = data.sample && data.plans.every((plan) => plan.strategy === 'ma')
    ? `${data.plans.length} 只示例 ETF 当前均选用 MA${data.strategy_settings.ma_days}；其余方法只供比较，不叠加预算。`
    : '每只 ETF 只由选中的一种策略计入本期预算；其余方法仅供比较。';
}

function renderDetailStrategy(plan, active) {
  const strategy = strategyDescriptions().find((item) => item.key === plan.strategy);
  const container = $('detailStrategyLead');
  container.replaceChildren();
  const title = document.createElement('strong');
  title.textContent = `当前生效 · ${strategy.title}`;
  const summary = document.createElement('span');
  summary.textContent = `${strategy.basis}。${strategy.action}`;
  container.append(title, summary);
  if (plan.strategy_reason) {
    const reason = document.createElement('p');
    reason.className = 'detail-strategy-reason';
    reason.textContent = `选用原因：${plan.strategy_reason}`;
    container.append(reason);
  }
  if (plan.strategy === 'valuation' && active?.status === 'blocked' && !active.valuation) {
    const warning = document.createElement('small');
    warning.textContent = '当前缺少有效指数 PE 历史，本期不会生成估值买入预算。';
    container.append(warning);
  }
}

function renderValuationSnapshot(plan, active) {
  const snapshot = $('valuationSnapshot');
  snapshot.hidden = plan.strategy !== 'valuation';
  if (snapshot.hidden) return;
  const valuation = active?.valuation;
  $('valuationPe').textContent = valuation ? Number(valuation.pe_ttm).toFixed(2) : '—';
  $('valuationPercentile').textContent = valuation ? `${Number(valuation.percentile).toFixed(2)}%` : '—';
  $('valuationDate').textContent = valuation?.date || '—';
  $('valuationWindow').textContent = valuation
    ? `按 ${valuation.start_date} 至 ${valuation.date} 的 ${quantity(valuation.samples)} 条指数估值计算历史分位；指数 PE 与 ETF 场内价格不是同一指标。`
    : '刷新数据后查看指数估值；缺少有效数据时不会产生估值买入提示。';
}

function renderDetail() {
  if (!document.body.classList.contains('detail-mode')) { $('detail').hidden = true; return; }
  const plan = data.plans.find((item) => item.id === selectedId);
  if (!plan) { $('detail').hidden = true; return; }
  $('detail').hidden = false;
  const rows = (data.report?.rows || []).filter((row) => row.plan_id === plan.id);
  const active = rows.find((row) => row.selected);
  $('detailName').textContent = `${plan.name} · ${plan.code}`;
  renderDetailStrategy(plan, active);
  renderValuationSnapshot(plan, active);
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
  const maLabel = active?.market?.ma_days ? `MA${active.market.ma_days}` : 'MA';
  $('maLegend').textContent = `前复权 ${maLabel}${plan.strategy === 'ma' ? '' : '（对照）'}`;
  $('referenceLegend').hidden = false;
  $('referenceLegendLabel').textContent = `${data.strategies[plan.strategy]}模拟 B`;
  $('chartTitle').textContent = `${data.strategies[plan.strategy]} · 历史价格与 B/S 点`;
  $('historyChart').setAttribute('aria-label', `${plan.name}前复权历史价格、${data.strategies[plan.strategy]}模拟买入与实际成交点`);
  $('estimateDisclosure').textContent = '所选起算日从零模拟持仓，按历史收盘价整手买入；不模拟卖出、手续费、税费或滑点。实际成交独立显示，不参与收益计算；回溯结果不等于实际账户收益或未来预测。';
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
  if (!rows.length) emptyRow(body, 4, '刷新数据后查看策略测算');
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
    if (point.valuation) {
      tooltipField('可用估值日期', point.valuation.date);
      tooltipField('指数 PE-TTM', Number(point.valuation.pe_ttm).toFixed(2));
      tooltipField('历史估值分位', `${Number(point.valuation.percentile).toFixed(2)}%`);
    } else if (point.valuation_error) {
      tooltipField('估值数据', reasonText(point.valuation_error));
    }
    const prior = points[index - 1];
    const daily = prior ? (point.chart_price / prior.chart_price - 1) * 100 : null;
    tooltipField('当日涨跌', daily == null ? '—' : `${daily >= 0 ? '+' : ''}${daily.toFixed(2)}%`);
    const reference = history.reference_buys.find((buy) => buy.date === point.date);
    if (reference) {
      tooltipField('模拟 B', `${money(reference.amount)} · ${quantity(reference.shares)} 份`);
      if (data.plans.find((plan) => plan.id === selectedId)?.strategy === 'drawdown') {
        tooltipField('模拟持仓依据', reference.reason);
      }
    }
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

function chartWindow(history) {
  const total = history.series.length;
  const view = chartViews.get(selectedId) || {mode:'1y', end:total};
  const size = Math.min(total, view.mode === 'all' ? total : view.mode === '3y' ? 720 : 240);
  const start = Math.max(0, Math.min(total - size, view.end - size));
  const end = start + size;
  chartViews.set(selectedId, {...view, end});
  return {start, end, size, total, mode:view.mode};
}

function renderChartControls(history, window) {
  document.querySelectorAll('[data-chart-period]').forEach((button) => {
    const active = button.dataset.chartPeriod === window.mode;
    button.classList.toggle('active', active);
    button.setAttribute('aria-pressed', String(active));
  });
  $('chartPrevious').disabled = window.start === 0;
  $('chartNext').disabled = window.end === window.total;
  const slider = $('chartPosition');
  slider.max = String(window.total - window.size);
  slider.value = String(window.start);
  slider.disabled = window.total <= window.size;
  const first = history.series[window.start]?.date || '';
  const last = history.series[window.end - 1]?.date || '';
  slider.setAttribute('aria-valuetext', `${first} 至 ${last}`);
  $('chartEarliest').textContent = history.series[0]?.date.slice(0, 7) || '—';
  $('chartLatest').textContent = history.series.at(-1)?.date.slice(0, 7) || '—';
}

function renderPlanChart(history) {
  const shown = displayedHistory(history);
  const window = chartWindow(history);
  renderChartControls(history, window);
  const visibleSeries = shown.series.slice(window.start, window.end);
  const firstDate = visibleSeries[0]?.date;
  const lastDate = visibleSeries.at(-1)?.date;
  const visibleDates = new Set(visibleSeries.map((point) => point.date));
  const visibleBuys = shown.reference_buys.filter((point) => visibleDates.has(point.date));
  const visibleTrades = history.trades.filter((trade) => visibleDates.has(trade.date));
  const chartHistory = {...shown, series:visibleSeries, reference_buys:visibleBuys,
                        trades:visibleTrades.map((trade) => ({...trade, on_chart:true}))};
  $('chartRange').textContent = firstDate ? `${firstDate} 至 ${lastDate} · 已完成日 K` : '暂无历史日 K';
  const buys = visibleTrades.filter((trade) => trade.side === 'buy').length;
  const sells = visibleTrades.filter((trade) => trade.side === 'sell').length;
  const plan = data.plans.find((item) => item.id === selectedId);
  const strategyName = data.strategies[plan.strategy];
  $('chartCount').textContent = `${history.replay_supported ? `模拟 B ${visibleBuys.length} · ` : ''}成交 B ${buys} / S ${sells}`;
  if (history.replay_supported) {
    $('chartNote').textContent = `图中价格和 MA${history.ma_days} 均为前复权并换算到最新价格尺度；MA 仅在均线策略中参与判断。模拟 B 按${strategyName}和所选起算日回放，从零持仓开始；成交 B/S 来自独立账本，实际成交价以事件表为准。`;
  } else {
    $('chartNote').textContent = `图中价格和 MA${history.ma_days} 均为前复权并换算到最新价格尺度；${reasonText(history.replay_reason || '当前没有可用的历史回放数据')}。仅标记已登记的成交 B/S，实际成交价以事件表为准。`;
  }
  $('referenceLegend').hidden = !history.replay_supported;
  drawChart(chartHistory);
}

function renderPlanHistory(history) {
  const shown = displayedHistory(history);
  const plan = data.plans.find((item) => item.id === selectedId);
  const strategyName = data.strategies[plan.strategy];
  const historyDates = new Set(history.series.map((point) => point.date));
  renderPlanChart(history);
  const events = [
    ...shown.reference_buys.map((point) => ({date:point.date, type:'模拟 B', source:`${strategyName}回放`, price:point.price, shares:point.shares, detail:point.reason})),
    ...history.trades.map((trade) => ({date:trade.date, type:trade.side === 'buy' ? '成交 B' : '成交 S', source:'成交账本', price:trade.price,
      shares:trade.shares, detail:`${trade.fill_id}${historyDates.has(trade.date) ? '' : ' · 行情覆盖外'}`})),
  ].sort((a, b) => b.date.localeCompare(a.date) || a.type.localeCompare(b.type));
  $('eventCount').textContent = `${events.length} 条`;
  const body = $('eventsBody');
  body.replaceChildren();
  for (const event of events) {
    const tr = document.createElement('tr');
    cell(tr, event.date);
    cell(tr, event.type, event.type.endsWith('S') ? 'event-sell' : 'event-buy');
    cell(tr, event.source);
    cell(tr, money(event.price));
    cell(tr, `${quantity(event.shares)} 份`);
    cell(tr, event.detail);
    body.append(tr);
  }
  if (!events.length) emptyRow(body, 6, '完整回溯区间没有买卖事件');
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
  $('estimateProfit').textContent = result.buy_count ? signedMoney(result.profit) : '—';
  $('estimateProfit').className = result.buy_count ? result.profit >= 0 ? 'positive' : 'negative' : '';
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
    renderEstimate({supported:false, reason:reasonText(history.replay_reason || '没有可用的历史回放数据')});
    return;
  }
  input.min = history.simulation_start;
  input.max = history.window_end;
  if (input.dataset.planId !== planId || input.value < input.min || input.value > input.max) {
    const visibleStart = history.series[Math.max(0, history.series.length - 240)]?.date;
    input.value = visibleStart && visibleStart > input.min ? visibleStart : input.min;
  }
  input.dataset.planId = planId;
  input.disabled = false;
  $('estimateWindow').textContent = `可选 ${input.min} 至 ${input.max}`;
  loadEstimate(planId, input.value);
}

async function loadPlanHistory(planId) {
  if (!data?.report) {
    $('historyChart').textContent = '刷新数据后查看历史回放';
    $('estimateStart').disabled = true;
    $('chartPrevious').disabled = true;
    $('chartNext').disabled = true;
    $('chartPosition').disabled = true;
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
  $('chartPrevious').disabled = true;
  $('chartNext').disabled = true;
  $('chartPosition').disabled = true;
  $('chartEarliest').textContent = '—';
  $('chartLatest').textContent = '—';
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
    ? '当前金额、策略和持仓为示例。刷新会展示最新测算，但不保存运行记录，也不会下单。个人计划需先在项目根目录建立并编辑 etf_plans.toml。'
    : '刷新会保存本次建议；页面只展示策略提示，不会下单或自动同步真实成交。';
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
  else if (rows.length && rows.every((row) => row.status === 'market_closed')) result = '当前无当日交易行情，本期无买入提示';
  else if (blocked) result = `${blocked} 只生效计划的数据待补`;
  else result = '本期没有新增买入建议';
  return `已重新获取数据${latest ? `（最新报价 ${latest}）` : ''}；${result}。${next.sample ? '示例运行不留档' : '建议已留档'}，未下单。`;
}

function valuationUpdateWarning(next) {
  const warnings = next.valuation_update_warnings;
  return Array.isArray(warnings) && warnings.length ? `估值数据更新提醒：${warnings.join('；')}` : '';
}

function showRefreshFeedback(message, error = false) {
  const feedback = $('refreshFeedback');
  feedback.textContent = message;
  feedback.className = `refresh-feedback${error ? ' error' : ''}`;
  feedback.hidden = !message;
}

let tradeAlertExpiry;
function renderTradeAlerts() {
  clearTimeout(tradeAlertExpiry);
  const report = data?.report;
  const generated = Date.parse(report?.generated_at || '');
  const age = Date.now() - generated;
  const fresh = Number.isFinite(generated) && age >= -60_000 && age < 300_000;
  const alerts = fresh ? report.rows.filter((row) =>
    row.selected && row.status === 'buy' && row.action_amount > 0 && row.action_shares > 0 &&
    (!document.body.classList.contains('detail-mode') || row.plan_id === selectedId)) : [];
  const section = $('tradeAlerts');
  section.hidden = alerts.length === 0;
  if (!alerts.length) return;
  section.classList.toggle('sample', data.sample);
  $('tradeAlertTitle').textContent = data.sample ? '示例策略触发买入条件' : '本期买入条件已触发';
  $('tradeAlertNote').textContent = data.sample
    ? '以下按示例预算测算，仅供观察；不是个人交易计划，系统未下单。'
    : '仅提示策略条件；请自行核对交易时段、场内价格和费用。系统未下单。';
  $('tradeAlertCount').textContent = `${alerts.length} 只 ETF`;
  const body = $('tradeAlertRows');
  body.replaceChildren();
  for (const row of alerts) {
    const item = document.createElement('div');
    item.className = 'trade-alert-row';
    const name = document.createElement('strong');
    name.textContent = `${row.name} · ${row.code}`;
    const amount = document.createElement('span');
    amount.textContent = `参考预算 ${money(row.action_amount)} · ${quantity(row.action_shares)} 份 · 整手估算 ${money(row.action_cost)}`;
    const reason = document.createElement('small');
    reason.textContent = `${data.strategies[row.strategy]} · ${row.reason}${row.market?.quote_at ? ` · 行情 ${row.market.quote_at.replace('T', ' ').slice(0, 19)}` : ''}`;
    item.append(name, amount, reason);
    body.append(item);
  }
  tradeAlertExpiry = setTimeout(renderTradeAlerts, Math.max(1000, 300_000 - age + 100));
}

function render(next) {
  data = next;
  if (!selectedId || !data.plans.some((plan) => plan.id === selectedId)) selectedId = data.plans[0]?.id;
  applyRoute();
  $('modeBadge').textContent = data.sample ? '示例计划 ⌄' : '本地计划 ⌄';
  $('modeBadge').classList.toggle('sample', data.sample);
  $('modeBadge').disabled = false;
  const rows = (data.report?.rows || []).filter((row) => row.selected);
  $('planCount').textContent = data.plans.length;
  $('actionAmount').textContent = data.report ? money(data.report.total_action_amount) : '—';
  $('buyCount').textContent = data.report ? rows.filter((row) => row.status === 'buy').length : '—';
  $('blockedCount').textContent = data.report ? rows.filter((row) => row.status === 'blocked').length : '—';
  $('updatedAt').textContent = data.report ? `最近测算 ${data.report.generated_at.replace('T', ' ').slice(0, 19)} · 建议未下单${Date.now() - Date.parse(data.report.generated_at) >= 300_000 ? ' · 请刷新数据' : ''}` : '尚无测算快照';
  const sampleNotice = data.sample ? '当前使用示例计划，预算仅供观察；点击右上角可查看参数。' : '';
  const valuationWarning = valuationUpdateWarning(data);
  showNotice([sampleNotice, valuationWarning].filter(Boolean).join(' '), Boolean(valuationWarning));
  renderStrategyGuide();
  renderTradeAlerts();
  renderPlans();
  renderDetail();
  renderHistory();
}

async function refresh() {
  const button = $('refreshBtn');
  button.disabled = true;
  button.textContent = '正在获取...';
  showRefreshFeedback('正在更新行情与估值数据并重新计算建议...');
  try {
    const next = await request('/api/refresh', {});
    historyCache.clear();
    estimateCache.clear();
    chartViews.clear();
    currentEstimate = null;
    render(next);
    const warning = valuationUpdateWarning(next);
    showRefreshFeedback([refreshResultMessage(next), warning].filter(Boolean).join(' '), Boolean(warning));
  }
  catch (error) {
    showNotice(`刷新失败：${error.message}`, true);
    showRefreshFeedback(`刷新失败：${error.message}`, true);
  }
  finally { button.disabled = false; button.textContent = '↻ 刷新数据'; }
}

document.querySelectorAll('.tab').forEach((button) => button.addEventListener('click', () => showTab(button.dataset.tab)));
$('backBtn').addEventListener('click', () => navigate(null));
$('refreshBtn').addEventListener('click', refresh);
$('modeBadge').addEventListener('click', openPlanDialog);
$('closePlanDialog').addEventListener('click', () => $('planDialog').close());
$('donePlanDialog').addEventListener('click', () => $('planDialog').close());
$('planDialog').addEventListener('click', (event) => { if (event.target === $('planDialog')) $('planDialog').close(); });
$('estimateStart').addEventListener('change', () => {
  const input = $('estimateStart');
  if (input.value) loadEstimate(selectedId, input.value);
});
document.querySelectorAll('[data-chart-period]').forEach((button) => button.addEventListener('click', () => {
  const history = historyCache.get(selectedId);
  if (!history) return;
  const view = chartViews.get(selectedId) || {mode:'1y', end:history.series.length};
  chartViews.set(selectedId, {...view, mode:button.dataset.chartPeriod});
  renderPlanChart(history);
}));
for (const [id, direction] of [['chartPrevious', -1], ['chartNext', 1]]) {
  $(id).addEventListener('click', () => {
    const history = historyCache.get(selectedId);
    if (!history) return;
    const window = chartWindow(history);
    const end = Math.max(window.size, Math.min(window.total,
      window.end + direction * Math.max(1, Math.round(window.size / 2))));
    chartViews.set(selectedId, {mode:window.mode, end});
    renderPlanChart(history);
  });
}
$('chartPosition').addEventListener('input', (event) => {
  const history = historyCache.get(selectedId);
  if (!history) return;
  const start = Number(event.target.value);
  const window = chartWindow(history);
  chartViews.set(selectedId, {mode:window.mode, end:start + window.size});
  renderPlanChart(history);
});

request('/api/dashboard').then((next) => {
  render(next);
  const generated = Date.parse(next.report?.generated_at || '');
  if (!Number.isFinite(generated) || Date.now() - generated >= 300_000) refresh();
})
  .catch((error) => showNotice(`读取失败：${error.message}`, true));
window.addEventListener('popstate', () => {
  if (!data) return;
  applyRoute();
  renderPlans();
  renderDetail();
  renderTradeAlerts();
  window.scrollTo(0, 0);
});
window.addEventListener('resize', () => {
  const history = historyCache.get(selectedId);
  if (history && !$('detail').hidden) renderPlanChart(history);
});
