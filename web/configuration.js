'use strict';
const $ = (id) => document.getElementById(id);
let data, watchCode = null, positionCode = null, planId = null;
const strategyNames = {ma:'均线定投', valuation:'估值定投', drawdown:'涨跌幅定投'};
function message(text, error = false) { $('feedback').textContent = text; $('feedback').hidden = !text; $('feedback').classList.toggle('error', error); }
async function request(url, method = 'GET', payload) {
  const options = {method, headers:{'Content-Type':'application/json'}};
  if (payload !== undefined) options.body = JSON.stringify(payload);
  const response = await fetch(url, options);
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || '请求失败');
  return result;
}
function field(form, name) { return form.elements.namedItem(name); }
function rowCell(row, value) { const cell = document.createElement('td'); cell.textContent = value; row.append(cell); return cell; }
function action(cell, label, handler, dangerous = false) { const button = document.createElement('button'); button.type = 'button'; button.textContent = label; button.className = dangerous ? 'remove' : ''; button.addEventListener('click', handler); cell.append(button); }
function setForm(form, values) { for (const [name, value] of Object.entries(values)) { const input = field(form, name); if (!input) continue; if (input.type === 'checkbox') input.checked = value; else input.value = value; } }
function frequency(plan) { return plan.frequency === 'daily' ? '每日' : plan.frequency === 'weekly' ? `周${['一','二','三','四','五'][plan.weekday]}` : `每月 ${plan.monthday} 日`; }
function numeric(form, name) { const value = field(form,name).value; if (value === '' || !Number.isFinite(Number(value))) throw new Error('请填写有效数字'); return Number(value); }
async function save(url, method, payload, text) {
  const controls = document.querySelectorAll('button'); controls.forEach((b) => { b.disabled = true; });
  try { await request(url, method, payload); await reload(); message(text); return true; }
  catch (error) { message(error.message, true); return false; }
  finally { controls.forEach((b) => { b.disabled = false; }); }
}
function render() {
  $('revision').textContent = `数据库配置 · 版本 ${data.revision} · ${data.watchlist.length} 只关注股票 · ${data.etf.plans.filter((p) => p.enabled).length} 个启用 ETF 计划`;
  $('watchCount').textContent = data.watchlist.length;
  $('portfolioCount').textContent = data.portfolio.length;
  $('etfCount').textContent = data.etf.plans.length;
  $('watchRows').replaceChildren();
  for (const item of data.watchlist) {
    const row = document.createElement('tr'); rowCell(row,item.code); rowCell(row,item.name); const actions = rowCell(row,'');
    action(actions,'编辑',() => { watchCode=item.code; setForm($('watchForm'),item); field($('watchForm'),'code').disabled=true; $('watchTitle').textContent='编辑关注股票'; });
    action(actions,'移出关注',() => save(`/api/ma120/watchlist?code=${encodeURIComponent(item.code)}`,'DELETE',undefined,`${item.name} 已移出关注池`),true);
    $('watchRows').append(row);
  }
  $('portfolioRows').replaceChildren();
  for (const item of data.portfolio) {
    const row = document.createElement('tr'); rowCell(row,`${item.name} ${item.code}`); rowCell(row,item.cost); rowCell(row,item.shares); const actions=rowCell(row,'');
    action(actions,'编辑',() => { positionCode=item.code; setForm($('portfolioForm'),item); field($('portfolioForm'),'code').disabled=true; $('portfolioTitle').textContent='编辑股票持仓'; });
    action(actions,'移出持仓',() => save(`/api/configuration/portfolio?code=${encodeURIComponent(item.code)}`,'DELETE',undefined,`${item.name} 已移出持仓配置`),true);
    $('portfolioRows').append(row);
  }
  $('etfRows').replaceChildren();
  for (const plan of data.etf.plans) {
    const row=document.createElement('tr'); rowCell(row,`${plan.name} ${plan.code}`); rowCell(row,`${strategyNames[plan.strategy]} / ${frequency(plan)}`); rowCell(row,`${plan.base_amount} / 上限 ${plan.max_amount}`); rowCell(row,plan.enabled?'已启用':'已停用'); const actions=rowCell(row,'');
    action(actions,'编辑',() => { planId=plan.id; setForm($('etfForm'),plan); field($('etfForm'),'code').disabled=true; $('etfTitle').textContent='编辑 ETF 计划'; showFrequency(); });
    if (plan.enabled) action(actions,'停用',() => save(`/api/configuration/plans?id=${encodeURIComponent(plan.id)}`,'DELETE',undefined,`${plan.name} 已停用，历史记录保留`),true);
    $('etfRows').append(row);
  }
  const rules=$('rulesForm'); setForm(rules,{...data.etf.settings,...data.etf.valuation,...data.ma120, ma_days:data.etf.settings.ma_days,stock_ma_days:data.ma120.ma_days});
  $('bandRows').replaceChildren(); for (const band of data.etf.settings.bands) addBand(band);
}
async function reload() { data=await request('/api/configuration'); render(); }
function addBand(band={below:30,multiplier:0.5}) {
  const row=document.createElement('tr');
  for (const key of ['below','multiplier']) { const cell=rowCell(row,''); const input=document.createElement('input'); input.type='number'; input.step='any'; input.required=true; input.dataset.key=key; input.value=band[key]; input.min=key==='below'?-100:0; input.setAttribute('aria-label',key==='below'?'偏离上界':'基础金额倍数'); cell.append(input); }
  const actions=rowCell(row,''); action(actions,'移除',() => row.remove(),true); $('bandRows').append(row);
}
function showFrequency() { const value=field($('etfForm'),'frequency').value; $('weekdayField').hidden=value!=='weekly'; $('monthdayField').hidden=value!=='monthly'; }
document.querySelectorAll('[data-panel]').forEach((button) => button.addEventListener('click',() => {
  document.querySelectorAll('[data-panel]').forEach((b) => { const active=b===button; b.classList.toggle('active',active); b.setAttribute('aria-selected',String(active)); });
  document.querySelectorAll('[data-section]').forEach((section) => { section.hidden=section.dataset.section!==button.dataset.panel; });
}));
$('watchForm').addEventListener('submit',async (event) => { event.preventDefault(); const form=event.target; const item={code:field(form,'code').value.trim().toUpperCase(),name:field(form,'name').value.trim()}; if (await save(watchCode?`/api/ma120/watchlist?code=${encodeURIComponent(watchCode)}`:'/api/ma120/watchlist',watchCode?'PATCH':'POST',watchCode?{name:item.name}:item,'关注池已保存')) form.reset(); });
$('watchForm').addEventListener('reset',() => { watchCode=null; field($('watchForm'),'code').disabled=false; $('watchTitle').textContent='新增关注股票'; });
$('portfolioForm').addEventListener('submit',async (event) => { event.preventDefault(); const form=event.target; try { const item={code:field(form,'code').value.trim().toUpperCase(),name:field(form,'name').value.trim(),cost:numeric(form,'cost'),shares:numeric(form,'shares')}; if (await save(positionCode?`/api/configuration/portfolio?code=${encodeURIComponent(positionCode)}`:'/api/configuration/portfolio',positionCode?'PATCH':'POST',item,'持仓已保存')) form.reset(); } catch(error) { message(error.message,true); } });
$('portfolioForm').addEventListener('reset',() => { positionCode=null; field($('portfolioForm'),'code').disabled=false; $('portfolioTitle').textContent='新增股票持仓'; });
$('etfForm').addEventListener('submit',async (event) => {
  event.preventDefault(); const form=event.target;
  try { const plan={code:field(form,'code').value.trim(),name:field(form,'name').value.trim(),index_code:field(form,'index_code').value.trim(),strategy:field(form,'strategy').value,base_amount:numeric(form,'base_amount'),max_amount:numeric(form,'max_amount'),frequency:field(form,'frequency').value,cost:numeric(form,'cost'),shares:numeric(form,'shares'),enabled:field(form,'enabled').checked,strategy_reason:field(form,'strategy_reason').value.trim()}; if(planId) plan.id=planId; if(plan.frequency==='weekly') plan.weekday=numeric(form,'weekday'); if(plan.frequency==='monthly') plan.monthday=numeric(form,'monthday'); if(await save(planId?`/api/configuration/plans?id=${encodeURIComponent(planId)}`:'/api/configuration/plans',planId?'PATCH':'POST',plan,'ETF 计划已保存')) form.reset(); }
  catch(error) { message(error.message,true); }
});
$('etfForm').addEventListener('reset',() => { planId=null; field($('etfForm'),'code').disabled=false; $('etfTitle').textContent='新增 ETF 计划'; setTimeout(showFrequency,0); });
field($('etfForm'),'frequency').addEventListener('change',showFrequency);
$('rulesForm').addEventListener('submit',async (event) => {
  event.preventDefault(); const form=event.target;
  try { const settings={ma_days:numeric(form,'ma_days'),lot_size:numeric(form,'lot_size'),above_multiplier:numeric(form,'above_multiplier'),bands:Array.from($('bandRows').children).map((row) => ({below:Number(row.querySelector('[data-key="below"]').value),multiplier:Number(row.querySelector('[data-key="multiplier"]').value)}))}; const valuation=Object.fromEntries(['lookback_years','min_samples','min_span_days','max_age_days','buy_below','high_at'].map((key) => [key,numeric(form,key)])); const ma120={ma_days:numeric(form,'stock_ma_days'),buy_factor:numeric(form,'buy_factor'),sell_factor:numeric(form,'sell_factor'),history_days:numeric(form,'history_days')}; await save('/api/configuration/rules','POST',{settings,valuation,ma120},'策略规则已保存；下次测算使用新规则'); }
  catch(error) { message(error.message,true); }
});
$('addBand').addEventListener('click',() => addBand());
$('reload').addEventListener('click',() => reload().then(() => message('已重新读取数据库')).catch((error) => message(error.message,true)));
reload().catch((error) => message(error.message,true));
