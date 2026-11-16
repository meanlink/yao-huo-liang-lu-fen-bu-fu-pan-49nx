import {api} from '../api/client.js';
import {workspace} from '../stores/workspace.js';
import {session} from '../stores/session.js';
import {heading} from '../components/common/shell.js';
import {table} from '../components/common/table.js';
import {esc,fmt,date,notify} from '../utils/dom.js';
import {submit} from '../hooks/forms.js';

const STATUS={draft:['待结账','draft'],stale:['待核对 / 已失效','stale'],closing:['入账中断','closing'],closed:['已结账','closed']};
const KIND={allocated:'窑次分摊',idle:'闲置（无窑运行）',unresolved:'待核对时段'};
// 录入、显示与后端一致按 UTC 处理，避免浏览器时区造成 8 小时偏差。
const iso=v=>v?new Date(Number(v)*1000).toISOString().slice(0,16):'';
const toEpoch=v=>v?Date.parse(v+'Z')/1000:null;

function localTime(value){
 const n=Number(value);if(!Number.isFinite(n))return '—';
 return new Date(n*1000).toLocaleString('zh-CN',{hour12:false,timeZone:'UTC'})+' UTC';
}

function statusBadge(status){
 const [label,cls]=STATUS[status]||[status,status];
 return '<span class="status '+esc(cls)+'">'+esc(label)+'</span>';
}

function equality(s){
 const ok=Math.abs((s.allocated_kwh+s.idle_kwh)-s.delta_kwh)<1e-6;
 const withPending=s.pending_count===0?ok:false;
 return '<div class="balance '+(withPending?'ok':'pending')+'"><span>表差</span><strong>'+fmt(s.delta_kwh)+' kWh</strong><span>=</span><span>窑次分摊</span><strong>'+fmt(s.allocated_kwh)+' kWh</strong><span>+</span><span>闲置</span><strong>'+fmt(s.idle_kwh)+' kWh</strong>'+(s.pending_count?'<span>+</span><span>待核对</span><strong>'+fmt(s.unresolved_kwh)+' kWh</strong>':'')+'<em>'+(s.pending_count?'待核对时段补齐后方可结账':(ok?'表差已配平':'差额未配平'))+'</em></div>';
}

export async function accountingPage(id){
 const [list,meters,firings,corrections]=await Promise.all([
   api('/accounting/settlements'),api('/accounting/meters'),
   api('/accounting/firings'),api('/accounting/corrections')]);
 workspace.settlements=list;
 if(!list.length){
   return heading('06 / METER LEDGER','电表结账与逐窑耗电','按带修订号的累计读数、窑次起止与装窑量分摊共用电表；无窑运行电量单列，分摊与闲置之和等于表差。')+
     '<section class="panel"><div class="empty">还没有结账单。先在下方登记电表与窑次，再创建一次结账。<a href="#/accounting/data">登记上游数据 ↗</a></div></section>'+
     ledgerSection(meters,firings);
 }
 const selected=id?await api('/accounting/settlements/'+id):await api('/accounting/settlements/'+list[0].id);
 workspace.selectedSettlement=selected.id;
 const s=selected;
 const admin=session.user.role==='admin';
 const writable=session.user.role!=='viewer';
 const canClose=admin&&(s.status==='draft');
 const equalOk=s.pending_count===0&&Math.abs((s.allocated_kwh+s.idle_kwh)-s.delta_kwh)<1e-6;

 return heading('06 / METER LEDGER','电表结账与逐窑耗电',
   '累计读数带修订号；同一抄表时刻取最高修订。累计回退须登记换表衔接，缺读数或衔接不清的时段待核对，不准结账。',
   '<button class="button" id="refresh-accounting">刷新账目 ↻</button>')+
 '<div class="result-selector"><label>结账单<select id="select-settlement">'+list.map(x=>'<option value="'+x.id+'" '+(x.id===s.id?'selected':'')+'>'+esc(x.reference)+' · '+esc(x.name)+'</option>').join('')+'</select></label>'+
 statusBadge(s.status)+'<span>版本 '+s.revision+' · 数据修订 '+s.input_revision+'</span><span class="approval">'+esc(s.group_code)+'</span></div>'+
 (s.status==='stale'?'<p class="warning">该分摊已失效：'+esc(s.stale_reason||'待核对时段未补齐')+'。'+(s.recalc_count?'已按受影响时段重算 '+s.recalc_count+' 次。':'')+'请补齐读数或换表衔接后重算，结账会被拒绝。</p>':'')+
 (s.status==='closing'?'<p class="warning">记账中断：已入账 '+((s.posting_progress?.posted_rows||[]).length)+' 行，进度已保留。使用原结账请求号重试或点击“恢复入账”，不会重复入账。</p>':'')+
 '<section class="result-lead"><p class="eyebrow">SETTLEMENT BALANCE</p><h2>'+esc(s.start_text)+' — '+esc(s.end_text)+' · '+esc(s.name)+'</h2>'+equality(s)+
   '<div class="metrics">'+[
     ['逐窑分摊合计',s.allocated_kwh,'kWh'],['闲置电量',s.idle_kwh,'kWh'],['待核对时段',s.pending_count,'段'],
     ['重算次数',s.recalc_count,'次'],['更正差额',(s.corrections||[]).reduce((a,c)=>a+c.total_delta_kwh,0),'kWh']
   ].map(([k,v,u])=>'<div><span>'+k+'</span><strong>'+esc(fmt(v))+'<small>'+u+'</small></strong></div>').join('')+'</div></section>'+
 '<div class="accounting-actions">'+
   (s.status==='closing'?'<button class="button" id="resume-settlement" '+(admin?'':'disabled')+'>恢复未完成入账</button>':'')+
   (canClose?'<button class="primary" id="close-settlement">按当前版本结账</button>':'')+
   (s.status==='stale'?'<button class="button primary" id="recompute-settlement" '+(writable?'':'disabled')+'>按受影响时段重算</button>':'')+
   (s.status==='closed'?'<span class="hint">已结账：更正只记差额，不改旧账。'+esc(s.closed_at?date(s.closed_at):'')+'</span>':'')+
 '</div>'+
 '<section class="panel"><div class="section-title"><h2>逐窑分摊与每合格件耗电</h2><span>装窑量加权 · '+s.rows.length+' 窑次</span></div>'+
 table([{key:'batch',label:'窑次'},{key:'kiln_code',label:'窑线'},{key:'start_text',label:'开窑'},{key:'end_text',label:'停窑'},
        {key:'loaded',label:'装窑量'},{key:'passed',label:'合格件'},{key:'yield_pct',label:'良率 %'},
        {key:'allocated_kwh',label:'耗电 kWh'},{key:'kwh_per_passed',label:'每合格件 kWh'},{key:'posted',label:'已入账'}],
       s.rows.map(r=>({...r,posted:r.posted?'是':'否'})),'窗口内没有窑次记录，电量全部计入闲置。')+'</section>'+
 '<section class="panel"><div class="section-title"><h2>时段切分与闲置电量</h2><span>分摊 + 闲置'+(s.pending_count?' + 待核对':'')+' = 表差</span></div>'+
 table([{key:'seq',label:'序'},{key:'start_text',label:'开始'},{key:'end_text',label:'结束'},{key:'kind',label:'类型'},
        {key:'delta_kwh',label:'电量 kWh'},{key:'firing_count',label:'在烧窑次'},{key:'breakdown',label:'分摊明细'}],
       s.slices.map(x=>({...x,kind:KIND[x.kind]||x.kind,
         breakdown:(x.breakdown||[]).map(([b,v])=>b+' '+fmt(v/1000)+' kWh').join('；')})),'尚无时段。')+'</section>'+
 (s.issues.length?'<section class="panel"><div class="section-title"><h2>待核对时段</h2><span>'+s.issues.length+' 项 · 未补齐不准结账</span></div>'+
   '<div class="issue-list">'+s.issues.map(i=>'<article><strong>'+esc(i.code)+'</strong><p>'+esc(i.message)+'</p><small>'+esc(localTime(i.start_at))+' — '+esc(localTime(i.end_at))+' · '+esc(JSON.stringify(i.detail))+'</small></article>').join('')+'</div></section>':'')+
 '<section class="panel"><div class="section-title"><h2>更正来由与差额</h2><span>不改原结账单</span></div>'+
   (s.corrections.length?table([{key:'reference',label:'更正单'},{key:'reason',label:'来由'},{key:'total_delta_kwh',label:'表差差额 kWh'},
      {key:'allocated_delta_kwh',label:'分摊差额 kWh'},{key:'idle_delta_kwh',label:'闲置差额 kWh'},{key:'created_at',label:'登记时间'}],
      s.corrections.map(c=>({...c,created_at:localTime(c.created_at)}))):'<p class="empty">尚无更正。已结账后若上游更新，这里只登记差额。</p>')+
   (s.corrections||[]).map(c=>'<div class="correction-rows"><strong>'+esc(c.reference)+'</strong>'+
     table([{key:'batch',label:'对象'},{key:'original_kwh',label:'原账 kWh'},{key:'current_kwh',label:'现算 kWh'},{key:'delta_kwh',label:'差额 kWh'}],
       c.rows.map(r=>({batch:r.batch||r.kind,original_kwh:r.original_kwh,current_kwh:r.current_kwh,delta_kwh:r.delta_kwh})))+'</div>').join('')+
   (s.status==='closed'&&admin?'<form id="correction-form"><label>更正来由（必填，至少 4 字）<input name="reason" required minlength="4" placeholder="例如：补录 16 点迟到读数修订"></label><button type="submit" class="primary">登记更正差额</button><p class="hint">仅记录新旧差额与来由；原结账单与已入账明细保持不变。</p></form>':'')+
 '</section>'+
 '<section class="panel"><div class="section-title"><h2>入账台账</h2><span>中断恢复依据 · 唯一键防重复</span></div>'+
   table([{key:'key',label:'入账键'},{key:'kind',label:'类型'},{key:'firing_id',label:'窑次 ID'},{key:'amount_kwh',label:'电量 kWh'},{key:'created_at',label:'入账时间'}],
     s.postings.map(p=>({...p,kind:KIND[p.kind]||p.kind,created_at:localTime(p.created_at)})),'尚未入账。')+'</section>'+
 ledgerSection(meters,firings,corrections);
}

function ledgerSection(meters,firings,corrections=[]){
 const writable=session.user.role!=='viewer';
 const groups=[...new Set(meters.map(m=>m.group_code))];
 return '<section class="panel"><div class="section-title"><h2>电表与读数</h2><span>带修订号的累计读数</span></div>'+
  table([{key:'code',label:'电表'},{key:'name',label:'名称'},{key:'group_code',label:'计量组'},{key:'interval',label:'期望间隔'},
         {key:'latest',label:'最新读数 kWh'},{key:'latest_at',label:'最新抄表'},{key:'revision',label:'修订号'}],
    meters.map(m=>{const last=(m.readings||[]).slice(-1)[0]||{};
      return {code:m.code,name:m.name,group_code:m.group_code,interval:(m.reading_interval_seconds/3600)+' 小时',
              latest:last.value_kwh??'—',latest_at:last.reading_at_text||'—',revision:last.revision??'—'};}),'尚未登记电表。')+
  (writable?'<div class="ledger-forms"><form id="reading-form"><h3>补录 / 更正读数</h3>'+
    '<label>电表<select name="meter_id">'+meters.map(m=>'<option value="'+m.id+'">'+esc(m.code)+' · '+esc(m.name)+'</option>').join('')+'</select></label>'+
    '<label>抄表时间<input type="datetime-local" name="reading_at" required value="'+iso(Date.now()/1000)+'"></label>'+
    '<div class="parameter-grid"><label>累计读数 kWh<input type="number" step="0.001" name="value_kwh" required></label>'+
    '<label>修订号（留空自动递增）<input type="number" min="1" name="revision"></label></div>'+
    '<button type="submit" class="primary">登记读数并重算受影响时段</button><p class="hint">时间按 UTC 录入与显示；累计读数只增不减，回退须先登记换表衔接。</p></form>'+
   '<form id="link-form"><h3>登记换表 / 清零衔接</h3>'+
    '<div class="parameter-grid"><label>旧表<select name="old_meter_id">'+meters.map(m=>'<option value="'+m.id+'">'+esc(m.code)+'</option>').join('')+'</select></label>'+
    '<label>新表（同表清零可重复选同一块）<select name="new_meter_id">'+meters.map(m=>'<option value="'+m.id+'">'+esc(m.code)+'</option>').join('')+'</select></label></div>'+
    '<label>换表时间<input type="datetime-local" name="at" required></label>'+
    '<div class="parameter-grid"><label>旧表止码 kWh<input type="number" step="0.001" name="old_final_kwh" required></label>'+
    '<label>新表起码 kWh<input type="number" step="0.001" name="new_initial_kwh" required></label></div>'+
    '<label>说明<input name="note" maxlength="300" placeholder="换表原因与表号"></label>'+
    '<button type="submit" class="button">登记衔接</button></form></div>':'')+
  '<h3 class="sub-head">窑次起止与装窑量</h3>'+
  table([{key:'batch',label:'窑次'},{key:'kiln_code',label:'窑线'},{key:'start_text',label:'开窑'},{key:'end_text',label:'停窑'},
         {key:'loaded',label:'装窑量'},{key:'passed',label:'合格件'},{key:'revision',label:'修订号'}],firings,'尚未登记窑次。')+
  (writable?'<form id="firing-form"><h3>登记 / 修订窑次</h3><div class="parameter-grid">'+
    '<label>窑次<input name="batch" required placeholder="QH-211"></label>'+
    '<label>计量组<select name="group_code">'+(groups.length?groups:['']).map(g=>'<option value="'+esc(g)+'">'+esc(g||'请先登记电表')+'</option>').join('')+'</select></label>'+
    '<label>窑线<input name="kiln_code" required placeholder="东窑"></label>'+
    '<label>装窑量<input type="number" min="1" name="loaded" required></label>'+
    '<label>合格件<input type="number" min="0" name="passed" required></label>'+
    '<label>开窑时间<input type="datetime-local" name="start_at" required></label>'+
    '<label>停窑时间<input type="datetime-local" name="end_at" required></label></div>'+
    '<button type="submit" class="button">保存窑次（上游修订会触发重算）</button></form>':'')+
  '<h3 class="sub-head">新建结账单</h3>'+
  (writable?'<form id="settlement-form"><div class="parameter-grid">'+
    '<label>结账单号<input name="reference" required placeholder="JD-202611-03"></label>'+
    '<label>名称<input name="name" required value="一号窑线 · 周分摊"></label>'+
    '<label>计量组<select name="group_code">'+(groups.length?groups:['']).map(g=>'<option value="'+esc(g)+'">'+esc(g||'请先登记电表')+'</option>').join('')+'</select></label>'+
    '<label>开始时间<input type="datetime-local" name="start_at" required></label>'+
    '<label>结束时间<input type="datetime-local" name="end_at" required></label></div>'+
    '<button type="submit" class="primary" '+(groups.length?'':'disabled')+'>创建并计算分摊</button></form>':'<p class="hint">观察员只读，不能登记数据或创建结账。</p>')+
 '</section>';
}

export function bindAccounting(refresh){
 const go=id=>{location.hash='#/accounting/'+id;};
 document.querySelector('#refresh-accounting')?.addEventListener('click',refresh);
 document.querySelector('#select-settlement')?.addEventListener('change',e=>go(e.target.value));

 const s=workspace.selectedSettlement;
 document.querySelector('#close-settlement')?.addEventListener('click',async()=>{
   const detail=await api('/accounting/settlements/'+s);
   if(!confirm('按当前版本结账？结账后不再改动旧账，后续变化只能登记更正差额。'))return;
   try{
     await api('/accounting/settlements/'+s+'/close',{method:'POST',body:JSON.stringify({
       expected_revision:detail.revision,expected_fingerprint:detail.fingerprint,
       request_id:crypto.randomUUID()})});
     notify('已结账，分摊与闲置之和等于表差');await refresh();
   }catch(e){notify(e.message,true);await refresh();}
 });
 document.querySelector('#resume-settlement')?.addEventListener('click',async()=>{
   try{await api('/accounting/settlements/'+s+'/resume',{method:'POST'});notify('已恢复并完成入账');await refresh();}
   catch(e){notify(e.message,true);}
 });
 document.querySelector('#recompute-settlement')?.addEventListener('click',async()=>{
   try{await api('/accounting/settlements/'+s+'/recompute',{method:'POST'});notify('已按受影响时段重算');await refresh();}
   catch(e){notify(e.message,true);}
 });
 const correctionForm=document.querySelector('#correction-form');
 if(correctionForm)submit(correctionForm,async f=>{
   await api('/accounting/settlements/'+s+'/corrections',{method:'POST',body:JSON.stringify({
     reason:f.get('reason'),request_id:crypto.randomUUID()})});
   notify('已登记更正差额，原结账单未改动');await refresh();
 });

 const readingForm=document.querySelector('#reading-form');
 if(readingForm)submit(readingForm,async f=>{
   const revision=f.get('revision');
   await api('/accounting/meters/'+f.get('meter_id')+'/readings',{method:'POST',body:JSON.stringify({
     reading_at:toEpoch(f.get('reading_at')),value_kwh:Number(f.get('value_kwh')),
     revision:revision?Number(revision):null})});
   notify('读数已登记，受影响时段的未结账分摊已重算');await refresh();
 });
 const linkForm=document.querySelector('#link-form');
 if(linkForm)submit(linkForm,async f=>{
   await api('/accounting/meters/links',{method:'POST',body:JSON.stringify({
     old_meter_id:Number(f.get('old_meter_id')),new_meter_id:Number(f.get('new_meter_id')),
     at:toEpoch(f.get('at')),old_final_kwh:Number(f.get('old_final_kwh')),
     new_initial_kwh:Number(f.get('new_initial_kwh')),note:f.get('note')||''})});
   notify('换表衔接已登记，回退时段已重新核对');await refresh();
 });
 const firingForm=document.querySelector('#firing-form');
 if(firingForm)submit(firingForm,async f=>{
   await api('/accounting/firings',{method:'POST',body:JSON.stringify({
     batch:f.get('batch'),group_code:f.get('group_code'),kiln_code:f.get('kiln_code'),
     start_at:toEpoch(f.get('start_at')),end_at:toEpoch(f.get('end_at')),
     loaded:Number(f.get('loaded')),passed:Number(f.get('passed'))})});
   notify('窑次已保存，受影响时段的未结账分摊已重算');await refresh();
 });
 const settlementForm=document.querySelector('#settlement-form');
 if(settlementForm)submit(settlementForm,async f=>{
   const created=await api('/accounting/settlements',{method:'POST',body:JSON.stringify({
     reference:f.get('reference'),name:f.get('name'),group_code:f.get('group_code'),
     start_at:toEpoch(f.get('start_at')),end_at:toEpoch(f.get('end_at'))})});
   notify(created.pending_count?'已创建；存在待核对时段，补齐后才能结账':'已创建并完成分摊');
   await refresh();go(created.id);
 });
}
