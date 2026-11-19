'use strict';
(()=>{
const el=id=>document.getElementById(id);
const states={IDLE:'空闲',RUNNING:'运行',WAITING:'等待',ERROR:'报错',DONE:'完成',OFFLINE:'离线'};
const colors={IDLE:'#88e5ab',RUNNING:'#ffd181',WAITING:'#8ac6fa',ERROR:'#ffa19e',DONE:'#beabff',OFFLINE:'#b1c0c5'};
const canvas=el('aud-wave'),ctx=canvas.getContext('2d'), ruler=25;
const voicePlayer=el('aud-voice-player'), voiceIdle='电脑试听不需要连接 C3，也不替换剪辑素材。';
const recordingPlayer=el('aud-recording-player');
const headers={'X-Session-Token':window.HLK_SESSION,'Content-Type':'application/json'};
const signal=s=>{el('aud-result').textContent=s;};
const preview=s=>{el('aud-preview-status').textContent=s;};
const visible=()=>!el('tab-audio').hidden&&!document.hidden;
const num=id=>Number(el(id).value), fmt=n=>n.toFixed(3)+' 秒';
const buttons=[...document.querySelectorAll('#tab-audio [data-device]')];
const draftKey='ailamp-audio-timeline-v1';
const cards={};
let src=null, meta=null, gen=0, seq=-1, submitting=false, pending=null, scratch={},manual=[],anchor=null,keyCuts=[];
let saved={}, savedEndpoint='', manualBindingsReady=false, linkKey='', libraryKey='', triedLibrary='';
let buttonConfig=null, configuredGain=null, keyListSignature='';
let audioContext=null, waveLoad=null, player=null, cursor=0, playTicket=0, raf=0, loading=false;
let voiceUrl=null, voiceKey=null, voicePending=false, voiceRevision=0;
let recordingUrl=null, recordingPending=false, recordingRevision=0, recordingLabel='', recordingTransport='';

function action(text,fn,kind='secondary'){
  const b=document.createElement('button');b.type='button';b.className=kind;b.textContent=text;
  b.addEventListener('click',fn);return b;
}
for(const [key,title] of Object.entries(states)){
  const box=document.createElement('section');box.className='aud-card';box.dataset.state=key;
  const h=document.createElement('h4');h.textContent=title;
  const value=document.createElement('p');value.className='aud-card-value';
  const row=document.createElement('div');row.className='aud-card-actions';
  const bind=action('绑定选段',()=>bindClip(key));
  const play=action('试听',()=>{const r=scratch[key];if(r)startPreview(r,r.start);},'ghost');
  const remove=action('移除草稿',()=>{delete scratch[key];delete saved[key];saveDraft();refresh();draw();},'ghost');
  row.append(bind,play,remove);box.append(h,value,row);el('aud-state-cards').append(box);
  cards[key]={box,value,bind,play,remove};
  const label=document.createElement('label');label.textContent=title;
  const input=document.createElement('input');
  input.type='number';input.id='aud-bind-'+key;input.min='0';input.max='3000';input.value='0';
  label.append(input);el('aud-bindings').append(label);
}
function valid(r){
  return !!src&&Number.isInteger(r?.start)&&Number.isInteger(r?.end)&&r.start>=0&&r.end<=src.frames&&r.end>r.start&&r.end-r.start<=32*src.rate;
}
function selection(){
  if(!src||!el('aud-start').value||!el('aud-end').value)return null;
  const start=Math.round(num('aud-start')*src.rate);let end=Math.round(num('aud-end')*src.rate);
  if(Math.abs(end-src.frames)<=8)end=src.frames;
  return valid({start,end})?{start,end}:null;
}
function getCut(){
  const r=selection();
  if(!r)throw new Error('请先在波形上选一段不超过 32 秒的音频。');
  return r;
}
function recordSlot(){
  const n=num('aud-slot');if(!Number.isInteger(n)||n<1||n>3000)throw new Error('设备编号是 1–3000。');return n;
}
function showSelection(r){
  el('aud-start').value=(r.start/src.rate).toFixed(3);el('aud-end').value=(r.end/src.rate).toFixed(3);
}
function saveDraft(){
  if(!src)return;
  try{sessionStorage.setItem(draftKey,JSON.stringify({id:src.id,frames:src.frames,selection:selection(),cursor,scratch,manual,keyCuts}));}catch(_){}
}
function restoreDraft(){
  try{
    const st=JSON.parse(sessionStorage.getItem(draftKey)||'null');
    if(!st||!(st.id===src.id||(st.handoff&&st.frames===src.frames)))return;
    for(const k of Object.keys(states))if(valid(st.scratch?.[k]))scratch[k]=st.scratch[k];
    if(Array.isArray(st.manual))manual=st.manual.filter(x=>valid(x)&&Number.isInteger(x.slot)&&x.slot>=1&&x.slot<=3000).slice(0,32).map(x=>({start:x.start,end:x.end,slot:x.slot}));
    if(Array.isArray(st.keyCuts))keyCuts=st.keyCuts.filter(valid).slice(0,32).map(({start,end})=>({start,end}));
    if(valid(st.selection))showSelection(st.selection);
    if(Number.isFinite(st.cursor))cursor=Math.max(0,Math.min(src.frames,st.cursor));
    saveDraft();
  }catch(_){}
}
function setSource(next){
  halt(false);waveLoad?.abort.abort();waveLoad=null;src=next;cursor=0;scratch={};manual=[];saved={};keyCuts=[];
  el('aud-source').textContent=src?src.name+' · '+fmt(src.frames/src.rate):'准备本地语音或导入录音；每段最多 32 秒。';
  el('aud-start').value=0;el('aud-end').value=0;
  if(src){
    for(const k of ['aud-start','aud-end'])el(k).max=src.frames/src.rate;
    showSelection({start:0,end:Math.min(src.frames,32*src.rate)});restoreDraft();
  }
  preview('电脑试听不会写入设备。');renderManual();renderKeyCuts();draw();
}
function bindClip(key){
  try{
    const r=getCut(),existing=scratch[key];
    if(existing&&r.start===existing.start&&r.end===existing.end)return;
    scratch[key]=r;saveDraft();refresh();draw();
    signal('「'+states[key]+'」已绑定选段，尚未写入 C3。');
  }catch(e){signal(e.message);}
}
const formatRange=r=>fmt(r.start/src.rate)+' – '+fmt(r.end/src.rate);
const changed=()=>Object.keys(states).filter(k=>scratch[k]&&(
  savedEndpoint!==linkKey||scratch[k].start!==saved[k]?.start||scratch[k].end!==saved[k]?.end));
const has=name=>meta?.available&&meta.features?.includes(name);
const ready=()=>meta?.available&&!submitting&&!recordingPending&&!meta.busy&&!meta.linked_busy;
function refresh(){
  const selected=!!selection(), locked=submitting||!!meta?.busy;
  const left=changed(), available=!!meta?.available, can=ready(), live=has('LIVE');
  for(const b of buttons)b.disabled=!can;
  for(const k of ['aud-tts','aud-clear','aud-file'])el(k).disabled=locked;
  for(const k of ['aud-preview','aud-add'])el(k).disabled=!selected||(k==='aud-add'&&locked);
  el('aud-transport-play').disabled=!src;el('aud-transport-stop').disabled=!src;
  for(const {id} of [el('aud-read'),el('aud-wifi'),el('aud-net')])el(id).disabled=!can||!meta.usb;
  el('aud-bind').disabled=!can||!meta?.state_voice||!manualBindingsReady;
  el('aud-push').disabled=!can||!meta?.state_voice||!left.length;
  el('aud-manual-push').disabled=!can||!manual.length;
  el('aud-live').disabled=!(meta?.live||can&&live&&src);
  el('aud-live').textContent=meta?.live?'停止 C3 实时试听':'用 C3 实时试听整段';
  el('aud-live-cut').disabled=!can||!live||!selected;
  el('aud-live').title=available&&!live?'更新 C3 固件后可用：试听不占 Flash。':'只使用 C3 的 RAM，不写 Flash';
  el('aud-live-cut').title=el('aud-live').title;
  el('aud-live-note').textContent=available&&!live?'当前 C3 固件不支持实时试听；更新固件后可用。电脑试听不受影响。':
    '实时试听使用 C3 的 RAM，不写 Flash，也不会加入设备录音列表。';
  el('aud-speak').disabled=!(meta?.live||can&&live&&el('aud-text').value.trim());
  el('aud-speak').textContent=meta?.live?'停止 C3 播报':'直接播报到 C3';
  el('aud-speak-note').textContent=available&&live?
    (meta.usb?'当前用 USB；可在灯控连接里切换到局域网。':'当前通过局域网连接，不需要插 USB。')+' 不保存录音，不更改灯或状态绑定。':
    '设备通过局域网连接即可直接播报。不存录音、不绑定状态、不替换剪辑素材。';
  refreshVoiceButton();
  el('aud-stop').disabled=!(meta?.live||can);
  el('aud-library').disabled=!can||!has('LIST');
  el('aud-lan-read-pair').hidden=!has('LAN_READ');
  el('aud-lan-read-pair').disabled=!can||!has('LAN_READ');
  el('aud-gain-apply').disabled=!can||!has('GAIN_SAVE');
  el('aud-gain-note').textContent=available&&!has('GAIN_SAVE')?'当前 C3 固件不能保存默认音量，请更新后使用。':
    configuredGain===null?'保存后重启保持该档位。未保存的数值可用于试听。':
    '设备已保存 '+configuredGain+' 档；重启、状态播报和实体按键使用这个档位。';
  const keyReady=can&&has('BUTTONS')&&!!buttonConfig;
  el('aud-button-mode').disabled=!keyReady;
  el('aud-button-mode-save').disabled=!keyReady||el('aud-button-mode').value===buttonConfig.mode;
  el('aud-button-add').disabled=!selected||locked||keyCuts.length>=32;
  el('aud-button-push').disabled=!keyReady||!keyCuts.length||keyCuts.length+buttonConfig.slots.length>32;
  const availableSlots=new Set(meta?.library?.tracks?.map(r=>r.slot)||[]);
  const count=buttonConfig?.mode==='ALL'?availableSlots.size:buttonConfig?.slots.filter(s=>availableSlots.has(s)).length;
  el('aud-button-note').textContent=buttonConfig?
    '设备当前：'+(buttonConfig.mode==='ALL'?'随机播全部已存录音':'随机播自选歌单')+' · '+count+' 段可播。'+(buttonConfig.mode==='ALL'?' 保存自选片段不会切换当前模式。':'')+
      (keyCuts.length+buttonConfig.slots.length>32?' 自选歌单最多 32 段，请先移除不用的项目。':''):
    available&&!has('BUTTONS')?'当前 C3 固件不支持自选歌单和全部随机，请更新后使用。':'连接 C3 后读取实际按键歌单。';
  for(const b of el('aud-button-playlist').querySelectorAll('button'))b.disabled=!keyReady;
  el('aud-bind-hint').textContent=!available?'可以先剪辑，连接支持状态播报的 C3 后写入。':
    !meta.state_voice?'当前固件不支持分状态播报。':
    !left.length?'选段并绑定一个状态后，可写入 C3。':'待写入 '+left.length+' 个状态；没有选中的状态及顶部按键录音不会更改。';
  for(const [k,c] of Object.entries(cards)){
    const r=scratch[k], savedHere=r&&!left.includes(k), deviceSlot=meta?.library?.bindings?.[k.toLowerCase()];
    c.bind.disabled=!selected||locked;
    c.bind.textContent=r?'换成选段':'绑定选段';
    c.play.disabled=!r;c.remove.disabled=!r||locked;
    c.box.classList.toggle('is-bound',!!r);
    c.value.textContent=r?(formatRange(r)+' · '+(savedHere?'已写入':'待写入')):
      (deviceSlot?'设备已绑定 #'+deviceSlot:'未绑定选段');
  }
  for(const b of el('aud-library-list').querySelectorAll('button')){
    b.disabled=!can||(b.dataset.delete==='1'&&(!meta.usb||!has('USB_DELETE')))||
      (b.dataset.addKey==='1'&&(!keyReady||buttonConfig.slots.length>=32));
  }
}
function renderManual(){
  const ol=el('aud-cuts');ol.replaceChildren();
  manual.forEach((c,i)=>{
    const li=document.createElement('li');li.textContent=formatRange(c)+' → #'+c.slot;
    li.append(action('试听',()=>startPreview(c,c.start),'ghost'));
    li.append(action('移除',()=>{if(submitting||meta?.busy)return;manual.splice(i,1);saveDraft();renderManual();refresh();},'ghost'));
    ol.append(li);
  });
}
function renderKeyCuts(){
  const ol=el('aud-button-cuts');ol.replaceChildren();
  keyCuts.forEach((c,i)=>{
    const li=document.createElement('li');li.textContent='待写入 · '+formatRange(c);
    li.append(action('试听',()=>startPreview(c,c.start),'ghost'));
    li.append(action('从草稿移除',()=>{if(submitting||meta?.busy)return;keyCuts.splice(i,1);saveDraft();renderKeyCuts();refresh();},'ghost'));
    ol.append(li);
  });
}
function setButtonConfig(next){
  if(!next)return;
  if(JSON.stringify(next)!==JSON.stringify(buttonConfig))el('aud-button-mode').value=next.mode;
  buttonConfig=next;
}
function saveButtonConfig(mode,slots){
  if(!buttonConfig||!ready())return;
  begin('buttons',{mode,slots,expected:buttonConfig});
}
function renderButtonList(x){
  const data=x.library, signature=JSON.stringify([buttonConfig,data?.tracks,data?.bindings]);
  if(signature===keyListSignature)return;
  keyListSignature=signature;
  const list=el('aud-button-playlist');list.replaceChildren();
  if(!buttonConfig)return;
  const slots=new Map((data?.tracks||[]).map(r=>[r.slot,r.bytes]));
  for(const id of buttonConfig.slots){
    const row=document.createElement('div');row.className='aud-library-row';
    const title=document.createElement('div');title.className='aud-library-track';
    const tags=Object.keys(states).filter(k=>data?.bindings[k.toLowerCase()]===id).map(k=>states[k]);
    title.textContent='录音 #'+id+(tags.length?' · '+tags.join(' / '):'')+(slots.has(id)?' · '+fmt(slots.get(id)/32000):' · 设备上没有对应录音');
    if(slots.has(id))row.append(title,action('用 C3 试听',()=>begin('play',{slot:id,gain:num('aud-gain')})));
    else row.append(title);
    row.append(action('移出自选',()=>saveButtonConfig(buttonConfig.mode,buttonConfig.slots.filter(n=>n!==id)),'ghost'));
    list.append(row);
  }
}
function renderLibrary(x){
  const l=x.library, key=JSON.stringify([l,x.available,x.usb,x.features]);
  if(key===libraryKey)return;libraryKey=key;
  const list=el('aud-library-list');list.replaceChildren();
  const note=el('aud-library-hint');
  if(!x.available){note.textContent='连接 C3 后可读取设备上已有的录音。';return;}
  if(has('LIST'))note.textContent=l?.complete?(l.tracks.length?'设备上有 '+l.tracks.length+' 段录音。':'设备上还没有已存录音。'):'等待设备列表，可手动刷新。';
  else note.textContent='当前 C3 固件不支持读取完整列表或删除。这里只显示本次已确认写入的录音。';
  if(!l?.tracks?.length)return;
  for(const r of l.tracks){
    const row=document.createElement('div');row.className='aud-library-row';
    const label=document.createElement('div');label.className='aud-library-track';
    const related=Object.keys(states).filter(k=>l.bindings[k.toLowerCase()]===r.slot);
    const title=document.createElement('strong');title.textContent=related.length?related.map(k=>states[k]).join(' / ')+'播报':'录音 #'+r.slot;
    const detail=document.createElement('span');detail.textContent=(related.length?'#'+r.slot+' · ':'')+fmt(r.bytes/32000);
    label.append(title,detail);
    const play=action('用 C3 试听',()=>begin('play',{slot:r.slot,gain:num('aud-gain')}));
    const listen=action('电脑试听',()=>listenRecording(r,l.endpoint,title.textContent));
    listen.setAttribute('aria-label','电脑试听录音 #'+r.slot);
    listen.title=has('LAN_READ')?'已配对时优先通过局域网回读，不让 C3 出声。':'通过 C3 原生 USB 回读，不让设备出声；灯控可保持 Wi‑Fi';
    const onKey=buttonConfig?.slots.includes(r.slot);
    const addKey=action('加入按键歌单',()=>saveButtonConfig(buttonConfig.mode,[...buttonConfig.slots,r.slot]),'ghost');
    addKey.dataset.addKey='1';addKey.hidden=!has('BUTTONS')||!buttonConfig||onKey;
    const del=action('删除',()=>{
      const stateNote=related.length?'，并解除「'+related.map(k=>states[k]).join(' / ')+'」的绑定':'';
      const buttonNote=r.slot<=33?' 低编号录音可能用于实体按键。':'';
      if(confirm('从 C3 删除 #'+r.slot+stateNote+'？此操作不能撤销。'+buttonNote))
        begin('delete',{slot:r.slot,bytes:r.bytes,states:related});
    },'ghost aud-delete');
    del.dataset.delete='1';del.hidden=!has('USB_DELETE');
    del.title=x.usb?'从设备 Flash 删除此段':'请通过 C3 原生 USB 连接后删除';
    const controls=document.createElement('div');controls.className='aud-library-actions';
    controls.append(listen,play,addKey,del);
    row.append(label,controls);list.append(row);
  }
  note.textContent+=has('LAN_READ')?' 电脑试听优先走局域网；首次配对时才需接原生 USB。':
    ' 当前固件的电脑试听需接同一块 C3 的原生 USB，灯控仍可用 Wi-Fi。';
  if(has('USB_DELETE')&&!x.usb)note.textContent+=' 删除需在灯控页选 USB。';
}
function recordingResult(text){
  const status=el('aud-recording-result');status.textContent=text;status.hidden=!text;
}
function closeRecording(){
  ++recordingRevision;
  recordingPlayer.pause();
  if(recordingUrl){
    recordingPlayer.removeAttribute('src');recordingPlayer.load();
    URL.revokeObjectURL(recordingUrl);
  }
  recordingUrl=null;recordingLabel=recordingTransport='';
  el('aud-recording-preview').hidden=true;
  recordingResult('');
}
async function listenRecording(row,endpoint,label){
  if(!ready())return;
  closeRecording();const revision=recordingRevision;
  recordingPending=true;refresh();
  recordingLabel=label;
  recordingResult('正在回读 '+label+'，将只在电脑上播放…');
  try{
    const res=await fetch('/api/audio_recording_preview',{method:'POST',headers,
      body:JSON.stringify({slot:row.slot,bytes:row.bytes,endpoint})});
    if(!res.ok)throw new Error((await res.json()).error||'回读失败，请重试。');
    const wav=await res.blob();
    if(revision!==recordingRevision)return;
    recordingTransport=res.headers.get('X-C3-Readback')==='lan'?'局域网':'USB';
    recordingUrl=URL.createObjectURL(wav);recordingPlayer.src=recordingUrl;
    el('aud-recording-preview').hidden=false;
    if(!visible()){recordingResult(label+' 已读回，可点播放试听。');return;}
    try{await recordingPlayer.play();}
    catch(e){if(revision===recordingRevision)recordingResult(e.name==='NotAllowedError'?
      label+' 已读回，点播放器的播放键试听。':'无法播放读回的录音，请重试。');}
  }catch(e){if(revision===recordingRevision)recordingResult(e.message);}
  finally{recordingPending=false;refresh();}
}
recordingPlayer.addEventListener('play',()=>{
  if(recordingUrl){voicePlayer.pause();halt();recordingResult('电脑正在播放 '+recordingLabel+'（'+recordingTransport+' 回读）；C3 不出声。');}
});
recordingPlayer.addEventListener('ended',()=>{
  if(recordingUrl)recordingResult(recordingLabel+'（'+recordingTransport+' 回读）试听结束，可用播放器重播。');
});
function render(x){
  if(meta&&x.seq<meta.seq)return;
  const previousKey=linkKey;
  linkKey=x.available?((x.usb?'USB:':'Wi-Fi:')+x.endpoint):'';
  if(linkKey!==previousKey){saved={};manualBindingsReady=false;triedLibrary='';buttonConfig=null;configuredGain=null;closeRecording();}
  meta=x;
  if(x.library?.buttons)setButtonConfig(x.library.buttons);
  el('aud-device').textContent=x.available?'C3 已连接 · '+x.endpoint:'未连接 C3';
  el('aud-device').classList.toggle('ready',x.available);
  el('aud-progress').value=x.progress;
  if(x.source?.id!==src?.id)setSource(x.source);
  if(x.seq!==seq&&!x.busy){
    seq=x.seq;const op=pending;pending=null;
    if(x.error){
      signal(x.error);
      if(op?.type==='button_push'||op?.type==='buttons')el('aud-button-result').textContent=x.error;
      if(op?.type==='lan_read_pair')recordingResult(x.error);
    }
    else if(typeof x.result==='string')signal(x.result);
    else if(x.result){
      const r=x.result;
      if(r.message)signal(r.message);
      if(r.lan_read_paired)recordingResult(r.message);
      if(r.buttons){
        setButtonConfig(r.buttons);
        if(op?.type==='button_push'||op?.type==='buttons')el('aud-button-result').textContent=r.message||'已保存。';
      }
      if(op?.type==='button_push'&&r.saved_tracks?.length){
        keyCuts=[];saveDraft();renderKeyCuts();
      }
      if(Number.isInteger(r.default_gain)&&(configuredGain===null||op?.type==='gain')){
        configuredGain=r.default_gain;el('aud-gain').value=configuredGain;
      }
      if(r.bindings){
        manualBindingsReady=true;
        for(const k of Object.keys(states))el('aud-bind-'+k).value=r.bindings[k.toLowerCase()]||0;
      }
      if(r.deleted_slot){saved={};}
      if(op?.rows&&op.endpoint===linkKey&&op.source===src?.id&&r.saved_states){
        savedEndpoint=linkKey;
        for(const v of op.rows)if(r.saved_states.includes(v.state))saved[v.state]=v;
      }
      if(r.amp){
        if(r.amp.default_gain!==undefined)configuredGain=Number(r.amp.default_gain);
        if(r.amp.gain!==undefined)el('aud-gain').value=r.amp.default_gain??r.amp.gain;
        signal('C3 已确认 · 功放 '+(r.amp.i2s==='1'?'就绪':'未就绪')+' · 语音分区 '+(r.amp.fs==='1'?'就绪':'未初始化'));
      }
      if(typeof r.state==='string')signal(r.state==='ONLINE'?'C3 已联网：'+r.ip+':8080':'设备尚未取得 IP：'+r.state);
    }
  }
  renderButtonList(x);renderLibrary(x);refresh();
  if(has('LIST')&&!x.library&&ready()&&triedLibrary!==linkKey){
    triedLibrary=linkKey;setTimeout(()=>{if(visible()&&ready()&&!meta.library)begin('library');},0);
  }
}
async function update(){
  if(!visible())return;
  try{render(await api('audio'));}catch(e){signal(e.message);}
}
async function begin(op,args={},rows=null){
  if(submitting||meta?.busy)return;
  submitting=true;refresh();signal(op==='live'?'正在向 C3 发送实时试听，不写 Flash…':op==='speak'?'正在生成并直接播报，不写 Flash…':'处理中…');
  pending={type:op,rows,endpoint:linkKey,source:src?.id};
  try{await api('audio_job',{op,...args});}
  catch(e){pending=null;signal(e.message);}
  finally{submitting=false;await update();}
}
function draw(){if(!raf)raf=requestAnimationFrame(paint);}
function position(){
  if(!player)return cursor;
  const r=player, now=r.offset+Math.max(0,audioContext.currentTime-r.started)*src.rate;
  return r.loop&&now>=r.end?r.start+(now-r.start)%(r.end-r.start):Math.min(r.end,now);
}
function paint(){
  raf=0;const w=canvas.width,h=canvas.height;
  ctx.fillStyle='#101b1d';ctx.fillRect(0,0,w,h);
  ctx.fillStyle='#203438';ctx.fillRect(0,0,w,ruler);
  const p=el('aud-time');
  if(!src){p.textContent='0.000 秒 / 0.000 秒';return;}
  const x=v=>Math.max(0,Math.min(w,v/src.frames*w)), sel=selection(), head=position();
  const duration=src.frames/src.rate, rough=duration/(w/105), exp=10**Math.floor(Math.log10(rough));
  const step=([1,2,5,10].find(n=>n*exp>=rough)||10)*exp;
  ctx.font='13px sans-serif';ctx.fillStyle='#b6c8c3';
  for(let t=0;t<duration;t+=step){
    const xx=x(t*src.rate);ctx.fillRect(xx,ruler-6,1,6);
    ctx.fillText((+t.toFixed(3))+'s',xx+4,15);
  }
  if(sel){ctx.fillStyle='#2e5846';ctx.fillRect(x(sel.start),ruler,x(sel.end)-x(sel.start),h-ruler);}
  let row=0;
  for(const c of Object.keys(states)){
    if(!scratch[c])continue;
    const r=scratch[c];ctx.fillStyle=colors[c];ctx.fillRect(x(r.start),h-3-row,Math.max(1,x(r.end)-x(r.start)),2);row+=3;
  }
  const mid=(h+ruler)/2,amp=(h-ruler)/73000;
  ctx.fillStyle='#6d8a82';ctx.fillRect(0,mid,w,1);
  ctx.strokeStyle='#9cebbd';ctx.beginPath();
  src.peaks.forEach(([lo,hi],i)=>{
    const xx=(i+.5)*w/src.peaks.length;
    ctx.moveTo(xx,mid-hi*amp);ctx.lineTo(xx,mid-lo*amp);
  });
  ctx.stroke();
  const headX=Math.max(0,Math.min(w-2,x(head)));
  ctx.fillStyle='#ffd181';ctx.fillRect(headX,0,2,h);ctx.fillRect(headX-3,0,8,6);
  p.textContent=fmt(head/src.rate)+' / '+fmt(duration);
  if(player&&!document.hidden)draw();
}
function bounds(e){
  const t=canvas.getBoundingClientRect();
  return {f:Math.round(src.frames*Math.max(0,Math.min(1,(e.clientX-t.left)/t.width))),ruler:e.clientY-t.top<t.height*ruler/canvas.height};
}
function seek(n,restart=true){
  const wasPlaying=!!player;
  halt(false);cursor=n;saveDraft();draw();
  if(wasPlaying&&restart){
    let r=range();
    if(r&&n>=r.start&&n<r.end)startPreview(r,n);
  }
}
canvas.addEventListener('pointerdown',e=>{
  if(!src||e.button!==0)return;
  canvas.focus();canvas.setPointerCapture(e.pointerId);
  anchor={...bounds(e),x:e.clientX,drag:false};
});
canvas.addEventListener('pointermove',e=>{
  if(!src||!anchor||anchor.ruler)return;
  if(!anchor.drag&&Math.abs(anchor.x-e.clientX)<5)return;
  anchor.drag=true;const {f}=bounds(e);
  halt(false);cursor=Math.min(f,anchor.f);showSelection({start:cursor,end:Math.max(f,anchor.f)});refresh();draw();
});
canvas.addEventListener('pointerup',e=>{
  if(!anchor||!src)return;
  if(!anchor.drag)seek(bounds(e).f);
  anchor=null;saveDraft();refresh();draw();
});
canvas.addEventListener('pointercancel',()=>{anchor=null;saveDraft();});
for(const k of ['aud-start','aud-end'])el(k).addEventListener('input',()=>{halt();saveDraft();refresh();draw();});

function range(){
  if(!src)return null;
  return el('aud-mode').value==='full'?{start:0,end:src.frames}:selection();
}
function playerLabel(){el('aud-transport-play').textContent=loading?'取消加载':player?'暂停':'播放';}
function halt(now=true){
  if(now)cursor=position();
  loading=false;++playTicket;
  if(player){const {node}=player;player=null;node.onended=null;try{node.stop();}catch(_){}}
  playerLabel();draw();
}
function sourceBuffer(ac,s){
  if(waveLoad?.id===s.id)return waveLoad.promise;
  const abort=new AbortController();
  const promise=(async()=>{
    const rsp=await fetch('/api/audio_preview',{method:'POST',headers,signal:abort.signal,body:JSON.stringify({source:s.id,whole:true})});
    if(!rsp.ok)throw new Error((await rsp.json()).error||'无法读取试听音频。');
    const bytes=await rsp.arrayBuffer();
    if(bytes.byteLength!==44+s.frames*2)throw new Error('试听数据长度不正确。');
    const view=new DataView(bytes,44);
    const buffer=ac.createBuffer(1,s.frames,s.rate), pcm=buffer.getChannelData(0);
    for(let i=0;i<s.frames;i++)pcm[i]=view.getInt16(i*2,true)/32768;
    return buffer;
  })();
  waveLoad={id:s.id,abort,promise};
  promise.catch(()=>{if(waveLoad?.promise===promise)waveLoad=null;});
  return promise;
}
async function startPreview(r, at){
  if(!r||!src){preview('先选一段音频，或切到「整段录音」。');return;}
  voicePlayer.pause();recordingPlayer.pause();
  halt(false);const ticket=playTicket, s=src;
  cursor=at;loading=true;playerLabel();draw();
  preview('正在准备电脑试听…');
  try{
    const Audio=window.AudioContext||window.webkitAudioContext;
    if(!Audio)throw new Error('当前浏览器不支持 Web Audio。');
    const ac=audioContext||(audioContext=new Audio());
    const [,buffer]=await Promise.all([ac.resume(),sourceBuffer(ac,s)]);
    if(ticket!==playTicket||s!==src)return;
    const node=ac.createBufferSource(), loop=el('aud-loop').checked;
    node.buffer=buffer;node.loop=loop;node.loopStart=r.start/s.rate;node.loopEnd=r.end/s.rate;
    node.connect(ac.destination);
    const started=ac.currentTime+.015;
    player={...r,node,loop,started,offset:at};
    node.onended=()=>{
      if(player?.node!==node)return;
      player=null;cursor=r.end;playerLabel();draw();saveDraft();preview('电脑试听结束。');
    };
    if(loop)node.start(started,at/s.rate);
    else node.start(started,at/s.rate,(r.end-at)/s.rate);
    loading=false;playerLabel();draw();preview(loop?'电脑循环试听中…':'电脑试听中…');
  }catch(e){if(ticket===playTicket){halt(false);preview(e.message||'电脑试听未开始。');}}
}
function togglePlayback(){
  if(player||loading){halt();saveDraft();preview('已暂停。');return;}
  const r=range();if(r)startPreview(r,cursor>=r.start&&cursor<r.end?cursor:r.start);
}
el('aud-transport-play').onclick=togglePlayback;
el('aud-transport-stop').onclick=()=>{halt(false);cursor=range()?.start||0;saveDraft();draw();preview('播放头已回到起点。');};
el('aud-preview').onclick=()=>{const r=selection();if(r){el('aud-mode').value='cut';startPreview(r,r.start);}};
el('aud-mode').onchange=()=>{halt();const r=range();if(r&&(cursor<r.start||cursor>=r.end))cursor=r.start;saveDraft();draw();};
el('aud-loop').onchange=()=>{
  const r=player;if(!r)return;
  const p=position();startPreview(r,p<r.end?p:r.start);
};
document.addEventListener('keydown',e=>{
  if(!visible()||!src||e.code!=='Space'||e.altKey||e.ctrlKey||e.metaKey||e.target?.closest?.('button,input,select,textarea,summary,audio,[contenteditable],a'))return;
  e.preventDefault();if(!e.repeat)togglePlayback();
});
async function portableAudio(file){
  if(file.size>32*1024*1024)throw new Error('导入文件不得超过 32 MB。');
  const v=new DataView(await file.slice(0,44).arrayBuffer());
  if(v.byteLength>=44&&v.getUint32(0)===0x52494646&&v.getUint32(12)===0x666d7420&&
      v.getUint16(20,true)===1&&v.getUint16(22,true)===1&&v.getUint32(24,true)===16000&&v.getUint16(34,true)===16)return file;
  const Decoder=window.OfflineAudioContext||window.webkitOfflineAudioContext;
  if(!Decoder)return file;
  let a;
  try{a=await new Decoder(1,1,16000).decodeAudioData(await file.arrayBuffer());}
  catch(_){return file;} // fallback to optional FFmpeg
  if(a.length>16000*300)throw new Error('音频源不能超过 5 分钟。');
  if(a.sampleRate!==16000||!a.length||a.numberOfChannels>8)return file;
  const output=new ArrayBuffer(44+a.length*2), w=new DataView(output);
  w.setUint32(0,0x52494646);w.setUint32(4,36+a.length*2,true);w.setUint32(8,0x57415645);w.setUint32(12,0x666d7420);
  w.setUint32(16,16,true);w.setUint16(20,1,true);w.setUint16(22,1,true);w.setUint32(24,16000,true);w.setUint32(28,32000,true);
  w.setUint16(32,2,true);w.setUint16(34,16,true);w.setUint32(36,0x64617461);w.setUint32(40,a.length*2,true);
  const channels=Array.from({length:a.numberOfChannels},(_,i)=>a.getChannelData(i));
  for(let j=0;j<a.length;j++){
    let x=0;for(const c of channels)x+=c[j];
    x=Math.max(-1,Math.min(1,x/channels.length));
    w.setInt16(44+j*2,Math.round(x*(x<0?32768:32767)),true);
  }
  return new Blob([output],{type:'application/octet-stream'});
}

el('aud-file').onchange=async e=>{
  const file=e.target.files[0];if(!file)return;e.target.value='';
  if(submitting||meta?.busy)return;
  try{
    submitting=true;refresh();signal('正在导入…');
    const rsp=await fetch('/api/audio_import',{method:'POST',headers:{...headers,'Content-Type':'application/octet-stream','X-Audio-Name':encodeURIComponent(file.name)},body:await portableAudio(file)});
    if(!rsp.ok)throw new Error((await rsp.json()).error);
  }catch(err){signal(err.message);}
  finally{submitting=false;await update();}
};
el('aud-push').onclick=()=>{
  const rows=changed().map(state=>({...scratch[state],state}));
  if(!rows.length||!src)return;
  if(confirm('写入「'+rows.map(v=>states[v.state]).join(' / ')+'」的播报？这些状态的旧绑定将被替换。'))
    begin('push',{source:src.id,cuts:rows,play:false,gain:num('aud-gain')},rows);
};
el('aud-add').onclick=()=>{
  try{
    const slot=recordSlot(), r=getCut();
    if(manual.length>=32||manual.some(c=>c.slot===slot))throw new Error('同批编号不能重复，最多 32 段。');
    manual.push({...r,slot});el('aud-slot').value=Math.min(3000,slot+1);saveDraft();renderManual();refresh();
  }catch(e){signal(e.message);}
};
el('aud-manual-push').onclick=()=>{
  if(manual.length&&src&&confirm('推送会替换设备上相同编号的语音。继续？'))
    begin('push',{source:src.id,cuts:manual,play:el('aud-auto-play').checked,gain:num('aud-gain')});
};
const slotJob=(op)=>{try{begin(op,{slot:recordSlot(),gain:num('aud-gain')});}catch(e){signal(e.message);}};
function live(cut){
  if(!src)return;
  try{
    const r=cut?getCut():{whole:true};
    halt();voicePlayer.pause();begin('live',{source:src.id,gain:num('aud-gain'),...r});
  }catch(e){signal(e.message);}
}
el('aud-live').onclick=()=>meta?.live?stopC3():live(false);
el('aud-live-cut').onclick=()=>live(true);
el('aud-voice').onchange=()=>{
  el('aud-voice-note').textContent=el('aud-voice').value==='local'?'离线生成，文本不会发出本机。':
    '在线神经语音：主动试听、生成或播报才会发送输入文字，不会发送你的录音。';
  clearVoiceSample();
};
const speechRequest=()=>({text:el('aud-text').value,profile:el('aud-voice').value,rate:num('aud-voice-rate')});
function refreshVoiceButton(){
  const b=el('aud-voice-listen');
  b.disabled=voicePending||!el('aud-text').value.trim();
  b.textContent=voicePending?'正在合成…':(!voicePlayer.paused&&!voicePlayer.ended?'暂停电脑试听':'电脑试听文字');
}
function voiceResult(s){el('aud-voice-result').textContent=s;}
function clearVoiceSample(){
  ++voiceRevision;
  voicePlayer.pause();
  if(voiceUrl){
    voicePlayer.removeAttribute('src');voicePlayer.load();
    URL.revokeObjectURL(voiceUrl);
  }
  voiceUrl=voiceKey=null;
  el('aud-voice-preview').hidden=true;
  voiceResult(voiceIdle);refreshVoiceButton();
}
async function listenVoice(){
  if(voicePending)return;
  if(!voicePlayer.paused&&!voicePlayer.ended){voicePlayer.pause();return;}
  const args=speechRequest();if(!args.text.trim())return;
  const revision=voiceRevision, key=JSON.stringify(args);
  try{
    if(!voiceUrl||voiceKey!==key){
      voicePending=true;refreshVoiceButton();voiceResult('正在合成当前文字，仅在电脑播放…');
      const res=await fetch('/api/audio_voice_preview',{method:'POST',headers,body:key});
      if(!res.ok)throw new Error((await res.json()).error||'电脑试听生成失败。');
      const wav=await res.blob();
      if(revision!==voiceRevision||!visible())return;
      if(voiceUrl)URL.revokeObjectURL(voiceUrl);
      voiceUrl=URL.createObjectURL(wav);voiceKey=key;voicePlayer.src=voiceUrl;
      el('aud-voice-preview').hidden=false;
    }
    if(voicePlayer.ended)voicePlayer.currentTime=0;
    try{await voicePlayer.play();}
    catch(e){
      if(revision===voiceRevision)voiceResult(e.name==='NotAllowedError'?
        '试听已生成，点播放器上的播放按钮即可。':'无法播放这段试听音频，请重试。');
    }
  }catch(e){if(revision===voiceRevision)voiceResult(e.message);}
  finally{voicePending=false;refreshVoiceButton();}
}
voicePlayer.addEventListener('play',()=>{if(voiceUrl){halt();recordingPlayer.pause();voiceResult('正在电脑试听；未发送给 C3。');}refreshVoiceButton();});
voicePlayer.addEventListener('pause',()=>{if(voiceUrl&&!voicePlayer.ended&&voicePlayer.paused)voiceResult('电脑试听已暂停。');refreshVoiceButton();});
voicePlayer.addEventListener('ended',()=>{if(voiceUrl)voiceResult('电脑试听结束；可再次播放。');refreshVoiceButton();});
el('aud-text').oninput=()=>{clearVoiceSample();refresh();};
el('aud-voice-rate').onchange=clearVoiceSample;
el('aud-voice-listen').onclick=listenVoice;
el('aud-tts').onclick=()=>begin('tts',speechRequest());
el('aud-speak').onclick=()=>{if(meta?.live)stopC3();else{voicePlayer.pause();begin('speak',speechRequest());}};
el('aud-read').onclick=()=>slotJob('read');
el('aud-play').onclick=()=>slotJob('play');
async function stopC3(){
  try{
    if(meta?.live){await api('audio_cancel',{});signal('正在停止 C3 实时试听…');}
    else begin('stop');
  }catch(e){signal(e.message);}
}
el('aud-stop').onclick=stopC3;
el('aud-clear').onclick=()=>{if(!Object.keys(scratch).length||confirm('清除录音和这次未写入的选段？设备已有录音不受影响。'))begin('clear');};
el('aud-gain-apply').onclick=()=>begin('gain',{gain:num('aud-gain')});
el('aud-button-mode').onchange=refresh;
el('aud-button-mode-save').onclick=()=>{if(buttonConfig)saveButtonConfig(el('aud-button-mode').value,buttonConfig.slots);};
el('aud-button-add').onclick=()=>{
  try{
    const cut=getCut();
    if(keyCuts.some(r=>r.start===cut.start&&r.end===cut.end))throw new Error('这个选段已在按键草稿中。');
    keyCuts.push(cut);saveDraft();renderKeyCuts();refresh();
    el('aud-button-result').textContent='片段已加入草稿，尚未写入 C3。';
  }catch(e){el('aud-button-result').textContent=e.message;}
};
el('aud-button-push').onclick=()=>{
  if(keyCuts.length&&src&&confirm('将 '+keyCuts.length+' 段录音写入 C3 并加入自选歌单？'))
    begin('button_push',{source:src.id,cuts:keyCuts});
};
el('aud-fetch').onclick=()=>begin('status');
el('aud-library').onclick=()=>begin('library');
el('aud-lan-read-pair').onclick=()=>{
  if(ready()){recordingResult('正在通过同一块 C3 的原生 USB 配对…');begin('lan_read_pair');}
};
el('aud-bind').onclick=()=>begin('bind',{bindings:Object.fromEntries(Object.keys(states).map(k=>[k,num('aud-bind-'+k)]))});
el('aud-wifi').onclick=()=>{const password=el('aud-password').value;el('aud-password').value='';begin('wifi',{ssid:el('aud-ssid').value,password});};
el('aud-net').onclick=()=>begin('net');
function loop(){
  const id=++gen;if(!visible())return;
  (async function tick(){
    if(id!==gen||!visible())return;
    await update();
    if(id===gen&&visible())setTimeout(tick,meta?.busy?450:2200);
  })();
}
for(const b of document.querySelectorAll('[data-tab]'))b.addEventListener('click',()=>{if(!visible()){halt();voicePlayer.pause();recordingPlayer.pause();}loop();});
document.addEventListener('visibilitychange',()=>{if(visible())draw();else recordingPlayer.pause();loop();});
refresh();draw();loop();
})();
