(()=>{
"use strict";
const data=JSON.parse(document.getElementById("trace-data").textContent);
const names={material_lot:"소재 로트",electrode_lot:"전극·막 로트",process_run:"공정",cell:"물리 셀",record:"시험",dataset:"raw/Parquet"};
const colors={material_lot:"#e5e9f8",electrode_lot:"#e9e3f4",process_run:"#f8ebd5",cell:"#dceee6",record:"#daebf3",dataset:"#e9efef"};
const statusNames={confirmed:"확정",draft:"초안",needs_review:"재검토",retired:"사용 중지",unlinked:"미연결"};
const $=id=>document.getElementById(id), nodes=new Map(data.nodes.map(n=>[n.id,n])), docs=new Map(data.documents.map(d=>[d.document_id,d])), evidence=new Map(data.evidence.map(e=>[e.evidence_id,e]));
const enabled=new Set(Object.keys(names)), NS="http://www.w3.org/2000/svg";
function el(tag,text,cls){const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(cls)e.className=cls;return e}
function svg(tag,attrs,text){const e=document.createElementNS(NS,tag);for(const [k,v]of Object.entries(attrs||{}))e.setAttribute(k,v);if(text!==undefined)e.textContent=text;return e}
for(const [kind,title]of Object.entries(names)){const label=el("label");const box=el("input");box.type="checkbox";box.checked=true;box.onchange=()=>{box.checked?enabled.add(kind):enabled.delete(kind);draw()};label.append(box,document.createTextNode(" "+title));$("types").append(label)}
for(const n of data.nodes){const option=el("option",names[n.kind]+" · "+n.label);option.value=n.id;$("focus").append(option)}
$("focus").value=data.initial_focus;
$("summary").textContent="항목 "+data.nodes.length+"개 · 연결 "+data.edges.length+"개 · PPT 검토 대기 "+data.pending+"개 · 재검토 연결 "+data.edges.filter(e=>e.status==="needs_review").length+"개";
function closure(start,edges,up,down){const result=new Set([start]),pending=[start];while(pending.length){const id=pending.pop();for(const e of edges){const next=up&&e.target===id?e.source:down&&e.source===id?e.target:null;if(next&&!result.has(next)){result.add(next);pending.push(next)}}}return result}
function select(item,isEdge=false){
 const box=$("detail"),proofBox=el("div"),fields=item.data.field_evidence||{};
 const titles={name:"항목명",process_type:"공정",performed_on:"공정·제조일",physical_cell_id:"물리 셀 ID",lot_id:"로트 ID",sample_id:"샘플 ID",sample_name:"샘플명",experiment_name:"시험",test_date:"시험 날짜",date_kind:"날짜 의미",set_no:"세트번호",namespace:"번호 사용 범위",test_type:"시험 종류",rpt_stage:"RPT 시점",channel_id:"채널",design_id:"설계 ID",batch_id:"제조 배치",material:"소재",supplier:"공급사",fabrication_method:"습식·건식",current_collector:"집전체",composition_basis:"조성 기준",electrode_side:"양극·음극",equipment_id:"설비",operator:"작업자",pressure_mpa:"구동압(MPa)",temperature_c:"시험온도(°C)",pressing_pressure:"압착압",mass_loading:"로딩",active_fraction:"활물질 비율",film_thickness:"막두께",process_run_id:"공정 실행 ID"};
 function showProof(ids){proofBox.replaceChildren(el("h3","장표 원문 · 슬라이드 근거"));if(!ids.length)proofBox.append(el("p","직접 근거가 없으면 연결된 시험·공정 항목을 확인하세요."));
  for(const id of new Set(ids)){const e=evidence.get(id);if(!e)continue;const d=docs.get(e.document_id),proof=el("div",undefined,"proof");proof.append(el("strong",(d?.name||e.document_id)+" · "+e.locator),el("p",e.text||"이미지·도표 근거: 원본에서 직접 확인 필요"));
   if(d?.proof?.valid&&d.proof.uri?.startsWith("file:")){const a=el("a","보관 장표 열기");a.href=d.proof.uri;a.target="_blank";a.rel="noopener";proof.append(a)}else proof.append(el("p","장표 사본이 없거나 변경됨","warn"));proofBox.append(proof)}
 }
 function fact(key,value,refs){if(value===null||value===undefined||value==="")return;const row=el("p");row.append(el("strong",(titles[key]||key)+" · "),document.createTextNode(String(value)));
  if(refs?.length){const button=el("button","근거");button.style.marginLeft="7px";button.style.padding="2px 6px";button.onclick=()=>showProof(refs);row.append(button)}box.append(row)}
 box.replaceChildren(el("h2",item.label),el("span",statusNames[item.status]||item.status,"badge"+(item.status==="confirmed"?"":" warn")));
 if(!isEdge){const btn=el("button","이 항목의 연결 보기");btn.onclick=()=>{$("focus").value=item.id;draw()};box.append(btn)}
 const ids=new Set(item.evidence_ids||[]);
 if(isEdge){fact("연결",nodes.get(item.source)?.label+" → "+nodes.get(item.target)?.label);fact("역할",item.data.role);fact("메모",item.data.notes)}
 else if(item.kind==="dataset"){
  fact("파일",item.data.relative_path);fact("보존 기록 수",item.data.rows?.toLocaleString());fact("전압·용량 단위",item.data.metadata.voltage_unit&&item.data.metadata.capacity_unit?item.data.metadata.voltage_unit+" / "+item.data.metadata.capacity_unit:"원본 단위 미확인");
  for(const e of data.edges.filter(e=>e.target===item.id)){const record=nodes.get(e.source);if(record)for(const id of record.evidence_ids||[])ids.add(id);fact("파일 무결성",e.data.raw_integrity==="verified_now"?"현재 파일 해시 확인":"등록 시점 기준 · 현재 파일 재검사 안 함")}
 }else{
  const keys=item.kind==="record"?["sample_id","sample_name","physical_cell_id","experiment_name","test_date","date_kind","set_no","namespace","test_type","rpt_stage","channel_id","design_id","batch_id"]:["process_type","performed_on"];
  for(const key of keys)fact(key,item.data[key],fields[key]||item.evidence_ids);
  for(const [key,value]of Object.entries(item.data.attributes||{}))fact(key,value,fields["attributes."+key]||item.evidence_ids);
  for(const [key,value]of Object.entries(item.data.parameters||{}))fact(key,String(value.value)+" "+value.unit,fields["parameters."+key]||item.evidence_ids);
  for(const [key,value]of Object.entries(item.data.metadata||{}))fact(key,value,fields[key]||item.evidence_ids);
  fact("메모",item.data.notes);
 }
 const provenance=el("details");provenance.append(el("summary","버전·출처 상세"),el("pre",JSON.stringify(item.data,null,2)));box.append(provenance,proofBox);showProof([...ids]);
}
function draw(){
 const review=$("review").checked,showUnlinked=$("unlinked").checked,focus=$("focus").value,query=$("search").value.toLocaleLowerCase();
 let edges=data.edges.filter(e=>review||e.status==="confirmed");
 const direction=$("direction").value;
 let allowed=focus?closure(focus,edges,direction!=="down",direction!=="up"):null;
 let visible=data.nodes.filter(n=>enabled.has(n.kind)&&(review||n.status==="confirmed"||n.kind==="dataset"&&showUnlinked)&&(!allowed||allowed.has(n.id))&&(showUnlinked||n.kind!=="dataset"||data.edges.some(e=>e.target===n.id)));
 if(query){const hits=new Set(visible.filter(n=>JSON.stringify(n).toLocaleLowerCase().includes(query)).map(n=>n.id));let related=new Set(hits);for(const id of hits)for(const next of closure(id,edges,true,true))related.add(next);visible=visible.filter(n=>related.has(n.id))}
 const ids=new Set(visible.map(n=>n.id));edges=edges.filter(e=>ids.has(e.source)&&ids.has(e.target));
 const rank=new Map(visible.map(n=>[n.id,0])),degree=new Map(visible.map(n=>[n.id,0]));
 for(const e of edges)degree.set(e.target,degree.get(e.target)+1);
 const queue=visible.filter(n=>!degree.get(n.id)).map(n=>n.id);
 for(let i=0;i<queue.length;i++){const id=queue[i];for(const e of edges.filter(e=>e.source===id)){rank.set(e.target,Math.max(rank.get(e.target),rank.get(id)+1));degree.set(e.target,degree.get(e.target)-1);if(!degree.get(e.target))queue.push(e.target)}}
 const groups=new Map(),positions=new Map();
 for(const n of visible){const r=rank.get(n.id);if(!groups.has(r))groups.set(r,[]);groups.get(r).push(n)}
 for(const [r,items]of groups){items.sort((a,b)=>(a.date||"").localeCompare(b.date||"")||a.label.localeCompare(b.label));items.forEach((n,i)=>positions.set(n.id,{x:30+r*270,y:35+i*105}))}
 const graph=$("graph");graph.replaceChildren();const defs=svg("defs"),marker=svg("marker",{id:"arrow",viewBox:"0 0 10 10",refX:9,refY:5,markerWidth:6,markerHeight:6,orient:"auto-start-reverse"});marker.append(svg("path",{d:"M 0 0 L 10 5 L 0 10 z",fill:"#9cb4ab"}));defs.append(marker);graph.append(defs);
 const width=Math.max(900,...[...positions.values()].map(p=>p.x+260)),height=Math.max(420,...[...positions.values()].map(p=>p.y+95));graph.setAttribute("width",width);graph.setAttribute("height",height);
 for(const e of edges){const a=positions.get(e.source),b=positions.get(e.target);const path=svg("path",{d:"M "+(a.x+225)+" "+(a.y+33)+" C "+(a.x+250)+" "+(a.y+33)+", "+(b.x-25)+" "+(b.y+33)+", "+b.x+" "+(b.y+33),class:"edge"+(e.status==="confirmed"?"":" review"),"marker-end":"url(#arrow)"});path.onclick=()=>select(e,true);graph.append(path);graph.append(svg("text",{x:(a.x+225+b.x)/2,y:(a.y+b.y)/2+24,class:"edge-label"},e.data.role||e.label))}
 for(const n of visible){const p=positions.get(n.id),g=svg("g",{class:"node",transform:"translate("+p.x+","+p.y+")"});g.append(svg("rect",{width:225,height:70,rx:11,fill:colors[n.kind],stroke:n.status==="confirmed"?"#c4d5ce":"#d4a569","stroke-width":n.id===focus?3:1}));g.append(svg("text",{x:12,y:20,class:"minor"},names[n.kind]+" · "+(statusNames[n.status]||n.status)));g.append(svg("text",{x:12,y:42},n.label.length>23?n.label.slice(0,22)+"…":n.label));g.append(svg("text",{x:12,y:58,class:"minor"},n.date||"날짜 미기재"));g.onclick=()=>select(n);g.append(svg("title",{},n.label));graph.append(g)}
 $("notice").textContent=visible.length?visible.length+"개 항목 · "+edges.length+"개 연결. 회색·초안 제외 상태에서는 확인된 연결만 따라갑니다.":"표시할 연결이 없습니다. PPT 근거와 셀·시험 연결을 먼저 등록하세요.";
 const timeline=$("timeline");timeline.replaceChildren();visible.filter(n=>n.kind!=="dataset").sort((a,b)=>(a.date||"9999").localeCompare(b.date||"9999")).forEach(n=>{const row=el("div",(n.date||"날짜 미기재")+" · "+names[n.kind]+" · "+n.label);row.onclick=()=>select(n);timeline.append(row)});
}
for(const id of ["search","focus","direction","review","unlinked"])$(id).addEventListener(id==="search"?"input":"change",draw);
draw();if(data.initial_focus&&nodes.has(data.initial_focus))select(nodes.get(data.initial_focus));
window.BatteryInsightTrace={draw,data,select};
})();
