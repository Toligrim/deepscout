const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
let projectId = 1;
let currentDomain = "";

function esc(v="") { return String(v).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function safeMarked(v="") { return esc(v).replaceAll('&lt;mark&gt;', '<mark>').replaceAll('&lt;/mark&gt;', '</mark>'); }
function status(el, text, cls="") { el.textContent = text; el.className = `status ${cls}`; }
async function api(path, opts={}) {
  const r = await fetch(path, {headers:{'Content-Type':'application/json', ...(opts.headers||{})}, ...opts});
  let data;
  try { data = await r.json(); } catch { data = {detail: await r.text()}; }
  if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
  return data;
}

async function loadProjects() {
  const projects = await api('/api/projects');
  const select = $('#projectSelect');
  select.innerHTML = projects.map(p => `<option value="${p.id}">${esc(p.name)}</option>`).join('');
  if (!projects.some(p => p.id === projectId)) projectId = projects[0]?.id || 1;
  select.value = String(projectId);
  await refreshStats();
}

$('#projectSelect').addEventListener('change', async e => { projectId = Number(e.target.value); await refreshStats(); if(currentDomain) await loadDomainRows(); });
$('#newProjectBtn').addEventListener('click', async () => {
  const name = prompt('Название проекта'); if(!name) return;
  const p = await api('/api/projects', {method:'POST', body:JSON.stringify({name})});
  projectId = p.id; await loadProjects();
});

$$('.tab').forEach(btn => btn.addEventListener('click', async () => {
  $$('.tab').forEach(x => x.classList.remove('active')); $$('.panel').forEach(x => x.classList.remove('active'));
  btn.classList.add('active'); $('#' + btn.dataset.tab).classList.add('active');
  if(btn.dataset.tab === 'library') await refreshStats();
}));

function selectedBackends() {
  const backends = [];
  if ($('#backendSearxng').checked) backends.push('searxng');
  if ($('#backendOpenserp').checked) backends.push('openserp');
  return backends.length ? backends : null;
}
function selectedOpenserpEngines() {
  const engines = $$('input[name=openserpEngine]:checked').map(x => x.value);
  return engines.length ? engines : null;
}

$('#searchForm').addEventListener('submit', async e => {
  e.preventDefault(); const q = $('#query').value.trim(); if(!q) return;
  status($('#searchStatus'), 'Ищу…'); $('#searchResults').innerHTML='';
  try {
    const data = await api('/api/search', {method:'POST', body:JSON.stringify({
      project_id:projectId, query:q, language:$('#language').value, time_range:$('#timeRange').value || null,
      backends:selectedBackends(), openserp_engines:selectedOpenserpEngines(),
    })});
    const backendLine = Object.entries(data.backends||{}).map(([name,v]) => `${esc(name)}: ${v.status==='failed'?'ошибка':v.count}`).join(' · ');
    status($('#searchStatus'), `Найдено и сохранено: ${data.count}. ${backendLine}`, data.warnings && data.warnings.length ? '' : 'good');
    $('#searchResults').innerHTML = data.results.map(renderSearchCard).join('') || '<div class="muted">Ничего не найдено.</div>';
    if (data.warnings && data.warnings.length) {
      $('#searchResults').insertAdjacentHTML('beforebegin', `<div class="warn-line">${data.warnings.map(esc).join(' · ')}</div>`);
    }
  } catch(err) { status($('#searchStatus'), err.message, 'error'); }
});

function renderSearchCard(r) {
  const domain = (()=>{try{return new URL(r.canonical_url || r.url).hostname}catch{return ''}})();
  const provenance = (r.backends||[]).map(b=>`<span class="chip">${esc(b)}</span>`).join('') + (r.engines||[]).map(e=>`<span class="chip">${esc(e)}</span>`).join('');
  return `<article class="card">
    <div class="card-title"><a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.title || r.url)}</a></div>
    <div class="url">${esc(r.canonical_url || r.url)}</div>
    <div class="snippet">${esc(r.snippet || '')}</div>
    <div class="engine-row">${provenance}</div>
    <div class="card-actions">
      <button class="mini action-domain" data-domain="${esc(domain)}">Исследовать домен</button>
      <button class="mini action-fetch" data-url="${esc(r.canonical_url || r.url)}">Текст + ссылки</button>
      <button class="mini action-quote" data-title="${esc((r.title || '').slice(0,120))}">Искать заголовок</button>
    </div>
  </article>`;
}

const STATUS_DOT = {ok:'dot-ok', degraded:'dot-degraded', failed:'dot-failed', unknown:'dot-unknown'};
function backendStatus(b) {
  if (!b.reachable) return 'failed';
  const bad = (b.engines||[]).filter(e => e.status === 'degraded' || e.status === 'failed').length;
  if (bad && bad === (b.engines||[]).length) return 'failed';
  if (bad) return 'degraded';
  return 'ok';
}
function renderBackendBlock(name, label, b) {
  const st = backendStatus(b);
  const engines = (b.engines||[]).map(e => `<span class="engine-chip" title="${esc(e.reason||'')}"><span class="dot ${STATUS_DOT[e.status]||'dot-unknown'}"></span>${esc(e.name)}</span>`).join('');
  const err = b.last_error ? ` · ${esc(b.last_error)}` : '';
  return `<div class="source-block">
    <div class="source-block-head"><span class="dot ${STATUS_DOT[st]}"></span>${esc(label)}${b.latency_ms!=null?` · ${Math.round(b.latency_ms)} мс`:''}${err}</div>
    <div class="engine-row">${engines}</div>
  </div>`;
}
async function loadSourcesHealth() {
  try {
    const h = await api('/api/search/health');
    const overall = ['searxng','openserp'].map(n => backendStatus(h[n]||{reachable:false,engines:[]}));
    const worst = overall.includes('failed') ? 'дегрдация' : overall.includes('degraded') ? 'частично' : 'ok';
    $('#sourcesSummary').textContent = `(${worst})`;
    const discovery = h.discovery || {};
    const discoveryBlock = `<div class="source-block"><div class="source-block-head">Discovery</div><div class="engine-row">
      <span class="engine-chip"><span class="dot ${discovery.wayback?.reachable?'dot-ok':'dot-failed'}"></span>Wayback</span>
      <span class="engine-chip"><span class="dot ${discovery.commoncrawl?.reachable?'dot-ok':'dot-failed'}"></span>Common Crawl</span>
    </div></div>`;
    $('#sourcesBody').innerHTML = renderBackendBlock('searxng','SearXNG', h.searxng||{engines:[]}) + renderBackendBlock('openserp','OpenSERP', h.openserp||{engines:[]}) + discoveryBlock;
  } catch(err) { $('#sourcesBody').innerHTML = `<div class="status error">${esc(err.message)}</div>`; }
}
loadSourcesHealth();

function quoteSearch(text) { $$('.tab')[0].click(); $('#query').value = `"${text.replaceAll('"','')}"`; $('#query').focus(); }
function deepDomain(domain) { $$('.tab')[1].click(); $('#domainInput').value = domain; currentDomain = domain; $('#domainInput').focus(); }
$('#searchResults').addEventListener('click', e => {
  const domainBtn = e.target.closest('.action-domain'); if(domainBtn) return deepDomain(domainBtn.dataset.domain);
  const fetchBtn = e.target.closest('.action-fetch'); if(fetchBtn) return fetchAndRead(fetchBtn.dataset.url);
  const quoteBtn = e.target.closest('.action-quote'); if(quoteBtn) return quoteSearch(quoteBtn.dataset.title);
});

$('#domainForm').addEventListener('submit', async e => {
  e.preventDefault(); currentDomain = $('#domainInput').value.trim();
  const sources = $$('input[name=source]:checked').map(x => x.value);
  status($('#domainStatus'), `Запускаю: ${sources.join(', ')}…`); $('#domainSummary').innerHTML='';
  try {
    const data = await api('/api/discover/domain', {method:'POST', body:JSON.stringify({project_id:projectId, domain:currentDomain, sources, limit_per_source:Number($('#discoverLimit').value), include_subdomains:$('#subdomains').checked, crawl_depth:Number($('#crawlDepth').value)})});
    currentDomain = data.domain;
    const cards = Object.entries(data.providers).map(([name,v]) => `<div class="stat"><b>${v.accepted}</b><span>${esc(name)}${v.error ? ' · error' : ''}</span></div>`).join('');
    $('#domainSummary').innerHTML = cards;
    const errors = Object.entries(data.providers).filter(([,v])=>v.error).map(([k,v])=>`${k}: ${v.error}`);
    status($('#domainStatus'), errors.length ? `Готово с ошибками отдельных источников. ${errors.join(' | ')}` : `Готово. Обработано ${data.processed} URL.`, errors.length?'':'good');
    await loadDomainRows(); await refreshStats();
  } catch(err) { status($('#domainStatus'), err.message, 'error'); }
});

async function loadDomainRows() {
  if(!currentDomain) return;
  const p = new URLSearchParams({project_id:String(projectId), domain:currentDomain, limit:'1000'});
  const q = $('#domainFilter').value.trim(); const kind=$('#kindFilter').value; const source=$('#sourceFilter').value;
  if(q) p.set('q', q); if(kind) p.set('kind',kind); if(source) p.set('source',source);
  try {
    const rows = await api('/api/urls?' + p.toString());
    $('#domainRows').innerHTML = rows.map(r => `<tr>
      <td><a class="table-url" href="${esc(r.url)}" target="_blank" title="${esc(r.url)}">${esc(r.url)}</a></td>
      <td><span class="chip">${esc(r.kind)}</span></td>
      <td>${(r.sources||'').split(',').filter(Boolean).map(s=>`<span class="chip">${esc(s)}</span>`).join('')}</td>
      <td><button class="mini row-fetch" data-url="${esc(r.url)}">Текст</button></td>
    </tr>`).join('');
  } catch(err) { status($('#domainStatus'), err.message, 'error'); }
}
['domainFilter','kindFilter','sourceFilter'].forEach(id => $('#' + id).addEventListener(id==='domainFilter'?'input':'change', debounce(loadDomainRows, 250)));
function debounce(fn, ms){let t;return(...args)=>{clearTimeout(t);t=setTimeout(()=>fn(...args),ms)}}

async function fetchAndRead(url) { const dlg=$('#pageDialog'); $('#dialogTitle').textContent='Загружаю…'; $('#dialogText').textContent=''; $('#dialogMeta').textContent=url; dlg.showModal();
  try {
    const d=await api('/api/fetch',{method:'POST',body:JSON.stringify({project_id:projectId,url})});
    $('#dialogTitle').textContent=d.title||d.url; $('#dialogMeta').textContent=`${d.status} · ${d.mime} · найдено ссылок: ${d.links_found}`; $('#dialogText').textContent=d.text_preview || 'Текст не извлечён (возможно, это не HTML).';
    await refreshStats(); if(currentDomain) await loadDomainRows();
  } catch(err) { $('#dialogTitle').textContent='Ошибка'; $('#dialogText').textContent=err.message; }
}
$('#domainRows').addEventListener('click', e => { const btn=e.target.closest('.row-fetch'); if(btn) fetchAndRead(btn.dataset.url); });

$('#localSearchForm').addEventListener('submit', async e => {
  e.preventDefault(); const q=$('#localQuery').value.trim(); if(!q)return;
  try {
    const rows=await api('/api/local-search?'+new URLSearchParams({project_id:String(projectId),q}));
    $('#localResults').innerHTML=rows.map(r=>`<article class="card"><div class="card-title"><a href="${esc(r.url)}" target="_blank">${esc(r.title||r.url)}</a></div><div class="url">${esc(r.url)}</div><div class="snippet">${safeMarked(r.snippet||'')}</div></article>`).join('') || '<div class="muted">Ничего не найдено.</div>';
  } catch(err){ $('#localResults').innerHTML=`<div class="status error">${esc(err.message)}</div>`; }
});

async function refreshStats(){
  try{const s=await api('/api/stats?project_id='+projectId); $('#stats').innerHTML=`<div class="stat"><b>${s.urls}</b><span>URL</span></div><div class="stat"><b>${s.domains}</b><span>доменов</span></div><div class="stat"><b>${s.fetched}</b><span>скачано</span></div>` + s.sources.slice(0,5).map(x=>`<div class="stat"><b>${x.n}</b><span>${esc(x.source)}</span></div>`).join('');}catch{}
}
loadProjects();
