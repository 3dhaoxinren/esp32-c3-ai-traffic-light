'use strict';
const $=id=>document.getElementById(id);
const names={idle:'空闲 / 已完成',running:'执行中',waiting:'等待确认',error:'异常 / 报错',offline:'无有效 AI 状态'};
const colors={RED:'红灯',YELLOW:'黄灯',GREEN:'绿灯',OFF:'熄灯'};
const effects={SOLID:'常亮',BREATHE:'呼吸',SLOW:'慢闪',FAST:'快闪'};
const editable=['idle','running','waiting','error'];
let state=null,pending=false,closed=false,polling=false,startupDirty=false,patternsDirty=false,patternsLoaded=false;
async function api(action,data){
  const headers={'X-Session-Token':window.HLK_SESSION};
  const options={headers,cache:'no-store'};
  if(data!==undefined){headers['Content-Type']='application/json';options.method='POST';options.body=JSON.stringify(data);}
  const res=await fetch('/api/'+action,options),value=await res.json();
  if(!res.ok)throw new Error(value.error||'操作失败');return value;
}
function showError(message){$('ui-error').textContent=message;$('ui-error').hidden=!message;}
async function perform(action,data={}){
  if(pending||closed)return false;pending=true;showError('');if(state)render(state);
  try{await api(action,data);await poll();return true;}catch(e){showError(e.message);return false;}
  finally{pending=false;if(state)render(state);}
}
function errors(s){return (s.ai.sessions||[]).filter(r=>r.effective==='error'&&!r.stale).map(({source,session,event_id})=>({source,session,event_id}));}
function buildPatterns(){
  for(const key of editable){
    const row=document.createElement('div');row.className='ai-pattern-row';
    const title=document.createElement('strong');title.textContent=names[key];row.append(title);
    for(const [kind,options] of [['color',colors],['effect',effects]]){
      const label=document.createElement('label'),select=document.createElement('select');
      label.textContent=kind==='color'?'灯色':'灯效';select.id='pattern-'+key+'-'+kind;
      select.setAttribute('aria-label',names[key]+label.textContent);
      for(const [value,text] of Object.entries(options))select.add(new Option(text,value));
      select.addEventListener('change',()=>{patternsDirty=true;});label.append(select);row.append(label);
    }
    $('patterns').append(row);
  }
}
function render(s){
  state=s;const l=s.lights,a=s.ai,auto=s.startup,busy=pending||l.busy||closed,blocked=busy||s.demo;
  $('demo-banner').hidden=!s.demo;
  $('connect').disabled=blocked||l.connected||!$('port').value;
  $('disconnect').disabled=blocked||!l.connected;
  $('port').disabled=busy||l.connected;$('scan').disabled=busy||l.connected;
  $('connection').textContent=l.connected?'已连接 '+l.endpoint:(l.busy?'连接中…':l.message);
  $('reply').textContent=l.reply||'';$('reply').hidden=!l.reply;
  $('follow').disabled=blocked||!l.connected||a.enabled;$('pause').disabled=blocked||!l.connected||!a.enabled;
  $('follow-status').textContent=a.enabled?'自动跟随中':'跟随已暂停';
  $('ai-state').textContent=names[a.target]||a.label;
  const p=a.patterns[a.target]||a.patterns.offline;
  $('ai-pattern').textContent='目标灯效：'+colors[p.color]+(p.color==='OFF'?'':effects[p.effect]);
  $('beacon').className='ai-beacon color-'+p.color.toLowerCase()+' effect-'+p.effect.toLowerCase();
  const seen=errors(s);$('ack').disabled=pending||closed||!seen.length;
  $('ack-note').textContent=seen.length?seen.length+' 个未确认异常；托盘右键也可确认。':a.sessions.some(r=>r.acknowledged)?'已查看的异常已解除灯光报警，原始失败状态保留。':'当前没有需要确认的异常。';
  $('codex').checked=a.codex_enabled;$('codex').disabled=blocked;
  $('observer').textContent=a.observer;
  $('sessions').replaceChildren();
  for(const row of a.sessions){
    const line=document.createElement('div');line.className='ai-session';
    const source=document.createElement('strong'),id=document.createElement('small'),status=document.createElement('span');
    source.textContent=row.source;id.textContent=row.session;
    status.textContent=row.stale?'已失效':row.acknowledged?'异常已查看':names[row.effective];
    status.className='ai-session-state state-'+row.effective;line.append(source,id,status);$('sessions').append(line);
  }
  if(!startupDirty){$('logon').checked=auto.autostart.enabled;$('auto-connect').checked=auto.auto_connect;$('auto-follow').checked=auto.auto_follow;}
  for(const id of ['logon','auto-connect','auto-follow','save-startup'])$(id).disabled=blocked;
  $('logon').disabled=blocked||!auto.autostart.supported;
  $('resume').disabled=blocked||l.connected||!auto.target||!auto.auto_connect;
  const t=auto.target;
  $('saved-target').textContent=t?(t.transport==='serial'?'已记住 '+t.port+(t.location?' · USB位置 '+t.location:''):'原配置是网络设备，请先连接一次 Nano USB'):'请先成功连接一次 Nano，程序会记住设备。';
  $('startup-message').textContent=startupDirty?'有未保存修改':auto.warning||auto.autostart.warning||auto.message;
  if(!patternsLoaded||!patternsDirty){for(const key of editable){$('pattern-'+key+'-color').value=a.patterns[key].color;$('pattern-'+key+'-effect').value=a.patterns[key].effect;}patternsLoaded=true;}
  $('save-patterns').disabled=pending||closed;
  document.querySelectorAll('[data-command]').forEach(b=>b.disabled=blocked||!l.connected);
  $('log').textContent=(s.logs||[]).map(r=>r.text||'').slice(-60).join('\n');
}
async function poll(){if(closed||polling)return;polling=true;try{render(await api('state'));}catch(e){showError('后台未连接，请从托盘重新打开界面。');}finally{polling=false;}}
async function scan(){try{const ports=await api('ports'),prev=$('port').value;$('port').replaceChildren();for(const p of ports)$('port').add(new Option(p.device+' · '+p.description,p.device));if([...$('port').options].some(o=>o.value===prev))$('port').value=prev;if(!ports.length)$('port').add(new Option('没有发现 USB 串口',''));if(state)render(state);}catch(e){showError(e.message);}}
$('scan').addEventListener('click',scan);
$('connect').addEventListener('click',()=>perform('light_connect',{transport:'serial',port:$('port').value}));
$('disconnect').addEventListener('click',()=>perform('light_disconnect'));
$('follow').addEventListener('click',()=>perform('ai_control',{enabled:true}));
$('pause').addEventListener('click',()=>perform('ai_control',{enabled:false}));
$('ack').addEventListener('click',()=>perform('ai_ack_errors',{events:errors(state)}));
$('codex').addEventListener('change',()=>perform('ai_codex',{enabled:$('codex').checked}));
for(const id of ['logon','auto-connect','auto-follow'])$(id).addEventListener('change',()=>{startupDirty=true;if(state)render(state);});
$('save-startup').addEventListener('click',async()=>{if(await perform('startup_settings',{autostart:$('logon').checked,auto_connect:$('auto-connect').checked,auto_follow:$('auto-follow').checked})){startupDirty=false;if(state)render(state);}});
$('resume').addEventListener('click',()=>perform('startup_resume'));
$('save-patterns').addEventListener('click',async()=>{const patterns={offline:{color:'OFF',effect:'SOLID'}};for(const key of editable)patterns[key]={color:$('pattern-'+key+'-color').value,effect:$('pattern-'+key+'-effect').value};if(await perform('ai_settings',{patterns})){patternsDirty=false;if(state)render(state);}});
document.querySelectorAll('[data-command]').forEach(b=>b.addEventListener('click',()=>perform('light_send',{command:b.dataset.command})));
$('exit').addEventListener('click',async()=>{if(await perform('exit')){closed=true;clearInterval(timer);showError('程序已退出，可关闭此网页。');}});
buildPatterns();scan();poll();const timer=setInterval(poll,1000);
