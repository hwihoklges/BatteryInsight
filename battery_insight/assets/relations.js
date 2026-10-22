/* Battery Insight: no libraries, remote assets, models, or missing-value imputation. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const NS = 'http://www.w3.org/2000/svg';
  const PALETTE = ['#278c91', '#6986ae', '#c48b65', '#8a86ad', '#7c9e78', '#b47988'];
  const state = {root:null,data: null,mode:'design',modeMetrics:{},cohort: '', selected: '', compare: '', metrics: [], edge: null, positions: new Map(), zoom: 1, panX: 0, panY: 0, panel: 'network', imported: false};
  let graphNodes = [], graphEdges = [], dragging = null, refreshing = false;
  const finite = value => typeof value === 'number' && Number.isFinite(value);
  const text = value => value === null || value === undefined ? '—' : String(value);
  const number = (value, digits = 4) => finite(value) ? (value !== 0 && (Math.abs(value) < .0001 || Math.abs(value) >= 1e6) ? value.toExponential(3) : new Intl.NumberFormat('ko-KR', {maximumFractionDigits: digits}).format(value)) : '—';
  const percent = value => finite(value) ? `${number(value * 100, 1)}%` : '—';
  const koreaTime = value => { const date=new Date(value); return Number.isNaN(date.getTime()) ? String(value) : new Intl.DateTimeFormat('ko-KR',{timeZone:'Asia/Seoul',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).format(date)+' KST'; };
  const short = (value, n = 24) => String(value ?? '').length > n ? String(value).slice(0, n - 1) + '…' : String(value ?? '');
  const list = value => Array.isArray(value) ? value : [];
  const isDesign = () => state.mode === 'design';
  const hasValue = value => finite(value) || typeof value==='string'&&value.trim()!=='' || value!==null&&typeof value==='object'&&!Array.isArray(value)&&Object.keys(value).length>0;
  const compositionBasis = value => ({electrode_total:'전극 전체 기준',active_material_total:'활물질 내부 기준'}[value] || value || '분모 미지정');
  const componentLabel = name => {const known={active:'활물질',solid_electrolyte:'고체 전해질',conductive:'도전재',binder:'바인더',other:'기타'};const match=String(name).match(/^(?:cathode|anode)_(active|solid_electrolyte|conductive|binder|other)_wt_pct$/);return match?known[match[1]]:known[name]||name;};
  const featureValue = (value,unit='') => finite(value) ? number(value) : typeof value==='string' ? value : value&&typeof value==='object'&&!Array.isArray(value) ? Object.entries(value).map(([name,amount])=>`${componentLabel(name)} ${number(amount)}${unit?' '+unit:''}`).join(' · ') : '—';
  const metricId = metric => metric.metric_id || metric.id || '';
  const nodeId = node => node.experiment_id || node.id || '';
  const nodeLabel = node => node ? (node.label || nodeId(node)) : '—';
  const feature = (node, id) => list(node?.features).find(f => f.metric_id === id);
  const countAvailable = node => Array.isArray(node.available_metrics) ? node.available_metrics.length : finite(node.available_metrics) ? node.available_metrics : list(node.features).filter(f => hasValue(f.value)).length;
  const nodeById = id => state.data.nodes.find(node => nodeId(node) === id);
  const metricById = id => state.data.metrics.find(metric => metricId(metric) === id) || {metric_id: id, label: id, unit: ''};
  const cohortData = id => list(state.data.cohorts).find(cohort => cohort.cohort === id);
  const colorFor = id => PALETTE[(Math.max(0, list(state.data.cohorts).findIndex(cohort => cohort.cohort === id))+(isDesign()?0:1)) % PALETTE.length];
  function element(tag, className, value) { const node = document.createElement(tag); if (className) node.className = className; if (value !== undefined) node.textContent = text(value); return node; }
  function svg(tag, attrs = {}, value) { const node = document.createElementNS(NS, tag); for (const [key, val] of Object.entries(attrs)) node.setAttribute(key, String(val)); if (value !== undefined) node.textContent = text(value); return node; }
  function option(value, label) { const node = element('option', '', label); node.value = value; return node; }
  function emptyRow(body, columns, message) { const row = element('tr'); const cell = element('td', 'empty-row', message); cell.colSpan = columns; row.append(cell); body.append(row); }
  function tableCell(row, value, cls = '') { const cell = element('td', cls, value); row.append(cell); return cell; }
  function legendItem(label, color) { const item = element('span', 'legend-item'); const dot = element('i', 'legend-dot'); dot.style.background = color; item.append(dot, document.createTextNode(label)); return item; }
  function showTooltip(event, value) { const box = $('tooltip'); box.textContent = value; box.hidden = false; const w = Math.min(340, window.innerWidth - 30); box.style.left = `${Math.max(10, Math.min(event.clientX + 14, window.innerWidth - w - 15))}px`; box.style.top = `${Math.max(10, Math.min(event.clientY + 13, window.innerHeight - box.offsetHeight - 15))}px`; }
  function hideTooltip() { $('tooltip').hidden = true; }
  const STATUS = {ok:'정상', valid:'유효', ready:'완료', processed:'처리 완료', reused:'변경 없음 · 재사용', pending:'대기', failed:'실패', missing:'결측', constant:'상수값', constant_metric:'상수 지표', all_missing:'모두 결측', insufficient_data:'자료 부족', insufficient_pairs:'짝 자료 부족', insufficient_cells:'셀 수 부족', insufficient_observations:'관측 수 부족', insufficient_points:'관측 수 부족', insufficient_shared_metrics:'공통 지표 수 부족', insufficient_metric_coverage:'공통 지표 비율 부족', below_similarity_threshold:'설정 유사도 미만', nonfinite:'유한값 아님', no_valid_values:'유효 측정 없음', no_finite_values:'유한 측정값 없음', incomplete_window:'관측 구간 불완전', no_scale:'표준화 범위 없음', zero_variance:'변동 없음'};
  Object.assign(STATUS,{unregistered:'실험 등록 대기',file_still_settling:'파일 기록 안정화 대기',not_found_or_pattern_mismatch:'파일 없음 또는 패턴 불일치',excluded_by_registration:'등록표에서 제외',observes_beyond_comparison_window:'비교 구간 이후 자료 참조',column_not_emitted:'계산 컬럼 없음',numeric_range_exceeded:'수치 범위 초과',insufficient_entities:'실험 수 부족'});
  Object.assign(STATUS,{no_design_fields_configured:'설계 항목 등록 필요',insufficient_shared_fields:'공통 설계 항목 부족',insufficient_design_coverage:'설계 관측 범위 부족',insufficient_weighted_coverage:'가중 설계 관측 범위 부족'});
  const statusText = value => STATUS[value] || String(value || '—');
  function statusTag(value) { const warn = !['ok','valid','ready','processed','reused'].includes(value); return element('span', `status-tag${value === 'failed' ? ' bad' : warn ? ' warn' : ''}`, statusText(value)); }
  function safeData(data) {
    if (!data || typeof data !== 'object' || !Array.isArray(data.nodes) || !Array.isArray(data.metrics) || !Array.isArray(data.edges)) throw new Error('실험 관계 분석에서 생성한 data.json 파일이 필요합니다. nodes, metrics, edges 항목을 확인하세요.');
    const ids = data.nodes.map(nodeId);
    if (ids.some(id => !id) || new Set(ids).size !== ids.length) throw new Error('실험 ID가 비어 있거나 중복됩니다.');
    const known = new Set(ids);
    if (data.edges.some(edge => !known.has(edge.source) || !known.has(edge.target) || !finite(edge.similarity))) throw new Error('연결의 실험 ID 또는 유사도 값이 올바르지 않습니다.');
    return data;
  }
  function scopeNodes() { return state.data.nodes.filter(node => !state.cohort || node.cohort === state.cohort); }
  function filteredNodes() {
    const terms = $('experiment-search').value.toLowerCase().trim().split(/\s+/).filter(Boolean);
    let nodes = scopeNodes().filter(node => terms.every(term => [nodeLabel(node), nodeId(node), node.cell_id, node.test_id, node.protocol, node.chemistry, node.cohort].join(' ').toLowerCase().includes(term)));
    if ($('neighbors-only').checked && state.selected) {
      const linked = new Set([state.selected]);
      for (const edge of state.data.edges) if (edge.similarity >= +$('threshold').value && (edge.source === state.selected || edge.target === state.selected)) { linked.add(edge.source); linked.add(edge.target); }
      nodes = nodes.filter(node => linked.has(nodeId(node)));
    }
    return nodes;
  }
  function selectedMetrics() { return state.metrics.map(metricById); }
  function defaultMetricIds(){const metrics=state.data.metrics;if(!isDesign())return metrics.slice(0,6).map(metricId);const chosen=[...metrics.filter(m=>m.kind==='categorical').slice(0,2),...metrics.filter(m=>m.kind==='composition').slice(0,2),...metrics.filter(m=>m.kind==='numeric').slice(0,2)];for(const metric of metrics){if(chosen.length>=6)break;if(!chosen.includes(metric))chosen.push(metric);}return chosen.map(metricId);}
  function setPayload(rootPayload, imported = false, modeChanged = false) {
    safeData(rootPayload);
    if(rootPayload.design_graph)safeData(rootPayload.design_graph);
    const hadData = Boolean(state.data);
    state.root=rootPayload;
    const emptyDesign={schema_version:rootPayload.schema_version,nodes:[],edges:[],metrics:[],cohorts:[],metric_correlations:[],excluded_pairs:[],settings:{},metadata:{disabled_reason:'no_design_fields_configured'}};
    const graph=isDesign()?(rootPayload.design_graph||emptyDesign):rootPayload;
    const payload={...graph,generated_at:rootPayload.generated_at,is_demo:rootPayload.is_demo,ingestion:rootPayload.ingestion};
    state.data = payload; state.imported = imported;
    const cohorts = list(payload.cohorts).map(item => item.cohort);
    if(modeChanged&&nodeById(state.selected))state.cohort=nodeById(state.selected).cohort;
    if (!cohorts.includes(state.cohort) && !(hadData && state.cohort === '' && cohorts.length>1)) state.cohort = cohorts[0] || '';
    if (!nodeById(state.selected) || (state.cohort && nodeById(state.selected).cohort !== state.cohort)) state.selected = scopeNodes()[0] ? nodeId(scopeNodes()[0]) : '';
    if (!nodeById(state.compare) || nodeById(state.compare)?.cohort !== nodeById(state.selected)?.cohort) state.compare = '';
    const knownMetrics = new Set(payload.metrics.map(metricId));
    state.metrics = state.metrics.filter(id => knownMetrics.has(id));
    if (!state.metrics.length) state.metrics = defaultMetricIds();
    state.edge = null; state.positions.clear();
    $('cohort').replaceChildren();
    if (cohorts.length > 1) $('cohort').append(option('', '전체 실험군 · 군내 연결'));
    for (const cohort of list(payload.cohorts)) $('cohort').append(option(cohort.cohort, `${cohort.cohort} (${list(cohort.experiment_ids).length || payload.nodes.filter(n => n.cohort === cohort.cohort).length})`));
    if (!cohorts.length) $('cohort').append(option('', '실험군 없음'));
    $('cohort').value = state.cohort;
    $('demo-banner').hidden = !payload.is_demo;
    $('error-banner').hidden = true;
    $('updated-at').textContent = payload.generated_at ? `갱신 ${koreaTime(payload.generated_at)}` : '';
    const settings = payload.settings || {};
    const configuredMinimum=isDesign()?settings.min_similarity:settings.edge_min_similarity;
    const minimumSimilarity = finite(configuredMinimum) ? Math.max(0,Math.min(1,configuredMinimum)) : 0;
    const previousThreshold = hadData ? +$('threshold').value : .65;
    $('threshold').min=minimumSimilarity;
    $('threshold').value=Math.max(minimumSimilarity,Math.min(1,previousThreshold));
    $('scope-description').textContent = isDesign()?'활물질·전극 구성·로딩이 가까운 실험을 찾습니다.':`공통 관측 구간${settings.life_min !== undefined || settings.life_max !== undefined ? ` (${text(settings.life_min)}–${text(settings.life_max)})` : ''}의 전기화학 지표가 비슷한 실험을 찾습니다.`;
    $('refresh-state').textContent = imported ? '선택한 JSON · 자동 갱신 중지' : location.protocol.startsWith('http') ? '로컬 서버 · 10초마다 갱신 확인' : '로컬 파일 · 새 JSON 열기로 갱신';
    renderMode();renderMetricOptions(); renderAll();
  }
  function switchMode(mode){if(mode===state.mode)return;state.modeMetrics[state.mode]=state.metrics.slice();state.mode=mode;state.metrics=(state.modeMetrics[mode]||[]).slice();state.edge=null;state.zoom=1;state.panX=state.panY=0;setPayload(state.root,state.imported,true);}
  function renderMode(){
    const design=isDesign();document.body.classList.toggle('design-mode',design);
    for(const mode of ['design','electrochem']){$('mode-'+mode).classList.toggle('active',state.mode===mode);$('mode-'+mode).setAttribute('aria-pressed',String(state.mode===mode));}
    $('tab-correlation').hidden=design;if(design&&state.panel==='correlation')showPanel('network');
    $('radar-svg').toggleAttribute('hidden',design);$('radar-help').hidden=design;$('design-comparison').hidden=!design;
    $('graph-title').textContent=design?'전극 설계·재료 유사도 네트워크':'전기화학 지표 유사도 네트워크';
    $('graph-description').textContent=design?'선을 선택하면 재료·조성·로딩 중 어떤 항목이 다른지 확인할 수 있습니다.':'선을 선택하면 어떤 측정 지표가 실험 간 차이를 만드는지 확인할 수 있습니다.';
    $('cohort-help').textContent=design?'등록한 설계 비교군 안에서 재료·조성·수치 조건을 비교합니다.':'설정된 실험군과 공통 관측 구간에서 비교합니다. 프로토콜·화학계·온도는 상세에서 확인하세요.';
    $('metric-help').textContent=design?'최대 8개를 골라 설계 비교표에서 봅니다. 네트워크 유사도는 설정된 전체 설계 항목을 사용합니다.':'최대 8개를 골라 레이더와 상관행렬을 함께 봅니다. 네트워크 유사도는 계산에 포함된 전체 대표 지표를 사용합니다.';
    document.querySelector('label[for="metric-search"]').textContent=design?'비교 설계 항목':'비교 지표';$('metric-search').placeholder=design?'설계 항목 / 분류':'지표 이름 / 분류';$('feature-search').placeholder=design?'설계 항목 검색':'지표 검색';
    $('map-help').textContent=design?'점은 실험, 선은 전극 설계의 유사도입니다. 전기화학 지표가 비슷한지는 두 점수 비교표에서 확인합니다.':'점은 실험, 선은 전기화학 지표의 유사도입니다. 설계가 비슷한지는 두 점수 비교표에서 확인합니다.';
    $('stat-metrics-label').textContent=design?'설계 항목':'전기화학 지표';$('stat-metrics-description').textContent=design?'등록된 재료·조성·수치 조건':'중복 정리 후 입력 지표';
    $('feature-title').textContent=design?'선택 실험의 설계 조건':'선택 실험의 측정 지표';$('feature-description').textContent=design?'재료명·조성·로딩을 등록된 값과 단위로 확인합니다.':'요약값과 유효 측정 수를 함께 봅니다. 미산출 값을 0으로 채우지 않습니다.';
    $('edge-distance-label').textContent=design?'가중 거리 기여':'표준화 거리';
  }
  function renderMetricOptions() {
    const root = $('metric-options'); root.replaceChildren();
    const query = $('metric-search').value.toLowerCase().trim();
    let count = 0;
    for (const metric of state.data.metrics) {
      const id = metricId(metric); if (query && ![id, metric.label, metric.family, metric.unit].join(' ').toLowerCase().includes(query)) continue;
      const row = element('label', 'metric-option'); const check = element('input'); check.type = 'checkbox'; check.checked = state.metrics.includes(id);
      check.disabled = !check.checked && state.metrics.length >= 8;
      check.addEventListener('change', () => { if (check.checked && state.metrics.length < 8) state.metrics.push(id); else state.metrics = state.metrics.filter(key => key !== id); renderMetricOptions(); renderRadar(); renderHeatmap(); });
      const label = element('span', '', metric.label || id); label.append(element('small', '', `${metric.family || '기타'}${metric.unit ? ' · ' + metric.unit : ''}`)); row.append(check, label); root.append(row); count++;
    }
    if (!count) root.append(element('p', 'help', '검색과 일치하는 지표가 없습니다.'));
    $('metric-count').textContent = `${state.metrics.length} / 8`;
  }
  function renderStats() {
    const nodes = scopeNodes(); const ingestion = state.data.ingestion || {};
    const excludedMetrics = list(state.data.cohorts).filter(c => !state.cohort || c.cohort === state.cohort).reduce((sum, cohort) => sum + list(cohort.excluded_metrics).length, 0);
    const issues = list(ingestion.pending).length + list(ingestion.failed).length + list(ingestion.issues).length + excludedMetrics;
    $('stat-experiments').textContent = nodes.length;
    $('stat-cells').textContent = `${new Set(nodes.map(node => node.cell_id).filter(Boolean)).size}개 셀 · ${state.cohort || list(state.data.cohorts).length + '개 실험군'}`;
    $('stat-metrics').textContent = state.data.metrics.length;
    $('stat-edges').textContent = graphEdges.length;
    $('stat-threshold').textContent = `유사도 ≥ ${number(+$('threshold').value, 2)}`;
    $('stat-issues').textContent = issues;
    $('quality-count').textContent = issues;
    $('stat-ingestion').textContent = `대기 ${list(ingestion.pending).length} · 실패 ${list(ingestion.failed).length} · 품질 ${list(ingestion.issues).length + excludedMetrics}`;
  }
  function hash(value) { let result = 2166136261; for (const char of String(value)) { result ^= char.charCodeAt(0); result = Math.imul(result, 16777619); } return result >>> 0; }
  function ensurePositions() {
    const nodes = scopeNodes().slice().sort((a,b) => nodeId(a).localeCompare(nodeId(b)));
    const fresh = nodes.filter(node => !state.positions.has(nodeId(node)));
    if (!fresh.length) return;
    nodes.forEach((node, i) => { if (!state.positions.has(nodeId(node))) { const angle = 2 * Math.PI * i / Math.max(nodes.length, 1) - Math.PI / 2; const radius = nodes.length === 1 ? 0 : 150 + hash(nodeId(node)) % 45; state.positions.set(nodeId(node), {x:450 + Math.cos(angle) * radius * 1.55, y:270 + Math.sin(angle) * radius}); } });
    const index = new Map(nodes.map((node, i) => [nodeId(node), i]));
    const edges = state.data.edges.filter(edge => index.has(edge.source) && index.has(edge.target));
    const positions = nodes.map(node => state.positions.get(nodeId(node)));
    const iterations = nodes.length > 200 ? 32 : nodes.length > 80 ? 65 : 140;
    for (let iteration = 0; iteration < iterations; iteration++) {
      const forces = nodes.map(() => ({x:0,y:0}));
      for (let i = 0; i < nodes.length; i++) for (let j = i+1; j < nodes.length; j++) {
        const dx = positions[i].x - positions[j].x || .1, dy = positions[i].y - positions[j].y || .1;
        const d2 = dx*dx + dy*dy, distance = Math.sqrt(d2), strength = Math.min(10, 4200 / Math.max(d2, 100));
        const fx = dx/distance * strength, fy = dy/distance * strength;
        forces[i].x += fx; forces[i].y += fy; forces[j].x -= fx; forces[j].y -= fy;
      }
      for (const edge of edges) {
        const i = index.get(edge.source), j = index.get(edge.target), dx = positions[j].x - positions[i].x, dy = positions[j].y - positions[i].y;
        const distance = Math.hypot(dx,dy) || 1; const preferred = 70 + (1-edge.similarity)*135;
        const force = (distance-preferred) * .008 * edge.similarity;
        forces[i].x += dx/distance * force; forces[i].y += dy/distance * force; forces[j].x -= dx/distance * force; forces[j].y -= dy/distance * force;
      }
      const cooling = .7 * (1 - iteration/iterations) + .2;
      positions.forEach((point, i) => { point.x = Math.max(65, Math.min(835, point.x + (forces[i].x + (450-point.x)*.008)*cooling)); point.y = Math.max(55, Math.min(490, point.y + (forces[i].y + (270-point.y)*.01)*cooling)); });
    }
    if(positions.length>1){
      const minX=Math.min(...positions.map(point=>point.x)),maxX=Math.max(...positions.map(point=>point.x));
      const minY=Math.min(...positions.map(point=>point.y)),maxY=Math.max(...positions.map(point=>point.y));
      const fit=Math.min(700/Math.max(1,maxX-minX),380/Math.max(1,maxY-minY),3);
      const centerX=(minX+maxX)/2,centerY=(minY+maxY)/2;
      positions.forEach(point=>{point.x=450+(point.x-centerX)*fit;point.y=270+(point.y-centerY)*fit;});
    }
  }
  function applyTransform() { $('network-transform').setAttribute('transform', `translate(${state.panX} ${state.panY}) translate(450 280) scale(${state.zoom}) translate(-450 -280)`); }
  function renderNetwork() {
    ensurePositions(); graphNodes = filteredNodes(); const ids = new Set(graphNodes.map(nodeId)); const threshold = +$('threshold').value;
    graphEdges = state.data.edges.filter(edge => ids.has(edge.source) && ids.has(edge.target) && edge.similarity >= threshold);
    $('threshold-value').textContent = number(threshold, 2);
    const edgesRoot = $('network-edges'), nodesRoot = $('network-nodes'); edgesRoot.replaceChildren(); nodesRoot.replaceChildren();
    for (const edge of graphEdges) {
      const a = state.positions.get(edge.source), b = state.positions.get(edge.target); const active = state.edge === edge;
      const dim = state.selected && edge.source !== state.selected && edge.target !== state.selected;
      const line = svg('line', {x1:a.x,y1:a.y,x2:b.x,y2:b.y,stroke:active?'#e29b68':'#6da5ad','stroke-width':active?3:Math.max(.8,edge.similarity*2.3),opacity:active ? 0.9 : dim ? 0.22 : 0.55});
      const hit = svg('line', {x1:a.x,y1:a.y,x2:b.x,y2:b.y,stroke:'transparent','stroke-width':12,class:'edge-hit',tabindex:0,role:'button','aria-label':`${nodeLabel(nodeById(edge.source))}와 ${nodeLabel(nodeById(edge.target))}, 유사도 ${number(edge.similarity,3)}`});
      hit.addEventListener('click', () => selectEdge(edge));
      hit.addEventListener('keydown', event => { if(event.key==='Enter'||event.key===' ') {event.preventDefault();selectEdge(edge);} });
      hit.addEventListener('mousemove', event => showTooltip(event, `${nodeLabel(nodeById(edge.source))} ↔ ${nodeLabel(nodeById(edge.target))}\n유사도 ${number(edge.similarity,3)} · 공통 ${text(edge.shared_metrics??edge.shared_fields)}개\n관측 범위 ${percent(edge.coverage)} · 클릭하여 차이 보기`)); hit.addEventListener('mouseleave',hideTooltip);
      edgesRoot.append(line,hit);
    }
    const degree = new Map(); for (const edge of graphEdges) {degree.set(edge.source,(degree.get(edge.source)||0)+1);degree.set(edge.target,(degree.get(edge.target)||0)+1);}
    for (const node of graphNodes) {
      const id=nodeId(node), point=state.positions.get(id), radius=9+Math.min(6,Math.sqrt(degree.get(id)||0)*1.5);
      const group=svg('g',{transform:`translate(${point.x} ${point.y})`,class:`network-node${id===state.selected?' selected':''}${id===state.compare?' comparison':''}`,tabindex:0,role:'button','aria-label':`${nodeLabel(node)}, 셀 ${text(node.cell_id)}, 사용 가능 지표 ${countAvailable(node)}개`,'data-node-id':id});
      group.append(svg('circle',{r:radius+5,class:'node-ring'}),svg('circle',{r:radius,fill:colorFor(node.cohort),class:'node-disc'}),svg('text',{y:radius+20,'text-anchor':'middle'},short(nodeLabel(node),22)));
      group.append(svg('title',{},`${nodeLabel(node)} · ${node.cohort}`));
      group.addEventListener('click', event => {event.stopPropagation();if(dragging?.moved)return;selectNode(id);});
      group.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();selectNode(id);}});
      group.addEventListener('pointerdown',event=>{event.stopPropagation();hideTooltip();const point=svgPoint(event);dragging={kind:'node',id,start:point,moved:false,pointer:event.pointerId};$('network-svg').setPointerCapture(event.pointerId);});
      group.addEventListener('mousemove',event=>{if(!dragging)showTooltip(event,`${nodeLabel(node)}\n셀 ${text(node.cell_id)} · ${text(node.protocol)}\n사용 가능 지표 ${countAvailable(node)}개 · 연결 ${degree.get(id)||0}개`);});group.addEventListener('mouseleave',hideTooltip);
      nodesRoot.append(group);
    }
    $('graph-count').textContent=`${graphNodes.length}개 실험 · ${graphEdges.length}개 연결`;
    $('graph-empty').hidden=graphNodes.length>0 && (graphEdges.length>0||graphNodes.length===1);
    $('graph-empty').textContent=graphNodes.length?`현재 기준의 연결이 없습니다. 유사도 기준과 공통 ${isDesign()?'설계 항목':'지표'} 범위를 확인하세요.`:'표시할 실험이 없습니다. 검색이나 실험군 선택을 확인하세요.';
    if(isDesign()&&(!state.data.metrics.length||state.data.metadata?.disabled_reason)){$('graph-empty').hidden=false;$('graph-empty').textContent='설계 정보 등록이 필요합니다. 활물질·전극 조성·로딩 항목과 실험별 값을 등록하면 설계 관계가 표시됩니다.';}
    const legend=$('network-legend');legend.replaceChildren();for(const cohort of [...new Set(graphNodes.map(node=>node.cohort))])legend.append(legendItem(cohort,colorFor(cohort)));
    applyTransform();renderStats();
  }
  function svgPoint(event) { const svgEl=$('network-svg');const point=svgEl.createSVGPoint();point.x=event.clientX;point.y=event.clientY;const matrix=svgEl.getScreenCTM();return matrix?point.matrixTransform(matrix.inverse()):{x:0,y:0}; }
  function selectNode(id) {state.selected=id;state.edge=null;if(nodeById(state.compare)?.cohort!==nodeById(id)?.cohort||state.compare===id)state.compare='';renderNetwork();renderInspector();renderFeatures();renderEdge();renderCrossview();}
  function selectEdge(edge) {state.edge=edge;state.selected=edge.source;state.compare=edge.target;hideTooltip();renderNetwork();renderInspector();renderFeatures();renderEdge();renderCrossview();$('edge-card').scrollIntoView({block:'nearest',behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'auto':'smooth'});}
  function renderInspector() {
    const node=nodeById(state.selected);$('selection-title').textContent=node?nodeLabel(node):'실험을 선택하세요';$('selection-badge').textContent=node?`${countAvailable(node)}개 ${isDesign()?'항목':'지표'}`:'';
    const metadata=$('selection-metadata');metadata.replaceChildren();
    if(node)for(const [label,value] of [['셀',node.cell_id],['시험',node.test_id],['프로토콜',node.protocol],['화학계',node.chemistry],['온도',finite(node.temperature_c)?`${number(node.temperature_c)} °C`:node.temperature_c],['실험군',node.cohort]]){const item=element('div');item.append(element('dt','',label),element('dd','',value));metadata.append(item);}
    const compare=$('compare-experiment');compare.replaceChildren(option('','비교할 실험을 선택하세요'));
    if(node)for(const other of state.data.nodes.filter(other=>other.cohort===node.cohort&&nodeId(other)!==state.selected))compare.append(option(nodeId(other),nodeLabel(other)));
    compare.value=state.compare;$('download-features').disabled=!node;renderRadar();
  }
  function radarRadius(value) {return(Math.max(-2,Math.min(2,value))+2)/4;}
  function renderRadar() {
    const root=$('radar-svg');root.replaceChildren();const legend=$('radar-legend');legend.replaceChildren();const metrics=selectedMetrics();const node=nodeById(state.selected);const compare=nodeById(state.compare);
    if(isDesign()){if(node)legend.append(legendItem(nodeLabel(node),'#278c91'));if(compare)legend.append(legendItem(nodeLabel(compare),'#e88a50'));renderDesignComparison();return;}
    if(!node||metrics.length<3){root.append(svg('text',{x:190,y:145,'text-anchor':'middle',class:'empty-chart'},node?'비교 지표를 3개 이상 선택하세요.':'네트워크에서 실험을 선택하세요.'));return;}
    const cx=190,cy=147,r=99;const polar=(index,ratio)=>{const angle=-Math.PI/2+index*2*Math.PI/metrics.length;return{x:cx+Math.cos(angle)*r*ratio,y:cy+Math.sin(angle)*r*ratio};};
    for(const ratio of [.25,.5,.75,1]){const points=metrics.map((_,i)=>{const p=polar(i,ratio);return`${p.x},${p.y}`;}).join(' ');root.append(svg('polygon',{points,fill:'none',stroke:ratio===.5?'#a9c8cd':'#e1eaee','stroke-width':ratio===.5?1.3:1,'stroke-dasharray':ratio===.5?'3 3':'none'}));}
    metrics.forEach((metric,i)=>{const point=polar(i,1);root.append(svg('line',{x1:cx,y1:cy,x2:point.x,y2:point.y,stroke:'#e4ecef'}));const label=polar(i,1.23);const labelEl=svg('text',{x:label.x,y:label.y+3,'text-anchor':'middle',fill:'#6a8290','font-size':9},short(metric.label||metricId(metric),18));labelEl.append(svg('title',{},`${metric.label||metricId(metric)} (${metric.unit||'단위 미지정'})`));root.append(labelEl);});
    root.append(svg('text',{x:cx+4,y:cy-r*.5+3,fill:'#91a7b0','font-size':8},'0'));
    for(const [current,color] of [[node,'#278c91'],[compare,'#e88a50']])if(current){
      const points=metrics.map((metric,i)=>{const value=feature(current,metricId(metric));return finite(value?.scaled)?polar(i,radarRadius(value.scaled)):null;});
      if(points.every(Boolean))root.append(svg('polygon',{points:points.map(p=>`${p.x},${p.y}`).join(' '),fill:color,'fill-opacity':.09,stroke:color,'stroke-width':1.8}));
      else for(let i=0;i<points.length;i++){const a=points[i],b=points[(i+1)%points.length];if(a&&b)root.append(svg('line',{x1:a.x,y1:a.y,x2:b.x,y2:b.y,stroke:color,'stroke-width':1.8}));}
      points.forEach((point,i)=>{if(!point)return;const value=feature(current,metricId(metrics[i]));const dot=svg('circle',{cx:point.x,cy:point.y,r:3.3,fill:color,stroke:'#fff','stroke-width':1});dot.append(svg('title',{},`${nodeLabel(current)} / ${metrics[i].label||metricId(metrics[i])}\n원값 ${number(value.value)} ${value.unit||metrics[i].unit||''}\n표준화 ${number(value.scaled,3)}${Math.abs(value.scaled)>2?' (표시 범위에서 잘림)':''}`));root.append(dot);});
      legend.append(legendItem(`${nodeLabel(current)}${points.some(p=>!p)?' · 결측 축 있음':''}`,color));
    }
  }
  function renderDesignComparison(){
    const node=nodeById(state.selected),compare=nodeById(state.compare),body=$('design-comparison-body');body.replaceChildren();
    $('design-left-label').textContent=node?short(nodeLabel(node),14):'선택 실험';$('design-right-label').textContent=compare?short(nodeLabel(compare),14):'비교 실험';
    if(!node||!state.metrics.length){emptyRow(body,3,node?'왼쪽에서 비교할 설계 항목을 선택하세요.':'설계 정보가 등록된 실험을 선택하세요.');return;}
    for(const metric of selectedMetrics()){const row=element('tr'),left=feature(node,metricId(metric)),right=feature(compare,metricId(metric));const label=tableCell(row,metric.label||metricId(metric),'design-field');const details=[metric.unit,metric.kind==='composition'?compositionBasis(metric.composition_basis):''].filter(Boolean);if(details.length)label.append(element('small','',details.join(' · ')));tableCell(row,featureValue(left?.value,metric.kind==='composition'?metric.unit:''),'design-value');tableCell(row,featureValue(right?.value,metric.kind==='composition'?metric.unit:''),'design-value');body.append(row);}
  }
  function renderCrossview(){
    const body=$('crossview-body');body.replaceChildren();const root=state.root,selected=state.selected;
    const graphDesign=root?.design_graph||{nodes:[],edges:[]},graphElectrochem=root||{nodes:[],edges:[]};
    const related=new Map();
    for(const [key,graph] of [['design',graphDesign],['electrochem',graphElectrochem]])for(const edge of list(graph.edges))if(edge.source===selected||edge.target===selected){const other=edge.source===selected?edge.target:edge.source;const entry=related.get(other)||{id:other};entry[key]=edge;related.set(other,entry);}
    const peers=[...related.values()].sort((a,b)=>{const difference=entry=>entry.design&&entry.electrochem?Math.abs(entry.design.similarity-entry.electrochem.similarity):-1;return difference(b)-difference(a)||Math.max(b.design?.similarity||0,b.electrochem?.similarity||0)-Math.max(a.design?.similarity||0,a.electrochem?.similarity||0)||a.id.localeCompare(b.id);});
    $('crossview-count').textContent=`${Math.min(peers.length,12)} / ${peers.length}개 관계`;
    if(!selected||!peers.length){emptyRow(body,4,selected?'이 실험의 비교 가능한 연결이 없습니다. 두 지도의 정보와 계산 기준을 확인하세요.':'네트워크에서 실험을 선택하세요.');return;}
    const allNodes=[...list(graphDesign.nodes),...list(graphElectrochem.nodes)];
    for(const entry of peers.slice(0,12)){const row=element('tr'),peer=allNodes.find(node=>nodeId(node)===entry.id),name=tableCell(row,'','metric-name');const activePeer=nodeById(entry.id),activeNode=nodeById(selected);if(activePeer&&activeNode&&activePeer.cohort===activeNode.cohort){const button=element('button','text-button',nodeLabel(peer));button.addEventListener('click',()=>{state.compare=entry.id;state.edge=list(state.data.edges).find(edge=>(edge.source===selected&&edge.target===entry.id)||(edge.target===selected&&edge.source===entry.id))||null;renderNetwork();renderInspector();renderEdge();});name.append(button);}else name.textContent=nodeLabel(peer);
      for(const key of ['design','electrochem']){const edge=entry[key],cell=tableCell(row,edge?number(edge.similarity,3):'—','numeric score-cell '+key);if(edge){const bar=element('div','distance-bar'),fill=element('span');fill.style.width=`${Math.max(0,Math.min(1,edge.similarity))*100}%`;bar.append(fill);cell.append(bar);cell.title=`공통 ${edge.shared_metrics??edge.shared_fields??'—'}개 · 관측 범위 ${percent(edge.coverage)}`;}else cell.title='다른 비교군, 미산출 또는 저장된 유사도 기준 미달';}
      tableCell(row,`설계 ${entry.design?.shared_metrics??entry.design?.shared_fields??'—'} / 지표 ${entry.electrochem?.shared_metrics??'—'}`);body.append(row);
    }
  }
  function renderFeatures() {
    const body=$('feature-body');body.replaceChildren();const node=nodeById(state.selected);const query=$('feature-search').value.toLowerCase().trim();
    const features=list(node?.features).filter(value=>!query||[value.metric_id,value.label,value.family].join(' ').toLowerCase().includes(query));
    if(!features.length){emptyRow(body,6,node?'조건에 맞는 지표가 없습니다.':'실험을 선택하세요.');return;}
    for(const value of features){const row=element('tr');const metric=metricById(value.metric_id),name=tableCell(row,value.label||value.metric_id,'metric-name');if(value.label&&value.label!==value.metric_id)name.append(element('small','',value.metric_id));if(metric.kind==='composition')name.append(element('small','',compositionBasis(metric.composition_basis)));tableCell(row,featureValue(value.value,metric.kind==='composition'?value.unit:''),isDesign()?'design-value':'numeric');tableCell(row,value.unit||'—');tableCell(row,text(value.valid_count),'numeric electrochem-only');tableCell(row,`${number(value.first_life)} – ${number(value.last_life)}`,'numeric electrochem-only');const status=tableCell(row,'');status.append(statusTag(value.status||(hasValue(value.value)?'valid':'missing')));body.append(row);}
  }
  function renderEdge() {
    const edge=state.edge;$('edge-card').hidden=!edge;if(!edge)return;
    const source=nodeById(edge.source),target=nodeById(edge.target);$('edge-title').textContent=`${nodeLabel(source)} ↔ ${nodeLabel(target)}`;$('edge-description').textContent=`${isDesign()?'설계':'전기화학'} 유사도 ${number(edge.similarity,3)} · 공통 ${text(edge.shared_metrics??edge.shared_fields)}개 · 관측 범위 ${percent(edge.coverage)}. ${isDesign()?'가중 거리 기여가 큰 항목부터 표시합니다.':'표준화 거리가 큰 지표부터 표시합니다.'}`;$('edge-left-label').textContent=`A · ${nodeLabel(source)}`;$('edge-right-label').textContent=`B · ${nodeLabel(target)}`;
    const contribution=item=>isDesign()&&finite(item.weighted_distance)?item.weighted_distance:item.standardized_distance||0;
    const body=$('edge-body');body.replaceChildren();const contributors=list(edge.contributors).slice().sort((a,b)=>contribution(b)-contribution(a));const max=Math.max(.001,...contributors.map(contribution));
    for(const item of contributors){const row=element('tr');const metric=metricById(item.metric_id),unit=item.unit||metric.unit||'';const name=tableCell(row,item.label||item.metric_id,'metric-name');name.append(element('small','',[unit||'단위 없음',metric.kind==='composition'?compositionBasis(metric.composition_basis):''].filter(Boolean).join(' · ')));tableCell(row,featureValue(item.left,metric.kind==='composition'?unit:''),isDesign()?'design-value':'numeric');tableCell(row,featureValue(item.right,metric.kind==='composition'?unit:''),isDesign()?'design-value':'numeric');tableCell(row,number(item.raw_delta),'numeric');const distance=tableCell(row,number(contribution(item),3),'numeric');if(isDesign())distance.append(element('small','',`거리 ${number(item.standardized_distance,3)} · 가중치 ${number(item.weight)}`));const bar=element('div','distance-bar'),fill=element('span');fill.style.width=`${100*contribution(item)/max}%`;bar.append(fill);distance.append(bar);body.append(row);}
    if(!contributors.length)emptyRow(body,5,'기여 지표 정보가 없습니다.');
  }
  function correlationColor(value) {if(!finite(value))return'#edf1f3';const target=value>=0?[41,141,150]:[215,137,110],base=[245,247,248],a=Math.abs(value);return`rgb(${base.map((v,i)=>Math.round(v+(target[i]-v)*a)).join(',')})`;}
  function getCorrelation(left,right){return list(state.data.metric_correlations).find(item=>item.cohort===state.cohort&&((item.left_metric===left&&item.right_metric===right)||(item.left_metric===right&&item.right_metric===left)));}
  function inspectCorrelation(entry,left,right){$('correlation-pair-title').textContent=`${metricById(left).label||left} × ${metricById(right).label||right}`;$('correlation-pair-value').textContent=entry&&finite(entry.rho)?number(entry.rho,3):'—';$('correlation-pair-value').style.color=entry&&finite(entry.rho)&&entry.rho<0?'#bb785c':'#278c91';$('correlation-pair-details').replaceChildren();for(const line of [`셀 ${entry?text(entry.n_cells):'0'}개 · 비교 실험 ${entry?text(entry.n_entities):'0'}개`,`분석 단위: 셀별 중앙값`,`상태: ${entry?statusText(entry.status):'계산 결과 없음'}`])$('correlation-pair-details').append(element('div','',line));}
  function renderHeatmap(){
    const root=$('heatmap-svg');root.replaceChildren();const metrics=selectedMetrics(),box=56,left=163,top=130,size=metrics.length*box;
    $('correlation-pair-title').textContent='칸을 선택하세요';$('correlation-pair-value').textContent='—';$('correlation-pair-details').textContent='각 칸의 n은 비교 가능한 독립 셀 수입니다.';
    root.setAttribute('viewBox',`0 0 ${left+size+25} ${top+size+25}`);root.setAttribute('width',left+size+25);root.setAttribute('height',Math.max(250,top+size+25));
    if(isDesign())return;
    if(!state.cohort||!metrics.length){root.append(svg('text',{x:15,y:80,class:'empty-chart'},!state.cohort?'한 실험군을 선택하면 군내 상관행렬을 볼 수 있습니다.':'왼쪽에서 비교 지표를 선택하세요.'));return;}
    metrics.forEach((metric,i)=>{const label=metric.label||metricId(metric);const rowLabel=svg('text',{x:left-10,y:top+i*box+box/2+4,'text-anchor':'end',fill:'#68818f','font-size':10},short(label,23));rowLabel.append(svg('title',{},label));root.append(rowLabel);const colLabel=svg('text',{transform:`translate(${left+i*box+box/2} ${top-10}) rotate(-42)`,'text-anchor':'start',fill:'#68818f','font-size':10},short(label,23));colLabel.append(svg('title',{},label));root.append(colLabel);});
    metrics.forEach((leftMetric,row)=>metrics.forEach((rightMetric,col)=>{const l=metricId(leftMetric),r=metricId(rightMetric),entry=getCorrelation(l,r),rho=entry?.rho;const group=svg('g',{class:'heatmap-cell',tabindex:0,role:'button','aria-label':`${leftMetric.label||l} 대 ${rightMetric.label||r}: ${number(rho,2)}, 셀 ${entry?.n_cells||0}개`});group.append(svg('rect',{x:left+col*box+2,y:top+row*box+2,width:box-4,height:box-4,rx:5,fill:correlationColor(rho)}));group.append(svg('text',{x:left+col*box+box/2,y:top+row*box+box/2-1,'text-anchor':'middle',fill:finite(rho)&&Math.abs(rho)>.65?'#fff':'#476370','font-size':12,'font-weight':600},number(rho,2)));group.append(svg('text',{x:left+col*box+box/2,y:top+row*box+box/2+14,'text-anchor':'middle',fill:finite(rho)&&Math.abs(rho)>.65?'#e5f1f2':'#91a2aa','font-size':8},`n=${entry?.n_cells||0}`));const inspect=()=>inspectCorrelation(entry,l,r);group.addEventListener('click',inspect);group.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();inspect();}});group.addEventListener('mousemove',event=>showTooltip(event,`${leftMetric.label||l} × ${rightMetric.label||r}\nSpearman ρ ${number(rho,3)} · 셀 ${entry?.n_cells||0}개\n${entry?statusText(entry.status):'계산 결과 없음'}`));group.addEventListener('mouseleave',hideTooltip);root.append(group);}));
  }
  function renderQuality(){
    const ingestion=state.data.ingestion||{},summary=$('ingestion-cards');summary.replaceChildren();
    for(const [label,value] of [['처리 완료',ingestion.processed??0],['캐시 재사용',ingestion.reused??0],['대기',list(ingestion.pending).length],['실패',list(ingestion.failed).length]]){const card=element('div');card.append(element('span','',label),element('strong','',Array.isArray(value)?value.length:value));summary.append(card);}
    const files=$('ingestion-body');files.replaceChildren();let rows=0;
    const fileRows=[...list(ingestion.failed).map(item=>({...item,status:'failed'})),...list(ingestion.pending).map(item=>({...item,status:'pending'})),...list(ingestion.records)];
    for(const item of fileRows){const row=element('tr');tableCell(row,item.path||item.source_path||item.experiment_id||item.id||'—','metric-name');const status=tableCell(row,'');status.append(statusTag(item.status||'processed'));tableCell(row,item.error||(item.reason?statusText(item.reason):'')||item.message||(item.run_id?`실행 ${item.run_id}`:'—'));files.append(row);rows++;}
    if(!rows)emptyRow(files,3,state.data.is_demo?'예시 데이터에는 raw 파일 수집 기록이 없습니다.':'수집 기록이 없습니다. 등록 설정을 통해 raw 파일을 추가하세요.');
    const excluded=$('excluded-body');excluded.replaceChildren();let exclusions=0;
    for(const issue of list(ingestion.issues)){const row=element('tr');tableCell(row,issue.experiment_id||'수집 검증');tableCell(row,issue.metric_id||issue.path||'—','metric-name');tableCell(row,`${statusText(issue.reason)}${issue.rows!==undefined?' · '+text(issue.rows)+'행':''}`);excluded.append(row);exclusions++;}
    for(const cohort of list(state.data.cohorts).filter(item=>!state.cohort||item.cohort===state.cohort))for(const metric of list(cohort.excluded_metrics)){const row=element('tr');tableCell(row,cohort.cohort);tableCell(row,metricById(metric.metric_id).label||metric.metric_id,'metric-name');tableCell(row,statusText(metric.reason));excluded.append(row);exclusions++;}
    for(const pair of list(state.data.excluded_pairs).filter(item=>!state.cohort||item.cohort===state.cohort)){const row=element('tr');tableCell(row,pair.cohort);tableCell(row,`${nodeLabel(nodeById(pair.source))} ↔ ${nodeLabel(nodeById(pair.target))}`,'metric-name');tableCell(row,`${statusText(pair.reason)} · 공통 ${text(pair.shared_metrics??pair.shared_fields)}개 · 관측 ${percent(pair.coverage)}`);excluded.append(row);exclusions++;}
    if(!exclusions)emptyRow(excluded,3,'선택 범위에서 제외된 항목이 없습니다.');
    const coverage=$('coverage-body');coverage.replaceChildren();for(const node of scopeNodes()){const row=element('tr');tableCell(row,nodeLabel(node),'metric-name');tableCell(row,`${text(node.cell_id)} / ${text(node.cohort)}`);const cell=tableCell(row,'');const meter=element('span','coverage-meter'),fill=element('span');const total=list(node.features).length;fill.style.width=`${total?Math.min(100,100*countAvailable(node)/total):0}%`;meter.append(fill);cell.append(meter,document.createTextNode(`${countAvailable(node)}개`));tableCell(row,`${list(node.features).filter(item=>hasValue(item.value)).length} / ${total}`);coverage.append(row);}if(!scopeNodes().length)emptyRow(coverage,4,'표시할 실험이 없습니다.');
  }
  function renderAll(){renderNetwork();renderInspector();renderFeatures();renderEdge();renderCrossview();renderHeatmap();renderQuality();}
  function showPanel(name){state.panel=name;document.querySelectorAll('.tab').forEach(tab=>{const selected=tab.dataset.panel===name;tab.classList.toggle('active',selected);tab.setAttribute('aria-selected',String(selected));});for(const id of ['network','correlation','quality'])$('panel-'+id).hidden=id!==name;hideTooltip();}
  function downloadFeatures(){const node=nodeById(state.selected);if(!node)return;const quote=value=>{let rendered=value!==null&&typeof value==='object'?JSON.stringify(value):text(value);if(typeof value==='string'&&/^\s*[=+\-@\t\r]/.test(rendered))rendered="'"+rendered;return '"'+rendered.replaceAll('"','""')+'"';};const keys=['metric_id','label','value','unit','scaled','valid_count','first_life','last_life','status'];const rows=[keys,...list(node.features).map(item=>keys.map(key=>item[key]??''))];const csv='\ufeff'+rows.map(row=>row.map(quote).join(',')).join('\r\n');const link=document.createElement('a'),url=URL.createObjectURL(new Blob([csv],{type:'text/csv;charset=utf-8'}));link.href=url;link.download=`${nodeId(node).replace(/[^\w\uAC00-\uD7A3.-]/g,'_')}_features.csv`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
  async function refresh(manual=false){
    if(refreshing||state.imported&&!manual)return;
    if(!location.protocol.startsWith('http')){if(manual)location.reload();return;}
    refreshing=true;
    try{const response=await fetch('data.json',{cache:'no-store'});if(!response.ok)throw new Error(`갱신 확인 실패 (${response.status})`);const payload=await response.json();safeData(payload);if(manual||payload.generated_at!==state.root.generated_at)setPayload(payload,false);else $('refresh-state').textContent='로컬 서버 · 최신 데이터';}
    catch(error){$('refresh-state').textContent='갱신 대기 · 기존 결과 표시 중';if(manual){$('error-banner').textContent=error.message;$('error-banner').hidden=false;}}
    finally{refreshing=false;}
  }
  $('cohort').addEventListener('change',()=>{state.cohort=$('cohort').value;state.selected=scopeNodes()[0]?nodeId(scopeNodes()[0]):'';state.compare='';state.edge=null;state.zoom=1;state.panX=state.panY=0;renderAll();});
  $('experiment-search').addEventListener('input',renderNetwork);$('metric-search').addEventListener('input',renderMetricOptions);$('feature-search').addEventListener('input',renderFeatures);$('threshold').addEventListener('input',renderNetwork);$('neighbors-only').addEventListener('change',renderNetwork);
  $('compare-experiment').addEventListener('change',()=>{state.compare=$('compare-experiment').value;state.edge=state.data.edges.find(edge=>(edge.source===state.selected&&edge.target===state.compare)||(edge.target===state.selected&&edge.source===state.compare))||null;renderNetwork();renderRadar();renderEdge();});
  $('edge-close').addEventListener('click',()=>{state.edge=null;renderEdge();renderNetwork();});$('download-features').addEventListener('click',downloadFeatures);
  $('reset-filters').addEventListener('click',()=>{$('experiment-search').value='';$('metric-search').value='';$('feature-search').value='';$('neighbors-only').checked=false;$('threshold').value=Math.max(.65,+$('threshold').min);state.metrics=defaultMetricIds();state.zoom=1;state.panX=state.panY=0;renderMetricOptions();renderAll();});
  $('zoom-in').addEventListener('click',()=>{state.zoom=Math.min(3,state.zoom*1.2);applyTransform();});$('zoom-out').addEventListener('click',()=>{state.zoom=Math.max(.45,state.zoom/1.2);applyTransform();});$('zoom-reset').addEventListener('click',()=>{state.zoom=1;state.panX=state.panY=0;applyTransform();});
  $('network-svg').addEventListener('pointerdown',event=>{if(event.target.closest('.edge-hit'))return;const point=svgPoint(event);dragging={kind:'pan',start:point,x:state.panX,y:state.panY,moved:false,pointer:event.pointerId};$('network-svg').setPointerCapture(event.pointerId);hideTooltip();});
  $('network-svg').addEventListener('pointermove',event=>{if(!dragging)return;const point=svgPoint(event),dx=point.x-dragging.start.x,dy=point.y-dragging.start.y;if(Math.abs(dx)+Math.abs(dy)>4)dragging.moved=true;if(dragging.kind==='pan'){state.panX=dragging.x+dx;state.panY=dragging.y+dy;applyTransform();}else{const position=state.positions.get(dragging.id);position.x=(point.x-state.panX-450)/state.zoom+450;position.y=(point.y-state.panY-280)/state.zoom+280;renderNetwork();}});
  function endDrag(event){if(!dragging)return;const previous=dragging;if($('network-svg').hasPointerCapture(event.pointerId))$('network-svg').releasePointerCapture(event.pointerId);if(previous.kind==='node'&&!previous.moved)selectNode(previous.id);setTimeout(()=>{if(dragging===previous)dragging=null;},0);}
  $('network-svg').addEventListener('pointerup',endDrag);$('network-svg').addEventListener('pointercancel',()=>{dragging=null;});
  document.querySelectorAll('.tab').forEach(tab=>tab.addEventListener('click',()=>showPanel(tab.dataset.panel)));
  document.querySelectorAll('.view-option').forEach(button=>button.addEventListener('click',()=>switchMode(button.dataset.mode)));
  $('reload').addEventListener('click',()=>refresh(true));
  $('data-upload').addEventListener('change',async event=>{const file=event.target.files[0];if(!file)return;try{const data=JSON.parse(await file.text());setPayload(data,true);}catch(error){$('error-banner').textContent=error.message;$('error-banner').hidden=false;}finally{event.target.value='';}});
  window.addEventListener('blur',hideTooltip);
  try{setPayload(JSON.parse($('relations-data').textContent));}catch(error){$('error-banner').textContent=`데이터를 열지 못했습니다: ${error.message}`;$('error-banner').hidden=false;}
  setInterval(()=>{if(state.data&&!document.hidden)refresh(false);},10000);
})();
