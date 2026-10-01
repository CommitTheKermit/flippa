'use strict';
import {readFrames} from './stream.js';
const $ = id => document.getElementById(id);
let csrf = '', state = {events:[]}, lastEvents = '', lastSessions = '';
let lastMembers = '';
let streamControl, binding, gesture, inputBusy = false, inputs = [];
let deviceState = {phase:'idle',message:'기본 에뮬레이터를 확인하고 있습니다.'}, previewSerial = '';
let lastPreparationPhase = '';
const screen = $('screen');
const zoomSteps = [.75, 1, 1.25, 1.5, 1.75, 2];
let screenZoom = 1;
let screenBaseSize;
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
  $('identity').textContent=state.user+' · '+(state.role==='owner'?'소유자':'테스터');
  $('membersOpen').hidden=state.role!=='owner';
  $('boot').disabled=state.role!=='owner';
  const members=JSON.stringify(state.members);
  if(members!==lastMembers){lastMembers=members;$('memberList').replaceChildren();for(const member of state.members||[]){
    const row=node('div',undefined,'row member');
    row.append(node('span',member.id+' · '+(member.queue_position?'대기 '+member.queue_position+'번째':member.running?'실행 중':member.pending?'승인 대기':member.ai_allowed?'사용 가능':'사용 중지')));
    const button=node('button',member.ai_allowed?'사용 중지':'사용 허용');
    button.onclick=()=>task(()=>api('admin/ai',{user:member.id,allowed:!member.ai_allowed}));row.append(button);$('memberList').append(row);
  }}
  if(binding && binding.session !== state.session?.id) streamControl?.abort();
  if(state.running || state.pending) cancelTouch();
  updateDeviceControls();
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
  const aiBusy=!!(state.running||state.pending);
  $('send').disabled=!!(aiBusy||!state.ai_allowed||!state.session);$('approve').disabled=state.running;
  $('aiModel').disabled=aiBusy;$('aiReasoning').disabled=aiBusy;
  $('aiState').textContent=!state.ai_allowed?'소유자가 AI 사용을 중지했습니다.':state.queue_position?'다른 요청 종료 후 실행 · 대기 '+state.queue_position+'번째':state.running?'확인 중 · 남은 호출 '+state.steps_left+'회':state.pending?'사용자 확인을 기다립니다.':'요청당 최대 '+state.step_limit+'회 · 이미지 전송 없음';
}
function updateDeviceControls(){
  const interactive=!!state.session;
  $('launch').disabled=!interactive;
  document.querySelectorAll('[data-key]').forEach(button=>button.disabled=!interactive);
  $('input').disabled=!interactive;
  $('inputForm').querySelector('button').disabled=!interactive;
  $('screenHint').textContent=interactive?'실시간 화면 · 클릭·드래그·영문 키보드로 조작하세요. 한글 입력은 에뮬레이터 키보드를 사용하세요.':'읽기 전용 미리보기입니다. 테스트를 시작하면 조작과 기록이 활성화됩니다.';
}
function updateScreenSize(){
  if(!screen.width || !screen.height)return;
  const wrap=$('screenWrap');
  if(screenZoom===1){
    wrap.style.height='';
    screenBaseSize={width:Math.max(1,wrap.clientWidth-32),height:Math.max(1,wrap.clientHeight-32),wrapHeight:wrap.clientHeight};
  }else if(screenBaseSize){
    wrap.style.height=Math.ceil(screenBaseSize.wrapHeight*screenZoom)+'px';
  }else{
    screenBaseSize={width:Math.max(1,wrap.clientWidth-32),height:Math.max(1,wrap.clientHeight-32),wrapHeight:wrap.clientHeight};
    wrap.style.height=Math.ceil(screenBaseSize.wrapHeight*screenZoom)+'px';
  }
  const fit=Math.min(screenBaseSize.width/screen.width,screenBaseSize.height/screen.height,1);
  const scale=fit*screenZoom;
  screen.style.width=Math.round(screen.width*scale)+'px';
  screen.style.height=Math.round(screen.height*scale)+'px';
  $('zoomValue').value=Math.round(screenZoom*100)+'%';
  $('zoomValue').textContent=Math.round(screenZoom*100)+'%';
  $('zoomOut').disabled=screenZoom===zoomSteps[0];
  $('zoomIn').disabled=screenZoom===zoomSteps.at(-1);
}
function changeScreenZoom(direction){
  const index=zoomSteps.indexOf(screenZoom);
  screenZoom=zoomSteps[Math.max(0,Math.min(zoomSteps.length-1,index+direction))];
  updateScreenSize();
}
async function refreshDevices(){
  const result=await api('devices'),old=$('devices').value,previousPreview=previewSerial;
  deviceState=result.preparation||deviceState;
  if(!state.session && deviceState.phase!==lastPreparationPhase){
    lastPreparationPhase=deviceState.phase;
    notice(deviceState.message||'에뮬레이터 상태를 확인하고 있습니다.', ['error','unavailable'].includes(deviceState.phase));
  }
  $('devices').replaceChildren();
  for(const d of result.devices)$('devices').add(new Option(d.label+' · '+d.state,d.serial));
  if(!result.devices.length)$('devices').add(new Option('연결된 기기 없음',''));
  if(old && result.devices.some(d=>d.serial===old))$('devices').value=old;
  else if(deviceState.serial)$('devices').value=deviceState.serial;
  $('avds').replaceChildren(...result.avds.map(a=>new Option(a,a)));
  if(deviceState.default)$('avds').value=deviceState.default;
  previewSerial=state.session?'':deviceState.phase==='ready'?deviceState.serial:'';
  if(binding?.serial && (binding.serial!==previewSerial || state.session))streamControl?.abort();
  if(previousPreview!==previewSerial && !state.session)streamControl?.abort();
  $('deviceStatus').textContent=deviceState.message||'에뮬레이터 상태를 확인하고 있습니다.';
  $('prepare').hidden=state.role!=='owner'||!['error','unavailable'].includes(deviceState.phase);
}
async function screenLoop(){
  const target=state.session?{session:state.session.id}:previewSerial?{serial:previewSerial}:null;
  if(target && !document.hidden){
    const targetKey=target.session?'session:'+target.session:'preview:'+target.serial,viewer=crypto.randomUUID();
    streamControl=new AbortController();
    const control=streamControl;
    try{
      const response=await fetch('/api/stream',{method:'POST',signal:control.signal,
        headers:{'Content-Type':'application/json','X-Plipa-CSRF':csrf},body:JSON.stringify({...target,viewer})});
      if(!response.ok) throw new Error((await response.json()).error);
      const reader=response.body.getReader();
      try{
        for await (const png of readFrames(reader)){
          const bitmap=await createImageBitmap(new Blob([png],{type:'image/png'}));
          const currentKey=state.session?'session:'+state.session.id:previewSerial?'preview:'+previewSerial:'';
          if(control.signal.aborted || currentKey!==targetKey){bitmap.close();break;}
          screen.width=bitmap.width;screen.height=bitmap.height;
          screen.getContext('2d',{alpha:false}).drawImage(bitmap,0,0);bitmap.close();
          updateScreenSize();
          binding={...target,viewer,interactive:!!target.session};screen.hidden=false;$('screenEmpty').hidden=true;
        }
      }finally{await reader.cancel().catch(()=>{});reader.releaseLock();}
    }catch(e){
      if(e.name!=='AbortError') $('deviceStatus').textContent=e.message || '화면 연결을 확인하세요. 기록은 보존됩니다.';
    }finally{
      cancelTouch();binding=null;screen.hidden=true;$('screenEmpty').hidden=false;
      control.abort();
    }
  }
  setTimeout(screenLoop,500);
}
$('startForm').onsubmit=e=>{e.preventDefault();task(async()=>{await api('start',{serial:$('devices').value,package:$('package').value,title:$('title').value});selected.clear();notice('테스트를 시작했습니다. 앱을 열고 조작하세요.');});};
$('membersOpen').onclick=()=>$('members').showModal();
$('membersClose').onclick=()=>$('members').close();
$('debugRead').onclick=()=>task(async()=>{
  const result=await api('debug',{tool:$('debugTool').value});
  $('attachmentImage').hidden=true;$('attachmentText').hidden=false;
  $('attachmentText').textContent=typeof result.result==='string'?result.result:JSON.stringify(result.result,null,2);
  $('attachment').showModal();notice('조회 결과를 타임라인에 남겼습니다.');
});
$('refresh').onclick=()=>task(refreshDevices);
$('prepare').onclick=()=>task(async()=>{await api('prepare',{});await refreshDevices();notice('기본 에뮬레이터 준비를 다시 시작했습니다.');});
$('load').onclick=()=>task(async()=>{await api('load',{id:$('sessions').value});selected.clear();notice('이전 기록을 열었습니다.');});
$('launch').onclick=()=>task(()=>api('launch',{}));
$('zoomOut').onclick=()=>changeScreenZoom(-1);
$('zoomIn').onclick=()=>changeScreenZoom(1);
$('zoomReset').onclick=()=>{screenZoom=1;$('screenWrap').scrollTo({left:0,top:0});updateScreenSize();};
new ResizeObserver(updateScreenSize).observe($('screenWrap'));
$('boot').onclick=()=>task(async()=>{await api('boot',{name:$('avds').value});await refreshDevices();notice('선택한 에뮬레이터 준비를 시작했습니다.');$('help').close();});
$('capture').onclick=()=>task(async()=>{const b=$('capture');b.disabled=true;try{await api('capture',{screen:$('includeScreen').checked});notice('수집 결과를 타임라인에 남겼습니다. 누락 여부를 확인하세요.');}finally{b.disabled=false;}});
$('noteForm').onsubmit=e=>{e.preventDefault();task(async()=>{await api('note',{message:$('note').value});$('note').value='';});};
$('chatForm').onsubmit=e=>{e.preventDefault();task(async()=>{await api('chat',{goal:$('goal').value,consent:$('consent').checked,consent_logs:$('consentLogs').checked,model:$('aiModel').value,reasoning_effort:$('aiReasoning').value});$('goal').value='';notice('AI 테스트를 요청했습니다. 다른 요청이 실행 중이면 순서대로 시작합니다.');});};
$('example').onclick=()=>{$('goal').value='현재 화면을 살펴보고 테스트할 항목을 알려줘';$('goal').focus();};
$('stop').onclick=()=>task(()=>api('stop',{}));
$('approve').onclick=()=>task(()=>api('approve',{id:state.pending?.id,approved:true}));
$('reject').onclick=()=>task(()=>api('approve',{id:state.pending?.id,approved:false}));
const androidKeys={BACK:'GoBack',HOME:'GoHome',ENTER:'Enter',DEL:'Backspace'};
document.querySelectorAll('[data-key]').forEach(b=>b.onclick=()=>queueKey(androidKeys[b.dataset.key]));
$('inputForm').onsubmit=e=>{e.preventDefault();task(async()=>{await api('manual',{kind:'text',text:$('input').value});$('input').value='';});};
function coords(e){const r=screen.getBoundingClientRect();return {x:Math.max(0,Math.min(1,(e.clientX-r.left)/r.width)),y:Math.max(0,Math.min(1,(e.clientY-r.top)/r.height))};}
function enqueue(input){
  // Keep only the newest unsent move; preserve down/up ordering.
  if(input.phase==='move' && inputs.at(-1)?.phase==='move') inputs[inputs.length-1]=input;
  else inputs.push(input);
  pumpInputs();
}
async function pumpInputs(){
  if(inputBusy)return;
  inputBusy=true;
  try{while(inputs.length)await api('input',inputs.shift());}
  catch(e){inputs=[];cancelTouch();notice(e.message,true);}
  finally{inputBusy=false;if(inputs.length)pumpInputs();}
}
function cancelTouch(){
  if(!gesture)return;
  const old=gesture;gesture=null;
  inputs=inputs.filter(i=>i.phase!=='move');
  enqueue({...old.binding,phase:'cancel'});
}
function queueKey(key){
  if(!binding?.interactive || state.running || state.pending || gesture || inputs.length>=32)return;
  enqueue({...binding,phase:'key',key});
}
screen.onpointerdown=e=>{
  if(!binding?.interactive || gesture || state.running || state.pending || e.button!==0)return;
  e.preventDefault();screen.focus();screen.setPointerCapture(e.pointerId);
  gesture={id:e.pointerId,binding:{...binding},...coords(e)};
  enqueue({...binding,phase:'down',...coords(e)});
};
screen.onpointermove=e=>{
  if(gesture?.id!==e.pointerId)return;
  Object.assign(gesture,coords(e));enqueue({...gesture.binding,phase:'move',...coords(e)});
};
screen.onpointerup=e=>{
  if(gesture?.id!==e.pointerId)return;
  const old=gesture;gesture=null;enqueue({...old.binding,phase:'up',...coords(e)});
};
screen.onpointercancel=cancelTouch;
screen.onlostpointercapture=cancelTouch;
window.addEventListener('blur',cancelTouch);
document.addEventListener('visibilitychange',()=>{if(document.hidden){cancelTouch();streamControl?.abort();}});
window.addEventListener('pagehide',()=>{
  if(gesture)fetch('/api/input',{method:'POST',keepalive:true,headers:{'Content-Type':'application/json','X-Plipa-CSRF':csrf},body:JSON.stringify({...gesture.binding,phase:'cancel'})}).catch(()=>{});
  streamControl?.abort();
});
setInterval(()=>{if(gesture)enqueue({...gesture.binding,phase:'move',x:gesture.x,y:gesture.y});},1000);
screen.onkeydown=e=>{
  if(e.isComposing || e.metaKey || e.ctrlKey || e.altKey)return;
  const key=e.key==='Escape'?'GoBack':e.key;
  if((key.length===1 && key.charCodeAt(0)>=32 && key.charCodeAt(0)<=126) || ['GoBack','Enter','Backspace','ArrowUp','ArrowDown','ArrowLeft','ArrowRight'].includes(key)){
    e.preventDefault();queueKey(key);
  }
};
$('helpOpen').onclick=()=>$('help').showModal();$('helpClose').onclick=()=>$('help').close();
$('attachmentClose').onclick=()=>$('attachment').close();
$('reportOpen').onclick=()=>{if(!selected.size){notice('타임라인에서 공유할 기록을 먼저 선택하세요.',true);return;}$('reportTitle').value=state.session?.title||'';$('steps').value=state.events.filter(e=>selected.has(e.id)).map((e,i)=>`${i+1}. ${e.message}`).join('\n');$('report').showModal();};
$('reportClose').onclick=()=>$('report').close();
$('exportForm').onsubmit=e=>{e.preventDefault();task(async()=>{const blob=await api('export',{ids:[...selected],title:$('reportTitle').value,steps:$('steps').value,expected:$('expected').value,actual:$('actual').value,ui:$('shareUi').checked,logs:$('shareLogs').checked,screens:$('shareScreens').checked});const url=URL.createObjectURL(blob),a=node('a');a.href=url;a.download='plipa-qa.zip';a.click();setTimeout(()=>URL.revokeObjectURL(url),10000);$('report').close();notice('선택한 기록을 내보냈습니다. 전달 전 ZIP 내용을 확인하세요.');});};
if(document.modelContext?.registerTool){Promise.resolve(document.modelContext.registerTool({name:'read_plipa_test_status',description:'Read the current Plipa session and event count. Does not expose QA evidence or execute device actions.',inputSchema:{type:'object',properties:{},additionalProperties:false},annotations:{readOnlyHint:true},execute:async input=>{if(!input||Object.keys(input).length)throw new Error('No arguments expected');await refresh();return {title:state.session?.title||null,running:state.running,awaitingApproval:!!state.pending,eventCount:state.events.length};}})).catch(()=>{});}
(async()=>{try{await refresh();await refreshDevices();if(state.role==='owner'&&deviceState.phase==='idle'){await api('prepare',{});await refreshDevices();}}catch(e){notice(e.message,true);}screenLoop();async function poll(){try{await refresh();await refreshDevices();}catch(e){notice('플리파 서버에 연결되지 않았습니다. 실행 창을 확인하세요.',true);}setTimeout(poll,1500);}setTimeout(poll,1500);})();
