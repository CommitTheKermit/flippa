'use strict';
const $ = id => document.getElementById(id);
let csrf = '', state = {events:[]}, lastEvents = '', lastSessions = '', screenURL;
const selected = new Set();
function notice(message, error=false){$('notice').textContent=message;$('notice').classList.toggle('error',error);}
async function api(path, data){
  const response=await fetch('/api/'+path,data===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-Plipa-CSRF':csrf},body:JSON.stringify(data)});
  if(!response.ok){const result=await response.json();throw new Error(result.error||'요청 실패');}
  return path==='export'?response.blob():response.json();
}
async function task(fn){try{await fn();await refresh();}catch(e){notice(e.message,true);}}
function node(tag,content,className){const n=document.createElement(tag);if(content!==undefined)n.textContent=content;if(className)n.className=className;return n;}
async function refresh(){
  state=await api('state');csrf=state.csrf;
  const sessions=JSON.stringify(state.sessions);
  if(sessions!==lastSessions){lastSessions=sessions;const old=$('sessions').value;$('sessions').replaceChildren(new Option('기록 선택',''));for(const s of state.sessions)$('sessions').add(new Option(s.title+' · '+s.created.slice(0,10),s.id));$('sessions').value=old;}
  const events=JSON.stringify(state.events);
  if(events!==lastEvents){
    lastEvents=events;$('events').replaceChildren();
    if(state.events.length)$('messages').replaceChildren();
    for(const e of state.events){
      const row=node('div',undefined,'event'),check=document.createElement('input');check.type='checkbox';check.checked=selected.has(e.id);check.id='event-'+e.id;check.onchange=()=>check.checked?selected.add(e.id):selected.delete(e.id);
      const label=node('label');label.htmlFor=check.id;label.append(node('small',new Date(e.time).toLocaleTimeString('ko-KR')+' · '+e.id.slice(0,6)),node('strong',e.fact===true?'관측 기록':e.kind==='ai'?'AI 추정·제안':'사용자·시스템 기록'),node('span',e.message));
      if(e.attachments?.length)label.append(node('small',e.attachments.map(x=>x.split('-').slice(1).join('-')).join(', ')));
      if(e.attachments?.length){const links=node('span');for(const name of e.attachments){const b=node('button',name.endsWith('.png')?'화면 확인':name.endsWith('.txt')?'로그 확인':'구조 확인');b.type='button';b.onclick=event=>{event.preventDefault();task(async()=>{const url='/api/attachment?name='+encodeURIComponent(name);$('attachmentImage').hidden=!name.endsWith('.png');$('attachmentText').hidden=name.endsWith('.png');if(name.endsWith('.png'))$('attachmentImage').src=url;else{const r=await fetch(url);if(!r.ok)throw new Error('자료를 읽지 못했습니다.');$('attachmentText').textContent=await r.text();}$('attachment').showModal();});};links.append(b);}label.append(links);}
      if(e.missing?.length)label.append(node('small','수집 누락: '+e.missing.join(' / ')));
      row.append(check,label);$('events').append(row);
      if(['user','ai','error'].includes(e.kind)){const m=node('div',undefined,'message '+e.kind);m.append(node('small',e.kind==='user'?'나':e.kind==='ai'?'플리파 · AI 추정·제안':'실행 중단'),node('span',e.message));$('messages').append(m);}
    }
    $('count').textContent=state.events.length;$('events').scrollTop=$('events').scrollHeight;$('messages').scrollTop=$('messages').scrollHeight;
  }
  $('approval').hidden=!state.pending;
  if(state.pending){const a=state.pending.action;$('approvalText').textContent=a.message+'\n'+JSON.stringify({조작:a.kind,대상:a.target,입력:a.text});}
  $('send').disabled=!!(state.running||state.pending);$('approve').disabled=state.running;
  $('aiState').textContent=state.running?'화면을 확인하고 있습니다…':state.pending?'사용자 확인을 기다립니다.':'이미지·로그는 AI에 전송하지 않습니다.';
}
async function refreshDevices(){const result=await api('devices');const old=$('devices').value;$('devices').replaceChildren();for(const d of result.devices)$('devices').add(new Option(d.label+' · '+d.state,d.serial));if(!result.devices.length)$('devices').add(new Option('연결된 기기 없음',''));if(old)$('devices').value=old;$('avds').replaceChildren(...result.avds.map(a=>new Option(a,a)));}
async function screenLoop(){
  try{if(state.session&&!document.hidden){const r=await fetch('/api/screen');if(!r.ok)throw new Error('연결 확인 중');const blob=await r.blob();const old=screenURL;screenURL=URL.createObjectURL(blob);$('screen').src=screenURL;$('screen').hidden=false;$('screenEmpty').hidden=true;if(old)URL.revokeObjectURL(old);}}
  catch(e){$('screen').hidden=true;$('screenEmpty').hidden=false;$('screenEmpty').textContent='기기 연결을 확인하세요. 저장한 기록은 남아 있습니다.';}
  setTimeout(screenLoop,1000);
}
$('startForm').onsubmit=e=>{e.preventDefault();task(async()=>{await api('start',{serial:$('devices').value,package:$('package').value,title:$('title').value});selected.clear();notice('테스트를 시작했습니다. 앱을 열고 조작하세요.');});};
$('refresh').onclick=()=>task(refreshDevices);
$('load').onclick=()=>task(async()=>{await api('load',{id:$('sessions').value});selected.clear();notice('이전 기록을 열었습니다.');});
$('launch').onclick=()=>task(()=>api('launch',{}));
$('boot').onclick=()=>task(async()=>{await api('boot',{name:$('avds').value});notice('에뮬레이터를 시작했습니다. 부팅 후 연결 목록을 새로고침하세요.');$('help').close();});
$('capture').onclick=()=>task(async()=>{const b=$('capture');b.disabled=true;try{await api('capture',{screen:$('includeScreen').checked});notice('수집 결과를 타임라인에 남겼습니다. 누락 여부를 확인하세요.');}finally{b.disabled=false;}});
$('noteForm').onsubmit=e=>{e.preventDefault();task(async()=>{await api('note',{message:$('note').value});$('note').value='';});};
$('chatForm').onsubmit=e=>{e.preventDefault();task(async()=>{await api('chat',{goal:$('goal').value,consent:$('consent').checked});$('goal').value='';notice('AI 테스트를 시작했습니다.');});};
$('example').onclick=()=>{$('goal').value='현재 화면을 살펴보고 테스트할 항목을 알려줘';$('goal').focus();};
$('stop').onclick=()=>task(()=>api('stop',{}));
$('approve').onclick=()=>task(()=>api('approve',{id:state.pending?.id,approved:true}));
$('reject').onclick=()=>task(()=>api('approve',{id:state.pending?.id,approved:false}));
document.querySelectorAll('[data-key]').forEach(b=>b.onclick=()=>task(()=>api('manual',{kind:'key',text:b.dataset.key})));
$('inputForm').onsubmit=e=>{e.preventDefault();task(async()=>{await api('manual',{kind:'text',text:$('input').value});$('input').value='';});};
function coords(e){const img=$('screen'),r=img.getBoundingClientRect();return [Math.round((e.clientX-r.left)/r.width*img.naturalWidth),Math.round((e.clientY-r.top)/r.height*img.naturalHeight)];}
let down;
$('screen').onpointerdown=e=>{down=coords(e);$('screen').setPointerCapture(e.pointerId);};
$('screen').onpointercancel=()=>{down=null;};
$('screen').onpointerup=e=>{if(!down)return;const end=coords(e),start=down;down=null;const swipe=Math.hypot(end[0]-start[0],end[1]-start[1])>30;task(()=>api('manual',{kind:swipe?'swipe':'tap',coords:swipe?[...start,...end]:end}));};
$('screen').onkeydown=e=>{const key={Escape:'BACK',Enter:'ENTER',Backspace:'DEL'}[e.key];if(key){e.preventDefault();task(()=>api('manual',{kind:'key',text:key}));}};
$('helpOpen').onclick=()=>$('help').showModal();$('helpClose').onclick=()=>$('help').close();
$('attachmentClose').onclick=()=>$('attachment').close();
$('reportOpen').onclick=()=>{if(!selected.size){notice('타임라인에서 공유할 기록을 먼저 선택하세요.',true);return;}$('reportTitle').value=state.session?.title||'';$('steps').value=state.events.filter(e=>selected.has(e.id)).map((e,i)=>`${i+1}. ${e.message}`).join('\n');$('report').showModal();};
$('reportClose').onclick=()=>$('report').close();
$('exportForm').onsubmit=e=>{e.preventDefault();task(async()=>{const blob=await api('export',{ids:[...selected],title:$('reportTitle').value,steps:$('steps').value,expected:$('expected').value,actual:$('actual').value,ui:$('shareUi').checked,logs:$('shareLogs').checked,screens:$('shareScreens').checked});const url=URL.createObjectURL(blob),a=node('a');a.href=url;a.download='plipa-qa.zip';a.click();setTimeout(()=>URL.revokeObjectURL(url),10000);$('report').close();notice('선택한 기록을 내보냈습니다. 전달 전 ZIP 내용을 확인하세요.');});};
if(document.modelContext?.registerTool){Promise.resolve(document.modelContext.registerTool({name:'read_plipa_test_status',description:'Read the current Plipa session and event count. Does not expose QA evidence or execute device actions.',inputSchema:{type:'object',properties:{},additionalProperties:false},annotations:{readOnlyHint:true},execute:async input=>{if(!input||Object.keys(input).length)throw new Error('No arguments expected');await refresh();return {title:state.session?.title||null,running:state.running,awaitingApproval:!!state.pending,eventCount:state.events.length};}})).catch(()=>{});}
(async()=>{try{await refresh();await refreshDevices();}catch(e){notice(e.message,true);}screenLoop();async function poll(){try{await refresh();}catch(e){notice('플리파 서버에 연결되지 않았습니다. 실행 창을 확인하세요.',true);}setTimeout(poll,1500);}setTimeout(poll,1500);})();
