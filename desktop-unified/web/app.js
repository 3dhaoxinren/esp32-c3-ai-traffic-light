'use strict';
const $ = id => document.getElementById(id);
let lastState = null;
let lastLogId = -1;
let pending = false;
let closed = false;
let polling = false;
let nextAutoRefresh = Infinity;
let startupDirty = false;
const kinds = {info:'·', tx:'TX', rx:'RX', success:'OK', error:'!', warn:'!'};

async function api(action, data) {
  const options = {headers: {'X-Session-Token': window.HLK_SESSION}, cache:'no-store'};
  if (data !== undefined) {
    options.method = 'POST';
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(data);
  }
  const res = await fetch('/api/' + action, options);
  const result = await res.json();
  if (!res.ok) throw new Error(result.error || '操作失败');
  return result;
}

function showError(message) {
  $('ui-error').textContent = message;
  $('ui-error').hidden = !message;
}

async function perform(action, data = {}) {
  if (pending || closed) return false;
  pending = true;
  showError('');
  if (lastState) render(lastState);
  try {
    await api(action, data);
    await poll();
    return true;
  } catch (error) {
    showError(error.message);
    return false;
  } finally {
    pending = false;
    if (lastState) render(lastState);
  }
}

async function refreshPorts() {
  try {
    const list = await api('ports');
    const lightPrevious = $('light-port').value;
    $('light-port').replaceChildren();
    if (!list.length) $('light-port').add(new Option('未发现 Nano / C3 串口', ''));
    for (const item of list) $('light-port').add(new Option(item.device + ' · ' + item.description, item.device));
    if (list.some(p => p.device === lightPrevious)) $('light-port').value = lightPrevious;
    const previous = $('port').value;
    $('port').replaceChildren();
    const atPorts=list.filter(x=>x.kind!=='esp32c3');
    if (!atPorts.length) $('port').add(new Option('未发现用于 AT 的串口', ''));
    for (const item of atPorts) $('port').add(new Option(item.device + ' · ' + item.description, item.device));
    if (atPorts.some(p => p.device === previous)) $('port').value = previous;
    if (lastState) render(lastState);
  } catch (error) { showError(error.message); }
}

function render(state) {
  // An opt-in automatic check starts 15 seconds after the current job ends.
  if (state.busy || !state.connected || lastState?.busy) nextAutoRefresh = Date.now() + 15000;
  lastState = state;
  const busy = state.busy || state.lights?.busy || pending;
  renderLights(state);
  renderAi(state);
  renderStartup(state);
  $('demo-banner').hidden = !state.demo;
  const network = $('transport').value === 'udp';
  $('connect').disabled = busy || state.connected || state.lights?.connected || (!network && !$('port').value);
  $('connect').textContent = network ? '连接局域网 AT' : '连接并读取状态';
  $('disconnect').disabled = !state.connected || pending;
  $('port').disabled = busy || state.connected;
  $('baud').disabled = busy || state.connected;
  $('transport').disabled = busy || state.connected;
  $('host').disabled = busy || state.connected;
  $('refresh').disabled = busy || state.connected;
  $('detect').disabled = busy || !state.connected;
  const normal = state.at_ready && ['at','remote'].includes(state.mode);
  $('read').disabled = busy || !normal;
  $('configure').disabled = busy || !state.at_ready;
  $('transparent').disabled = busy || !state.at_ready;
  $('audit').disabled = busy || !state.at_ready;
  $('refresh-status').disabled = busy || !state.connected || state.mode === 'transparent';
  $('upgrade').disabled = busy || !normal || state.transport !== 'serial' || !state.upgrade_supported;
  $('return-at').disabled = busy || state.mode !== 'recovery';
  $('nano-baud').disabled = busy || !normal || state.transport !== 'serial';
  $('detect').disabled = busy || !state.connected || ['transparent','recovery'].includes(state.mode);
  const writing = state.busy && !['read','status','detect','connect','audit'].includes(state.job);
  for (const id of ['ssid','password','auth','show-password']) $(id).disabled = writing;
  if (state.connected) {
    $('transport').value = state.transport;
    $('baud').value = String(state.baud);
    $('serial-fields').hidden = state.transport === 'udp';
    $('udp-fields').hidden = state.transport !== 'udp';
    if (state.transport === 'serial' && [...$('port').options].some(p=>p.value===state.port)) $('port').value=state.port;
  }
  $('stop').hidden = !state.busy;
  $('stop').disabled = pending;
  $('stage').textContent = state.stage;
  $('message').textContent = state.message;
  $('mode-badge').textContent = state.mode==='recovery'?'Recovery':state.at_ready ? (state.transport==='udp'?'远程 AT':'AT 就绪') : state.connected ? '待确认' : '未连接';
  $('mode-badge').className = 'badge' + (state.at_ready ? ' ready' : '');
  $('device-dot').className = 'dot' + (state.at_ready ? ' ready' : '');
  $('device-label').textContent = state.connected ? [state.port, state.version || '等待版本信息'].join(' · ') : '模块尚未连接';
  $('status-dot').className = 'dot' + (state.busy ? ' running' : state.result === 'error' ? ' error' : state.at_ready ? ' ready' : '');
  $('progress').hidden = !state.total;
  $('progress').max = state.total || 1;
  $('progress').value = state.progress;
  $('progress-count').textContent = state.total ? `${state.progress} / ${state.total}` : '';
  const lamp=state.lights||{};
  if(lamp.connected||lamp.busy||lamp.result==='error'){
    $('stage').textContent=lamp.busy?'灯控操作中':lamp.connected?'灯控已连接':'灯控需要处理';
    $('message').textContent=lamp.message;
    $('status-dot').className='dot '+(lamp.busy?'running':lamp.result==='error'?'error':'ready');
  }
  renderDevice(state);
  renderConfig(state.config);
  const latest = state.logs.length ? state.logs[state.logs.length - 1].id : 0;
  $('log-count').textContent=state.logs.length+' 条';
  if (latest !== lastLogId) {
    lastLogId = latest;
    const box = $('logs');
    const wasAtBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 50;
    box.replaceChildren();
    if (!state.logs.length) {
      const empty = document.createElement('div'); empty.className='empty-log';
      empty.textContent='连接后，命令与模块回复会显示在这里。'; box.append(empty);
    }
    for (const log of state.logs) {
      const row = document.createElement('div');
      row.className = 'log-row' + (log.kind === 'error' ? ' error-row' : '');
      for (const [className,text] of [['log-time',log.time],['log-kind',kinds[log.kind] || '·'],['log-text',log.text]]) {
        const item = document.createElement('span'); item.className=className; item.textContent=text; row.append(item);
      }
      box.append(row);
    }
    if (wasAtBottom) box.scrollTop = box.scrollHeight;
  }
}

function renderDevice(s) {
  const d=s.device||{}, now=Date.now()/1000, age=s.last_seen?Math.max(0,Math.floor(now-s.last_seen)):null;
  const responsive=s.connected && (s.at_ready || s.mode==='recovery') && age!==null && age<35;
  const manualWaiting = !$('auto-refresh').checked && (s.at_ready || s.mode==='recovery') && age!==null;
  $('live-device').textContent=responsive?'响应正常':s.connected?(s.busy?'正在读取':s.mode==='transparent'?'已暂停查询':manualWaiting?'已连接 · 待刷新':'未确认响应'):'尚未连接';
  $('device-dot').className='dot'+(responsive?' ready':'');
  $('live-port').textContent=s.connected?`${s.port} · ${s.transport==='serial'?s.baud+' baud':'局域网 AT'}`:'连接后读取设备回复';
  $('live-mode').textContent=({at:'AT 配置',remote:'远程 AT',recovery:'升级模式',transparent:'数据透传'})[s.mode]||'待检测';
  $('live-firmware').textContent=s.upgrade_supported?'V1 · 上电 AT / 指令升级':s.transport==='udp'?'串口模式未检测':'升级能力未确认';
  const suspended=['recovery','transparent'].includes(s.mode);
  const stale=s.status_updated && (now-s.status_updated>35 || !s.at_ready);
  $('live-wifi').textContent=suspended?'暂停查询':(!s.connected||!s.status_updated)?'未确认':(stale?'上次：':'')+({unconfigured:'未配置 Wi-Fi',reachable:'局域网可达',unverified:'联网待确认'}[d.wifi]||'未确认');
  $('live-wifi').className=d.wifi==='unconfigured'?'warn':'';
  $('live-ssid').textContent=d.ssid===undefined||d.ssid===null?'名称未读取':d.ssid?'已存名称：'+d.ssid:'模块保存的名称为空';
  $('live-ip').textContent=(suspended||stale?'上次：':'')+(d.ip||'—');
  $('live-mac').textContent='MAC '+(d.mac||'—');
  $('live-seen').textContent=age===null?'无最新响应':`上次回复 ${age} 秒前`;
  $('updated-at').textContent=s.status_updated?'状态更新 '+new Date(s.status_updated*1000).toLocaleTimeString('zh-CN',{hour12:false}):'尚未读取设备';
  $('status-warnings').hidden=!(s.warnings||[]).length;
  $('status-warnings').textContent=(s.warnings||[]).join('；')+'。其余已读取状态仍有效。';
  $('wifi-notice').hidden=d.wifi!=='unconfigured'||suspended||!s.at_ready;
  $('wifi-notice').textContent='小板通信正常，但保存的 Wi-Fi 名称为空。填写网络名称和密码即可配置，无需短接 ES/DF。';
  $('connection-help').textContent=s.mode==='recovery'?'当前在升级模式。可返回普通 AT，或释放串口供升级器接管。':s.upgrade_supported?'已检测到 V1 固件：上电即可使用 AT，ES/DF 保持上拉即可。':'使用 USB 串口读取模块身份与固件能力。ES/DF 保持原来的上拉接法。';
  $('socket-summary').textContent=d.protocol?`当前类型 ${({'1':'TCP 服务端','2':'TCP 客户端'})[d.protocol]||d.protocol}；目标 ${d.remote_ip||'未读取'}:${d.remote_port||'未读取'}；本地端口 ${d.local_port||'未读取'}。`:'连接后显示透传目标和端口。';
  const recovery=s.mode==='recovery';
  $('upgrade-badge').textContent=recovery?'Recovery 就绪':s.upgrade_supported?'V1 入口可用':'待检测';
  $('upgrade-badge').className='badge'+(s.upgrade_supported?' ready':'');
  $('upgrade-title').textContent=recovery?'已进入升级模式':s.upgrade_supported?'支持 AT 指令进入升级':'先连接 USB 串口';
  $('upgrade-description').textContent=recovery?'已验证 IoT_V1.30_2。可返回 AT，或释放串口给升级器。':s.upgrade_supported?'已收到 UP=1:UART-RECOVERY-V1。点击下方按钮即可进入 Recovery。':'仅在串口检测到 UP 能力回复后开放入口。';
}

function renderConfig(values) {
  const labels = {WA:'网络模式',WM:'STA 配置方式',Sssid:'Wi-Fi 名称',Sssidl:'名称字节数',Sam:'加密方式',dhcp:'DHCP',DE:'远程 AT 开关',DP:'远程 AT 前缀',mac:'MAC（原始回复）',ip:'IP 参数',Ub:'模块波特率',UType:'透传类型',UIp:'目标地址',URPort:'目标端口',ULPort:'本地端口',UP:'升级能力'};
  const descriptions = {WA:{'0':'STA（连接路由器）','1':'AP（热点）'},WM:{'2':'手动参数'},Sam:{'9':'WPA/WPA2-PSK','7':'WPA2-PSK'},dhcp:{'1':'自动获取'}};
  $('config-block').hidden = !Object.keys(values).length;
  $('config-values').replaceChildren();
  for (const [key,value] of Object.entries(values)) {
    const row=document.createElement('div'),dt=document.createElement('dt'),dd=document.createElement('dd');
    dt.textContent=labels[key] || key; dd.textContent=descriptions[key]?.[value] || (value===''?'未设置':value);
    row.append(dt,dd); $('config-values').append(row);
  }
  $('use-ssid').hidden = !values.Sssid;
  $('use-ssid').disabled = lastState?.busy || pending;
}

async function poll() {
  if (closed || polling) return;
  polling = true;
  try {
    render(await api('state'));
    if (!document.hidden && $('auto-refresh').checked && lastState.connected && !lastState.busy && !pending && (lastState.at_ready || lastState.mode === 'recovery') && Date.now()>=nextAutoRefresh) {
      nextAutoRefresh=Date.now()+15000;
      perform('status',{quiet:true});
    }
  }
  catch (_) {
    $('stage').textContent='无法连接本机程序';
    $('message').textContent='请重新打开“AI指示灯.exe”，使用新打开的界面。';
    for (const id of ['connect','disconnect','detect','configure','read','transparent','audit','stop','upgrade','return-at','nano-baud','refresh-status']) $(id).disabled=true;
    document.querySelectorAll('#tab-lights button,#tab-ai button').forEach(b=>b.disabled=true);
    $('light-message').textContent='本机AI指示灯已断开，请重新打开程序。';
    $('live-device').textContent='AI指示灯已断开';$('device-dot').className='dot error';
    $('live-wifi').textContent='状态已过期';
    $('ai-connection-status').textContent='AI指示灯已断开';
    $('ai-connection-status').className='badge connection-error';
    $('ai-connection-seen').textContent='连接状态已过期，请重新打开AI指示灯';
    $('ai-beacon').className='ai-beacon color-off';
  }
  finally { polling = false; }
}

function counts() {
  $('ssid-count').textContent = new TextEncoder().encode($('ssid').value).length + ' / 32 字节';
  $('password-count').textContent = $('password').value ? $('password').value.length + ' 位' : '8–63 位';
}

$('refresh').addEventListener('click',refreshPorts);
$('transport').addEventListener('change',()=>{
  const network=$('transport').value==='udp';$('serial-fields').hidden=network;$('udp-fields').hidden=!network;
  if(lastState)render(lastState);
});
$('connect').addEventListener('click',()=>perform('connect',{transport:$('transport').value,host:$('host').value,port:$('port').value,baud:Number($('baud').value)}));
$('disconnect').addEventListener('click',()=>perform('disconnect'));
$('detect').addEventListener('click',()=>perform('detect'));
$('read').addEventListener('click',()=>perform('read'));
$('refresh-status').addEventListener('click',()=>{nextAutoRefresh=Date.now()+15000;perform('status');});
// Do not restore a browser-cached checked state after reload or reconnect.
$('auto-refresh').checked = false;
$('auto-refresh').addEventListener('change',()=>{
  nextAutoRefresh = $('auto-refresh').checked ? Date.now()+15000 : Infinity;
  if(lastState) render(lastState);
});
function confirmAction(title,description,action){
  $('dialog-title').textContent=title;$('dialog-description').textContent=description;
  const dialog=$('action-dialog');dialog.returnValue='cancel';
  dialog.addEventListener('close',()=>{if(dialog.returnValue==='confirm')perform(action);},{once:true});dialog.showModal();
}
$('transparent').addEventListener('click',()=>confirmAction('切换到数据透传？','请先确认 TCP/UDP 目标参数。切换后暂停 AT 状态查询；这版固件断电重启可回到 AT。','transparent'));
$('upgrade').addEventListener('click',()=>confirmAction('进入固件升级模式？','模块将重启进入 Recovery，串口切到 115200。此步骤只切换模式，之后可以一键返回 AT。','upgrade'));
$('return-at').addEventListener('click',()=>perform('return_at'));
$('nano-baud').addEventListener('click',()=>confirmAction('把模块设为 9600？','为经典 Nano 的软件串口准备。模块通信速度将改为 9600，AI指示灯会同步切换并验证；需要时会重启模块。','nano_baud'));
document.querySelectorAll('[data-tab]').forEach(button=>button.addEventListener('click',()=>{
  document.body.dataset.view=button.dataset.tab;
  document.querySelectorAll('[data-tab]').forEach(b=>{const selected=b===button;b.classList.toggle('active',selected);b.setAttribute('aria-selected',String(selected));});
  history.replaceState(null,'','#'+button.dataset.tab);
  for(const panel of document.querySelectorAll('.tab-panel'))panel.hidden=panel.id!=='tab-'+button.dataset.tab;
}));
$('audit').addEventListener('click',()=>perform('audit'));
$('stop').addEventListener('click',()=>perform('stop'));
$('use-ssid').addEventListener('click',()=>{$('ssid').value=lastState.config.Sssid;counts();});
$('show-password').addEventListener('click',()=>{
  const show=$('password').type==='password'; $('password').type=show?'text':'password';
  $('show-password').textContent=show?'隐藏':'显示'; $('show-password').setAttribute('aria-pressed',String(show));
});
for (const id of ['ssid','password']) $(id).addEventListener('input',counts);
$('wifi-form').addEventListener('submit',async event=>{
  event.preventDefault();
  const ssid=$('ssid').value,password=$('password').value;
  if (!/^[\x20-\x7e]{1,32}$/.test(ssid)) {showError('Wi-Fi 名称需为 1–32 位英文、数字或半角符号。');return;}
  if (!/^[\x20-\x7e]{8,63}$/.test(password)) {showError('密码需为 8–63 位英文、数字或半角符号。');return;}
  if (await perform('configure',{ssid,password,auth:Number($('auth').value)})) {
    $('password').value=''; $('password').type='password'; $('show-password').textContent='显示';
    $('show-password').setAttribute('aria-pressed','false'); counts();
  }
});
$('export').addEventListener('click',()=>{
  if (!lastState) return;
  const text=lastState.logs.map(l=>`${l.time} [${kinds[l.kind] || '·'}] ${l.text}`).join('\r\n');
  const url=URL.createObjectURL(new Blob([text],{type:'text/plain;charset=utf-8'}));
  const link=document.createElement('a');link.href=url;link.download='HLK-M30-session.txt';link.click();
  setTimeout(()=>URL.revokeObjectURL(url),1000);
});
$('exit').addEventListener('click',async()=>{
  try { await api('exit',{});closed=true;clearInterval(poller);
    $('stage').textContent='程序已退出';$('message').textContent='连接已关闭，可以关闭此页面。';
    document.querySelectorAll('button,input,select').forEach(e=>e.disabled=true);
  } catch (e) {showError(e.message);}
});

function renderStartup(s) {
  const a=s.startup||{}, t=a.target, busy=pending||closed||s.demo;
  if(!startupDirty){
    $('startup-logon').checked=!!a.autostart?.enabled;
    $('startup-connect').checked=!!a.auto_connect;
    $('startup-follow').checked=!!a.auto_follow;
    $('startup-roam').checked=!!a.roam_usb;
  }
  $('startup-logon').disabled=busy||!a.autostart?.supported;
  $('startup-connect').disabled=busy;
  $('startup-follow').disabled=busy;
  $('startup-roam').disabled=busy;
  $('startup-save').disabled=busy;
  $('startup-resume').disabled=busy||!a.auto_connect||!t||s.lights?.connected||s.lights?.busy||s.busy||s.connected;
  $('startup-target').textContent=t?'已记住：'+(t.transport==='tcp'?`Wi-Fi TCP · ${t.host}:${t.tcp_port}`:`USB · ${t.port} · 115200${t.location?' · USB位置 '+t.location:''}（多串口优先按序列号或已记住的位置匹配）`):'尚未记住设备，请先在“灯控调试”成功连接一次。';
  $('startup-feedback').textContent=startupDirty?'有未保存修改':a.warning||a.autostart?.warning||a.message||'尚未启用自动连接';
}
for(const id of ['startup-logon','startup-connect','startup-follow','startup-roam'])$(id).addEventListener('change',()=>{startupDirty=true;if(lastState)renderStartup(lastState);});
$('startup-save').addEventListener('click',async()=>{
  const saved=await perform('startup_settings',{autostart:$('startup-logon').checked,auto_connect:$('startup-connect').checked,auto_follow:$('startup-follow').checked,roam_usb:$('startup-roam').checked});
  if(saved){startupDirty=false;if(lastState)renderStartup(lastState);}
});
$('startup-resume').addEventListener('click',()=>perform('startup_resume'));

let soundDirty=false;
function renderLights(s) {
  const l=s.lights||{}, connected=!!l.connected;
  const busy=s.busy||l.busy||pending||closed;
  if (connected) {
    $('light-transport').value=l.transport;
    if(l.transport==='tcp') { const [host,port]=l.endpoint.split(':');$('light-host').value=host;$('light-tcp-port').value=port; }
    else { const port=l.endpoint.split(' · ')[0];if([...$('light-port').options].some(p=>p.value===port))$('light-port').value=port; }
  }
  const tcp=$('light-transport').value==='tcp';
  $('light-usb-fields').hidden=tcp;$('light-tcp-fields').hidden=!tcp;
  $('light-connect').disabled=busy||connected||s.connected||s.demo||(!tcp&&!$('light-port').value)||(tcp&&!$('light-host').value.trim());
  $('light-disconnect').disabled=busy||!connected;
  for(const id of ['light-transport','light-port','light-host','light-tcp-port','light-refresh'])$(id).disabled=busy||connected;
  document.querySelectorAll('[data-light-command]').forEach(b=>b.disabled=busy||!connected);
  $('light-net').disabled=busy||!connected||l.transport!=='serial';
  $('light-c3-audio').hidden=!(connected&&l.max_pcm);
  const audio=connected&&l.sound_v2&&!l.max_pcm;
  $('toy-audio').hidden=!audio;
  $('toy-number').hidden=!(audio&&l.numbered_play);
  $('toy-play').disabled=busy||!audio||!l.numbered_play;
  if(!audio)soundDirty=false;
  if(audio&&!soundDirty)$('toy-count').value=l.sound_count;
  $('toy-save').disabled=$('toy-test').disabled=busy||!audio;
  $('toy-hint').textContent=(audio?'灯板已保存 '+l.sound_count+' 条按键语音。':'')+(l.sound_message||'');
  $('light-connect-hint').textContent=s.connected?'请先点击左侧“释放串口 / 断开连接”，再连接灯控。':tcp?'可点「搜索局域网灯板」填入 IP，或手动填写；默认端口 8080。':'关闭 Arduino 串口监视器。初次打开 USB 可能让 Nano 重启，后续按钮共用同一连接。';
  $('light-badge').textContent=l.busy?'正在操作':connected?'已连接 · '+l.endpoint:'未连接';
  $('light-badge').className='badge'+(connected?' ready':'');
  const colors={RED:'红灯',YELLOW:'黄灯',GREEN:'绿灯',OFF:'全灭'};
  $('light-state').textContent=l.updated?`${connected?'':'上次：'}${l.mode==='AI'?'AI · '+(aiLabels[(l.ai_state||'').toLowerCase()]||'未知')+' · ':l.mode==='AUTO'?'自动循环 · ':l.mode==='VOICE'?'语音接管 · ':'手动 · '}${colors[l.color]||'未知'}`:'尚未读取灯态';
  $('light-updated').textContent=l.updated?'回执时间 '+new Date(l.updated*1000).toLocaleTimeString('zh-CN',{hour12:false})+' · 点击查询更新':'状态仅在操作或查询后更新';
  for(const color of ['red','yellow','green'])$('lamp-'+color).classList.toggle('lit',connected&&l.color===color.toUpperCase());
  $('light-message').textContent=l.message||'连接后自动查询一次；不会定时发送查询指令。';
  $('light-message').className=l.result==='error'?'error':'hint';
  $('light-reply').hidden=!l.reply;$('light-reply').textContent=l.reply||'';
}

$('toy-count').addEventListener('input',()=>soundDirty=true);
$('toy-save').addEventListener('click',async()=>{
  const el=$('toy-count');const n=el.valueAsNumber;
  if(!Number.isInteger(n)||n<0||n>32){el.reportValidity();showError('请输入 0–32 的整数');return;}
  if(await perform('light_sound',{op:'count',count:n}))soundDirty=false;
});
$('toy-play').addEventListener('click',()=>{if(!$('toy-track').reportValidity())return;const track=Number($('toy-track').value);if(Number.isInteger(track)&&track>=1&&track<=3000)perform('light_sound',{op:'play',track});});
$('toy-test').addEventListener('click',()=>perform('light_sound',{op:'finish'}));
$('light-refresh').addEventListener('click',refreshPorts);
$('light-open-audio').onclick=()=>document.querySelector('[data-tab="audio"]').click();
$('light-host').addEventListener('input',()=>{if(lastState)renderLights(lastState);});
$('light-transport').addEventListener('change',()=>{if(lastState)render(lastState);});
$('light-connect').addEventListener('click',()=>perform('light_connect',{transport:$('light-transport').value,port:$('light-port').value,host:$('light-host').value.trim(),tcp_port:Number($('light-tcp-port').value)}));
$('light-disconnect').addEventListener('click',()=>perform('light_disconnect'));
$('light-net').addEventListener('click',()=>perform('light_send',{command:'NET ON'}));
document.querySelectorAll('[data-light-command]').forEach(button=>button.addEventListener('click',()=>perform('light_send',{command:button.dataset.lightCommand})));


const aiLabels={done:'完成',idle:'空闲 / 完成',running:'执行中',waiting:'等待确认',error:'异常 / 报错',offline:'未接入 / 状态失效'};
const defaultPatterns={idle:{color:'GREEN',effect:'SOLID'},running:{color:'YELLOW',effect:'BREATHE'},waiting:{color:'YELLOW',effect:'SLOW'},error:{color:'RED',effect:'FAST'},offline:{color:'OFF',effect:'SOLID'}};
const colorNames={RED:'红灯',YELLOW:'黄灯',GREEN:'绿灯',OFF:'熄灯'};
const effectNames={SOLID:'常亮',BREATHE:'呼吸',SLOW:'慢闪',FAST:'快闪'};
const patternStates=['idle','running','waiting','error'];
let patternsDirty=false, patternsLoaded=false;
function patternName(p){return p.color==='OFF'?'熄灯':colorNames[p.color]+effectNames[p.effect];}
function normalizePatternControls(state){
  const color=$('pattern-'+state+'-color').value, effect=$('pattern-'+state+'-effect');
  for(const option of effect.options) option.disabled=(color==='OFF'&&option.value!=='SOLID')||(option.value==='BREATHE'&&!['YELLOW','GREEN'].includes(color));
  if(effect.selectedOptions[0]?.disabled){effect.value='SOLID';return true;}
  return false;
}
function fillPatterns(patterns){
  for(const state of patternStates){
    $('pattern-'+state+'-color').value=patterns[state].color;
    $('pattern-'+state+'-effect').value=patterns[state].effect;
    normalizePatternControls(state);
  }
  patternsLoaded=true;
}
function readPatterns(){
  const patterns={offline:{...defaultPatterns.offline}};
  for(const state of patternStates)patterns[state]={color:$('pattern-'+state+'-color').value,effect:$('pattern-'+state+'-effect').value};
  return patterns;
}
function renderPatterns(a){
  const saved=a.patterns||defaultPatterns;
  if(!patternsLoaded||!patternsDirty)fillPatterns(saved);
  for(const state of patternStates){
    $('pattern-'+state+'-saved').textContent='已保存：'+patternName(saved[state]);
    for(const field of ['color','effect'])$('pattern-'+state+'-'+field).disabled=pending||closed;
  }
  $('ai-save-patterns').disabled=!patternsDirty||pending||closed;
  $('ai-reset-patterns').disabled=pending||closed;
  $('ai-settings-summary').textContent=patternsDirty?'有未保存修改':'已保存 · 重启保留';
  if(a.settings_warning){$('ai-settings-note').textContent=a.settings_warning;$('ai-settings-note').className='warning-text';}
}
function renderAiConnection(s){
  const l=s.lights||{}, hasEndpoint=!!l.endpoint, tcp=l.transport==='tcp';
  $('ai-connection-type').textContent=hasEndpoint?(tcp?'Wi-Fi · TCP 无线':'USB · Nano 串口'):'尚未连接灯板';
  $('ai-connection-endpoint').textContent=l.endpoint||'—';
  $('ai-connection-status').textContent=l.connected?(l.result==='error'?'已连接 · 需检查':'已连接'):l.busy?'连接中…':l.result==='error'?'连接失败':'未连接';
  $('ai-connection-status').className='badge'+(l.result==='error'?' connection-error':l.connected?' ready':'');
  $('ai-connection-path').textContent=hasEndpoint?(tcp?'电脑 → Wi-Fi → HLK-M30 → Nano → 灯板':'电脑 → USB → Nano → 灯板'):'在“灯控调试”中选择 Wi-Fi TCP 或 Nano USB。';
  $('ai-connection-seen').textContent=l.updated?'最近回执 '+new Date(l.updated*1000).toLocaleTimeString('zh-CN',{hour12:false})+(l.connected?'':' · 当前已断开'):'尚无灯板回执';
  $('ai-connect-tab').textContent=l.connected?'管理灯控连接':'连接灯板';
  $('ai-desktop-note').textContent=s.desktop?.tray?'AI指示灯已常驻右下角托盘。关闭网页后继续跟随；双击托盘图标重新打开，右键可暂停或退出。':'关闭网页不会退出AI指示灯；重新打开程序可返回界面。';
}
for(const state of patternStates){
  for(const field of ['color','effect'])$('pattern-'+state+'-'+field).addEventListener('change',()=>{
    const normalized=normalizePatternControls(state);patternsDirty=true;
    $('ai-settings-note').className='hint';
    $('ai-settings-note').textContent=normalized?'该灯色不支持原灯效，已改为常亮。点击“保存灯效”应用。':'尚未保存，实体灯继续使用已保存的灯效。';
    if(lastState)renderAi(lastState);
  });
}
$('ai-reset-patterns').addEventListener('click',()=>{fillPatterns(defaultPatterns);patternsDirty=true;$('ai-settings-note').className='hint';$('ai-settings-note').textContent='已填入默认灯效，点击“保存灯效”应用。';if(lastState)renderAi(lastState);});
$('ai-save-patterns').addEventListener('click',async()=>{
  const patterns=readPatterns();
  if(await perform('ai_settings',{patterns})){
    patternsDirty=false;patternsLoaded=false;
    $('ai-settings-note').className='hint';$('ai-settings-note').textContent='灯效已保存。自动跟随或试亮时应用，重启AI指示灯仍保留。';
    if(lastState)renderAi(lastState);
  }
});
let lastAiRows='';
let screenDirty=false;
for(const id of ['screen-rotation','screen-mode','screen-done-text'])$(id).addEventListener('input',()=>{screenDirty=true;});
$('screen-save').addEventListener('click',async()=>{
  if(await perform('screen_settings',{rotation:Number($('screen-rotation').value),mode:$('screen-mode').value,done_text:$('screen-done-text').value}))screenDirty=false;
});
$('screen-notice-send').addEventListener('click',()=>perform('screen_notice',{text:$('screen-notice-text').value,seconds:Number($('screen-notice-seconds').value)}));
function renderScreen(s){
  const settings=s.ai?.screen||{rotation:0,mode:'AUTO'},l=s.lights||{},ack=l.screen;
  if(!screenDirty){$('screen-rotation').value=String(settings.rotation);$('screen-mode').value=settings.mode;$('screen-done-text').value=settings.done_text||'已完成';}
  $('screen-notice-send').disabled=pending||closed||l.busy||!l.connected||l.screen_supported===false||settings.mode==='OFF'||screenDirty;
  $('screen-notice-feedback').textContent=l.busy?'正在与 Nano 通信…':l.notice_message||'通知发送期间请稍候；结束后恢复当前模式。';
  $('screen-save').disabled=pending||closed;
  const matched=ack&&ack.rotation===settings.rotation&&ack.mode===settings.mode;
  $('screen-feedback').textContent=screenDirty?'有未保存修改':!l.connected?'设置已保存，连接 Nano 后应用':l.screen_supported===false?'当前固件仅支持灯控，无屏幕功能；USB / 无线灯控及 AI 跟随不受影响。':l.screen_error||(!matched?'设置已保存，等待 Nano 应用':ack.online?'屏幕已应答 · '+({AUTO:'自动跟随',RAIN:'代码雨预览',DONE:'已完成预览',STATUS:'仅显示状态',OFF:'关屏'}[ack.mode])+' · 模型：'+({DONE:'已完成',RUNNING:'处理中',IDLE:'待机',WAITING:'等待确认',ERROR:'异常',OFFLINE:'离线'}[ack.state]||ack.state)+' · '+ack.rotation+'°':'Nano 已收设置，等待屏幕应答');
}
function renderAi(s){
  const a=s.ai||{},l=s.lights||{},busy=s.busy||l.busy||pending||closed;
  $('ai-badge').textContent=l.connected&&l.voice_hold?'语音手动接管':a.preview?'正在试亮':a.enabled?'自动跟随中':'跟随已暂停';
  $('ai-badge').className='badge'+(a.enabled?' ready':'');
  $('ai-state').textContent=aiLabels[a.state]||'未接入';
  renderAiConnection(s);renderPatterns(a);renderScreen(s);
  const patterns=a.patterns||defaultPatterns, target=patterns[a.target]||patterns.offline;
  $('ai-target').textContent=(a.preview?'试亮：':a.enabled?'目标灯效：':'暂停：')+patternName(target);
  const held=l.connected&&l.voice_hold;
  $('voice-state').textContent=held?'Nano 已报告语音手动接管；语音板供电与播报需现场确认。':l.connected?'灯板已连接，尚无语音接管回执；语音板状态未确认。':'灯板未连接，语音扩展状态未确认。';
  if(held)$('ai-target').textContent='语音接管：'+(colorNames[l.color]||'未知')+' · 说“恢复跟随”恢复 AI 灯效';
  const visible=held?{color:l.color||'OFF',effect:'SOLID'}:l.connected&&(a.enabled||a.preview)?target:patterns.offline;
  $('ai-beacon').className='ai-beacon color-'+visible.color.toLowerCase()+' effect-'+visible.effect.toLowerCase();
  $('ai-beacon').setAttribute('aria-label','目标灯效：'+patternName(visible));
  $('ai-enable').disabled=busy||!l.connected||a.enabled||s.demo;
  $('ai-disable').disabled=busy||(!a.enabled&&!a.preview);
  $('ai-codex').checked=a.codex_enabled!==false;$('ai-codex').disabled=pending||closed||s.demo;
  $('ai-link').textContent=l.connected?`灯板已连接 ${l.endpoint}。${l.result==='error'?l.message:'灯板回执：'+(l.reply||'等待回执')}`:'灯板未连接，当前只显示汇总状态。先点击“连接灯板”。';
  $('ai-observer').textContent=a.observer||'尚未开始本地监听';
  document.querySelectorAll('[data-ai-preview]').forEach(b=>{b.disabled=busy||!l.connected||s.demo;b.textContent=aiLabels[b.dataset.aiPreview]+' · '+patternName(patterns[b.dataset.aiPreview]);});
  const rows=a.sessions||[], key=JSON.stringify(rows.map(r=>[r.source,r.session,r.state,r.stale,r.effective,r.acknowledged,r.event_id]));
  const errors=rows.filter(r=>r.effective==='error'&&!r.stale);
  $('ai-ack-errors').hidden=!errors.length;
  $('ai-ack-errors').disabled=pending||closed;
  $('ai-ack-note').textContent=errors.length?`${errors.length} 个未确认异常。查看后点击按钮，仅解除这些异常的灯光报警；新异常仍会提醒。`:rows.some(r=>r.acknowledged)?'已查看的异常已解除灯光报警；任务原始失败记录仍保留。':'';
  if(key!==lastAiRows){
    lastAiRows=key;$('ai-sessions').replaceChildren();
    if(!rows.length){const p=document.createElement('p');p.className='hint';p.textContent='还没有有效会话。Codex 会自动读取；其他 AI 需配置状态上报。';$('ai-sessions').append(p);}
    for(const row of rows){const el=document.createElement('div');el.className='ai-session';
      const title=document.createElement('strong');title.textContent=row.source;
      const session=document.createElement('small');session.textContent=row.session;session.title=row.session;
      const status=document.createElement('span');status.className='ai-session-state state-'+row.effective;status.textContent=row.stale?'已失效':row.acknowledged?'异常已查看':aiLabels[row.effective];
      el.append(title,session,status);$('ai-sessions').append(el);}
  }
}
$('ai-enable').addEventListener('click',()=>perform('ai_control',{enabled:true}));
$('ai-disable').addEventListener('click',()=>perform('ai_control',{enabled:false}));
$('ai-ack-errors').addEventListener('click',()=>perform('ai_ack_errors',{events:(lastState?.ai?.sessions||[]).filter(r=>r.effective==='error'&&!r.stale).map(({source,session,event_id})=>({source,session,event_id}))}));
$('ai-connect-tab').addEventListener('click',()=>document.querySelector('[data-tab="lights"]').click());
$('aud-open-lights').addEventListener('click',()=>document.querySelector('[data-tab="lights"]').click());
$('ai-codex').addEventListener('change',()=>perform('ai_codex',{enabled:$('ai-codex').checked}));
document.querySelectorAll('[data-ai-preview]').forEach(b=>b.addEventListener('click',()=>perform('ai_preview',{state:b.dataset.aiPreview})));

const requested=location.hash.slice(1);
const initialTab=document.querySelector('[data-tab="'+(['lights','ai','audio'].includes(requested)?requested:'ai')+'"]');
initialTab.click();
let poller;
function queuePoll(){
  clearTimeout(poller);
  if(closed||document.hidden)return;
  poller=setTimeout(async()=>{await poll();queuePoll();},lastState?.busy||lastState?.lights?.busy?800:2300);
}
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&!closed)poll();queuePoll();});
refreshPorts();poll();counts();queuePoll();
