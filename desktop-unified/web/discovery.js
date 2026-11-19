'use strict';
(()=>{
 const by=id=>document.getElementById(id);
 const toggle=by('advanced-at-toggle');
 toggle.addEventListener('click',()=>{
   const on=document.body.classList.toggle('at-advanced');
   toggle.setAttribute('aria-expanded',String(on));
   toggle.textContent=on?'收起高级 AT':'高级 AT';
   if(on)document.querySelector('[data-tab=wifi]').click();
   else if(['wifi','firmware'].includes(document.body.dataset.view))document.querySelector('[data-tab=ai]').click();
 });

 const scanButtons=['discover-lan','discover-at-wifi','discover-c3-wifi'].map(by);
 let active=false, lampsFound=[];
 const entries={
   'discover-lan':{kind:'lamps',list:'discover-lan-choices',hint:'discover-lan-hint'},
   'discover-at-wifi':{kind:'wifi',list:'discover-at-choices',hint:'discover-at-hint'},
   'discover-c3-wifi':{kind:'wifi',list:'discover-c3-choices',hint:'discover-c3-hint'}
 };
 async function run(item){
   if(active)return;
   active=true;
   for(const b of scanButtons)b.disabled=true;
   const status=by(item.hint),list=by(item.list);
   list.hidden=true;list.replaceChildren();
   status.textContent=item.kind==='lamps'?'正在搜索本地灯板（8080）…':'正在读取附近 Wi‑Fi…';
   const until=Date.now()+28000;
   try{
     await api('discover',{kind:item.kind});
     let x;
     do{
       await new Promise(r=>setTimeout(r,650));x=await api('discover');
     }while(x.busy&&Date.now()<until);
     if(x.busy)throw new Error('搜索还未结束，请稍后再试。');
     if(x.error)throw new Error(x.error);
     if(item.kind==='lamps'){
       lampsFound=x.results;
       list.add(new Option('选择发现的灯板',''));
       x.results.forEach((v,i)=>list.add(new Option(v.kind+' · '+v.ip+':'+v.port+(v.active?'（当前连接）':''),String(i))));
     }else{
       list.add(new Option('选择 Wi‑Fi 名称',''));
       x.results.forEach(v=>list.add(new Option(v.ssid+(v.has_24?' · 2.4 GHz 可见':' · 未确认 2.4 GHz')+(v.signal?' · '+v.signal+'%':''),v.ssid)));
     }
     list.hidden=!x.results.length;
     status.textContent=x.results.length?'找到 '+x.results.length+' 个；请选择。':item.kind==='lamps'?'未发现灯板，可手动填写同网段 IP。':'没有读到附近网络，可手动输入名称。';
   }catch(e){status.textContent=e.message;}
   finally{active=false;for(const b of scanButtons)b.disabled=false;}
 }
 for(const btn of scanButtons)btn.addEventListener('click',()=>run(entries[btn.id]));
 for(const [choice,input] of [['discover-at-choices','ssid'],['discover-c3-choices','aud-ssid']]){
   by(choice).addEventListener('change',e=>{
     if(!e.target.value)return;
     by(input).value=e.target.value;
     by(input).dispatchEvent(new Event('input',{bubbles:true}));
   });
 }
 by('discover-lan-choices').addEventListener('change',e=>{
   if(e.target.value==='')return;
   const row=lampsFound[Number(e.target.value)];if(!row)return;
   const status=by('discover-lan-hint');
   if(lastState?.lights?.connected){
     status.textContent=row.active?'正在连接这块灯板。':'先断开当前灯控，再选择新设备。';return;
   }
   by('light-transport').value='tcp';
   by('light-host').value=row.ip;
   by('light-tcp-port').value=String(row.port);
   if(lastState)renderLights(lastState);
   status.textContent='地址已填入；点「连接灯控」确认。';
 });
})();
