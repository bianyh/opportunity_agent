// Unit-level DOM/EventSource harness, not a substitute for browser acceptance.
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');
class Element {
  constructor(){this.children=[];this.textContent='';this.innerHTML='';this.value='';this.hidden=false;this.classList={toggle(){},add(){},remove(){}};}
  append(child){this.children.push(child);this.lastElementChild=child;child.parent=this;}
  remove(){if(this.parent){this.parent.children=this.parent.children.filter(child=>child!==this);this.parent.lastElementChild=this.parent.children.at(-1);}}
  querySelector(){return this.button||(this.button=new Element());}
  querySelectorAll(){return [];}
}
const elements = new Map(), streams=[], intervals=[];
let cleared = 0;
let mode='failed';
let completionCode='PASS',failureReport=null;
const document = {
  getElementById(id){if(!elements.has(id))elements.set(id,new Element());return elements.get(id);},
  createElement(){return new Element();}, querySelectorAll(){return [];}
};
class EventSource {
  constructor(url){this.url=url;this.handlers={};streams.push(this);}
  addEventListener(name,handler){this.handlers[name]=handler;}
  close(){this.closed=true;}
}
const sandbox = {document,EventSource,console,URL,encodeURIComponent,
  setInterval(callback){intervals.push(callback);return intervals.length;},clearInterval(){cleared++;},
  setTimeout(){return 1;},clearTimeout(){},
  async fetch(url){
    const body=url.includes('/runs/')?{id:'r1',status:mode,trace_id:'a'.repeat(32),error:'Router failed',answer:mode==='completed'?'最终回答':'',completion:{status:completionCode},failure_report:failureReport}:
      url.endsWith('/profile')?{payload:{},version:1}:[];
    return {ok:true,status:200,json:async()=>body};
  }
};
const source=fs.readFileSync(path.join(__dirname,'../opportunity_agent/v2/web/app.js'),'utf8')
  .replace('  loadInitial();','  globalThis.routerTest={followRun,state,ui,requestId};');
vm.createContext(sandbox);vm.runInContext(source,sandbox);
async function check(){
  const {followRun,state,ui,requestId}=sandbox.routerTest;
  // Insecure HTTP contexts expose getRandomValues without randomUUID.
  const webCrypto=require('crypto').webcrypto;
  sandbox.crypto={getRandomValues:webCrypto.getRandomValues.bind(webCrypto)};
  const httpIds=Array.from({length:500},()=>requestId());
  assert.equal(new Set(httpIds).size,httpIds.length);
  assert.ok(httpIds.every(id=>/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(id)));
  sandbox.crypto={randomUUID:()=> 'native-uuid'};
  assert.equal(requestId(),'native-uuid');
  delete sandbox.crypto;
  const legacyIds=Array.from({length:500},()=>requestId());
  assert.equal(new Set(legacyIds).size,legacyIds.length);
  assert.ok(legacyIds.every(id=>id.length>0&&id.length<=128));
  state.conversationId='c';state.running=true;
  let followed=followRun('r1','CMU MSCS','c');
  streams[0].handlers.trace_started({data:JSON.stringify({payload:{trace_id:'a'.repeat(32)}})});
  streams[0].handlers.run_failed({data:JSON.stringify({payload:{error:'Router failed'}})});
  await followed;
  assert.equal(document.getElementById('run-status').textContent,'执行失败');
  assert.equal(ui.steps.textContent,'执行失败');
  assert.ok(ui.messages.children[0].innerHTML.includes('Router failed'));
  assert.equal(ui.messages.lastElementChild.children[0].textContent,'重试这条消息');
  assert.ok(document.getElementById('run-info').innerHTML.includes('a'.repeat(32)));
  assert.equal(state.running,false);assert.equal(cleared,1);
  state.running=true;
  followed=followRun('r2','查询截止日期','c');
  await intervals.at(-1)(); // No SSE event arrives: terminal polling must recover.
  await followed;
  assert.equal(state.running,false);assert.equal(cleared,2);
  assert.equal(document.getElementById('run-status').textContent,'执行失败');
  state.running=true;mode='running';
  const count=ui.messages.children.length;
  followed=followRun('r3','查询项目','c');
  const third=streams.at(-1);
  third.onerror(); // A transient disconnect must not finish the run or close EventSource.
  assert.equal(state.running,true);assert.ok(!third.closed);
  third.handlers.research_progress({data:JSON.stringify({sequence:1,payload:{stage:'search',school:'CMU',program:'MSCS',current:1,total:5}})});
  assert.ok(ui.steps.textContent.includes('搜索官网 · CMU · MSCS · 项目 1/5'));
  completionCode='PARTIAL';failureReport={has_issues:true,status:'PARTIAL',summary:'部分完成，查询条件尚未满足',
    extraction_timeout_seconds:91.735,missing_fields:['deadline','gre_policy'],completion_reasons:['尚未得到证据'],
    unverified_candidates:[{title:'数学学院 <img src=x>',url:'https://math.gatech.edu/admissions',university:'GT',reason:'未确认属于目标项目'},
      {title:'恶意链接',url:'javascript:alert(1)',reason:'未确认'}],
    issues:[{message:'模型字段提取超时',count:3,schools:['Brown','<img src=x onerror=alert(1)>']}]};
  third.handlers.run_diagnostics({data:JSON.stringify({payload:failureReport})});
  assert.equal(document.getElementById('run-diagnostics').hidden,false);
  assert.ok(document.getElementById('run-diagnostics').innerHTML.includes('91.7 秒'));
  assert.ok(!document.getElementById('run-diagnostics').innerHTML.includes('<img'));
  assert.ok(document.getElementById('run-diagnostics').innerHTML.includes('可能不匹配（未计入合格结果）'));
  assert.ok(document.getElementById('run-diagnostics').innerHTML.includes('href="https://math.gatech.edu/admissions"'));
  assert.ok(!document.getElementById('run-diagnostics').innerHTML.includes('href="javascript:'));
  third.handlers.answer_snapshot({data:JSON.stringify({sequence:2,payload:{text:'第一段'}})});
  third.handlers.answer_snapshot({data:JSON.stringify({sequence:2,payload:{text:'重放旧事件'}})});
  assert.equal(ui.messages.children.length,count+1);
  assert.ok(ui.messages.lastElementChild.innerHTML.includes('第一段'));
  assert.ok(!ui.messages.lastElementChild.innerHTML.includes('重放旧事件'));
  third.handlers.answer_snapshot({data:JSON.stringify({sequence:3,payload:{text:'第一段，第二段'}})});
  mode='completed';third.handlers.run_completed({data:JSON.stringify({sequence:4,payload:{}})});
  await followed;
  assert.equal(document.getElementById('run-status').textContent,'部分完成');
  assert.equal(document.getElementById('run-diagnostics').hidden,false);
  assert.equal(ui.messages.children.length,count+1); // Final replaces draft, not another assistant message.
  assert.ok(ui.messages.lastElementChild.innerHTML.includes('最终回答'));
  state.running=true;mode='running';completionCode='PASS';failureReport=null;
  followed=followRun('r4','查询项目','c',{last_event_sequence:23,draft_answer:'恢复的草稿',progress:{stage:'extract',school:'UIUC'}});
  const fourth=streams.at(-1);
  assert.ok(fourth.url.endsWith('?after=23'));
  assert.ok(ui.steps.textContent.includes('UIUC'));
  fourth.handlers.research_progress({data:JSON.stringify({payload:{stage:'repair_adjust',school:'UIUC',
    tool:'search_official_pages',tools_used:4,tool_limit:6}})});
  assert.ok(ui.steps.textContent.includes('调整工具调用策略'));
  assert.ok(ui.steps.textContent.includes('工具 4/6'));
  fourth.handlers.answer_snapshot({data:JSON.stringify({sequence:22,payload:{text:'旧草稿'}})});
  assert.ok(ui.messages.lastElementChild.innerHTML.includes('恢复的草稿'));
  fourth.handlers.answer_reset({data:JSON.stringify({sequence:24,payload:{}})});
  assert.equal(ui.messages.children.length,count+1);
  failureReport={has_issues:false,status:'PASS',recovered_failures:2,
    tool_usage:{tools_used:4,tool_limit:6,decisions_used:1,decision_limit:2}};
  mode='completed';fourth.handlers.run_completed({data:JSON.stringify({sequence:25,payload:{}})});
  await followed;
  assert.equal(state.running,false);
  assert.ok(document.getElementById('run-diagnostics').innerHTML.includes('最终查询条件已满足'));
  console.log('frontend failure persistence, trace display and silent-SSE recovery passed');
}
const failTimer=setTimeout(()=>{console.error('frontend harness did not reach a terminal state',sandbox.routerTest.state,streams.length,intervals.length);process.exitCode=1;},2000);
check().catch(error=>{console.error(error);process.exitCode=1;}).finally(()=>clearTimeout(failTimer));
