'use strict';
(()=>{
const el=id=>document.getElementById(id);
const states={IDLE:'空闲',RUNNING:'运行',WAITING:'等待',ERROR:'报错',DONE:'完成',OFFLINE:'离线'};
const status=el('aud-result'), canvas=el('aud-wave'), ctx=canvas.getContext('2d');
let src=null,cuts=[],anchor=null,gen=0,url=null,busy=false,meta=null,lastSeq=-1;
const headers={'X-Session-Token':window.HLK_SESSION,'Content-Type':'application/json'};
const signal=m=>{status.textContent=m;};
const num=id=>Number(el(id).value);
const visible=()=>!el('tab-audio').hidden&&!document.hidden;
for(const [k,t] of Object.entries(states)){
  let l=document.createElement('label');l.textContent=t;let v=document.createElement('input');
  v.type='number';v.id='aud-bind-'+k;v.min='0';v.max='3000';v.value='0';l.append(v);el('aud-bindings').append(l);
}
function result(r){
  if(typeof r==='string'){signal(r);return;}
  if(!r||typeof r!=='object')return;
  if(r.bindings)for(const k of Object.keys(states))el('aud-bind-'+k).value=r.bindings[k.toLowerCase()]||0;
  if(r.amp){
    if(r.amp.gain!==undefined)el('aud-gain').value=Number(r.amp.gain);
    signal('C3 已确认 · 功放 '+(r.amp.i2s==='1'?'就绪':'未就绪')+' · 语音分区 '+(r.amp.fs==='1'?'就绪':'未初始化'));
  }
  if(typeof r.state==='string')signal(r.state==='ONLINE'?'C3 已联网：'+r.ip+':8080':'设备尚未取得 IP：'+r.state);
}
function render(x){
  meta=x;el('aud-device').textContent=x.available?'C3 已连接 · '+x.endpoint:'请选择 MAX98357A C3';
  el('aud-device').classList.toggle('ready',x.available);el('aud-progress').value=x.progress;
  for(const b of document.querySelectorAll('#tab-audio [data-device]')){
    b.disabled=busy||x.busy||x.linked_busy||!x.available||(['aud-read','aud-wifi','aud-net'].includes(b.id)&&!x.usb)||b.id==='aud-bind'&&!x.state_voice;
  }
  for(const k of ['aud-tts','aud-clear','aud-file'])el(k).disabled=busy||x.busy;
  if(x.source?.id!==src?.id){
    src=x.source;cuts=[];
    el('aud-source').textContent=src?src.name+' · '+(src.frames/src.rate).toFixed(3)+' 秒':'准备本地语音或导入录音；每段最多 32 秒。';
    el('aud-start').value=0;el('aud-end').value=src?Math.min(32,src.frames/src.rate).toFixed(3):0;
    if(src)for(const i of ['aud-start','aud-end'])el(i).max=src.frames/src.rate;
    renderCuts();draw();
  }
  if(x.seq!==lastSeq&&!x.busy){
    lastSeq=x.seq;if(x.error)signal(x.error);else result(x.result);
  }
}
async function update(){
  if(!visible())return;
  try{render(await api('audio'));}catch(e){signal(e.message);}
}
async function begin(op,args={}){
  if(busy||meta?.busy)return;
  busy=true;signal('处理中…');
  try{await api('audio_job',{op,...args});}catch(e){signal(e.message);}
  finally{busy=false;await update();}
}
function paint(){
  const w=canvas.width,h=canvas.height;
  ctx.fillStyle='#101b1d';ctx.fillRect(0,0,w,h);ctx.fillStyle='#718184';ctx.fillRect(0,h/2,w,1);
  if(!src)return;
  const full=src.frames/src.rate,x=s=>Math.max(0,Math.min(1,s/full))*w;
  ctx.fillStyle='#284d3d';ctx.fillRect(x(num('aud-start')),0,Math.max(0,x(num('aud-end'))-x(num('aud-start'))),h);
  ctx.strokeStyle='#97e5b7';ctx.beginPath();
  src.peaks.forEach(([lo,hi],i)=>{
    let xx=(i+.5)*w/src.peaks.length;
    ctx.moveTo(xx,Math.min(h,h/2-hi*h/70000));ctx.lineTo(xx,Math.max(0,h/2-lo*h/70000));
  });
  ctx.stroke();ctx.fillStyle='#b6c8c3';ctx.font='13px sans-serif';
  ctx.fillText('0.00 秒',8,h-7);ctx.fillText(full.toFixed(3)+' 秒',w-85,h-7);
}
let needs=false;
function draw(){if(!needs){needs=true;requestAnimationFrame(()=>{needs=false;paint();});}}
function secs(e){const b=canvas.getBoundingClientRect();return (src.frames/src.rate)*Math.max(0,Math.min(1,(e.clientX-b.left)/b.width));}
function move(e){
  if(anchor===null||!src)return;
  const v=secs(e);
  el('aud-start').value=Math.min(v,anchor).toFixed(3);el('aud-end').value=Math.max(v,anchor).toFixed(3);draw();
}
canvas.addEventListener('pointerdown',e=>{if(src){anchor=secs(e);canvas.setPointerCapture(e.pointerId);move(e);}});
canvas.addEventListener('pointermove',move);
for(const ev of ['pointerup','pointercancel'])canvas.addEventListener(ev,e=>{move(e);anchor=null;});
for(const id of ['aud-start','aud-end'])el(id).addEventListener('input',draw);
function segment(){
  if(!src)throw new Error('请先准备一段音频。');
  const start=Math.round(num('aud-start')*src.rate);let end=Math.round(num('aud-end')*src.rate);
  if(Math.abs(end-src.frames)<=8)end=src.frames;
  let slot=num('aud-slot');
  if(!(0<=start&&start<end&&end<=src.frames&&(end-start)<=32*src.rate))throw new Error('请在波形上选择不超过 32 秒的片段。');
  if(!Number.isInteger(slot)||slot<1||slot>3000)throw new Error('设备编号是 1–3000。');
  return {start,end,slot,state:el('aud-role').value};
}
function renderCuts(){
  const list=el('aud-cuts');list.replaceChildren();
  cuts.forEach((s,i)=>{
    const li=document.createElement('li');
    li.textContent=(s.start/src.rate).toFixed(3)+' – '+(s.end/src.rate).toFixed(3)+' 秒 → '+s.slot+' · '+(states[s.state]||'不绑定');
    const b=document.createElement('button');b.className='ghost';b.textContent='移除';
    b.addEventListener('click',()=>{cuts.splice(i,1);renderCuts();});li.append(b);list.append(li);
  });
}
el('aud-add').onclick=()=>{
  try{
    const s=segment();
    if(cuts.length>=32||cuts.some(c=>c.slot===s.slot||s.state&&s.state===c.state))throw new Error('同批不能重复编号或状态，最多 32 段。');
    cuts.push(s);el('aud-slot').value=Math.min(3000,s.slot+1);el('aud-role').value='';renderCuts();
  }catch(e){signal(e.message);}
};
el('aud-preview').onclick=async()=>{
  try{
    const rsp=await fetch('/api/audio_preview',{method:'POST',headers,body:JSON.stringify({...segment(),source:src.id})});
    if(!rsp.ok)throw new Error((await rsp.json()).error);
    if(url)URL.revokeObjectURL(url);url=URL.createObjectURL(await rsp.blob());
    const p=el('aud-player');p.src=url;p.hidden=false;await p.play();
  }catch(e){signal(e.message);}
};
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
  try{
    busy=true;signal('正在导入…');
    const rsp=await fetch('/api/audio_import',{method:'POST',headers:{...headers,'Content-Type':'application/octet-stream','X-Audio-Name':encodeURIComponent(file.name)},body:await portableAudio(file)});
    if(!rsp.ok)throw new Error((await rsp.json()).error);
  }catch(err){signal(err.message);}
  finally{busy=false;await update();}
};
el('aud-push').onclick=()=>{
  try{
    let rows=cuts.length?cuts:[segment()];
    if(confirm('推送会替换设备上相同编号的语音。继续？'))
      begin('push',{source:src.id,cuts:rows,play:el('aud-auto-play').checked,gain:num('aud-gain')});
  }catch(e){signal(e.message);}
};
el('aud-tts').onclick=()=>begin('tts',{text:el('aud-text').value});
el('aud-read').onclick=()=>begin('read',{slot:num('aud-slot')});
el('aud-play').onclick=()=>begin('play',{slot:num('aud-slot'),gain:num('aud-gain')});
el('aud-stop').onclick=()=>begin('stop');
el('aud-clear').onclick=()=>{if(url){URL.revokeObjectURL(url);url=null;}el('aud-player').hidden=true;begin('clear');};
el('aud-gain-apply').onclick=()=>begin('gain',{gain:num('aud-gain')});
el('aud-count-apply').onclick=()=>begin('count',{count:num('aud-count')});
el('aud-fetch').onclick=()=>begin('status');
el('aud-bind').onclick=()=>begin('bind',{bindings:Object.fromEntries(Object.keys(states).map(k=>[k,num('aud-bind-'+k)]))});
el('aud-wifi').onclick=()=>{const password=el('aud-password').value;el('aud-password').value='';begin('wifi',{ssid:el('aud-ssid').value,password});};
el('aud-net').onclick=()=>begin('net');
function loop(){
  const id=++gen;if(!visible())return;
  (async function tick(){
    if(id!==gen||!visible())return;
    await update();
    if(id===gen&&visible())setTimeout(tick,meta?.busy?475:2800);
  })();
}
for(const b of document.querySelectorAll('[data-tab]'))b.addEventListener('click',loop);
document.addEventListener('visibilitychange',loop);
draw();loop();
})();
