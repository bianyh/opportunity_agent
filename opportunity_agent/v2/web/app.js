(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const ui = {auth:$('auth'), app:$('app'), messages:$('messages'), conversations:$('conversations'),
    profile:$('profile-view'), plan:$('plan-view'), approvals:$('approvals-view'), steps:$('run-steps')};
  const state = {user:null, conversations:[], conversationId:null, profile:null, plan:null,
    plans:[], approvals:[], preferences:[], conflicts:[], activeTab:'profile', running:false, authMode:'login'};
  let requestSequence = 0;
  function requestId() {
    const webCrypto = globalThis.crypto;
    if (typeof webCrypto?.randomUUID === 'function') return webCrypto.randomUUID();
    // HTTP on a LAN address lacks randomUUID, but still permits getRandomValues.
    if (typeof webCrypto?.getRandomValues === 'function') {
      const bytes = webCrypto.getRandomValues(new Uint8Array(16));
      bytes[6] = (bytes[6] & 0x0f) | 0x40;
      bytes[8] = (bytes[8] & 0x3f) | 0x80;
      const hex = Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
      return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;
    }
    // These IDs deduplicate requests; they are not authentication credentials.
    return `request-${Date.now().toString(36)}-${(++requestSequence).toString(36)}-${Math.random().toString(36).slice(2)}`;
  }
  const fields = [
    ['school','本科学校'],['major','专业'],['academic_year','年级','number'],
    ['graduation_year','毕业年份','number'],['target_degree','目标学位'],
    ['target_countries','目标国家（逗号分隔）','list'],['target_fields','目标方向（逗号分隔）','list'],
    ['target_schools','目标学校（逗号分隔）','list'],['target_programs','目标项目（逗号分隔）','list'],
    ['toefl_score','TOEFL','number'],['ielts_score','IELTS','number'],['gre_score','GRE','number'],
    ['internship_experiences','实习经历（逗号分隔）','list'],
    ['research_experiences','科研经历（逗号分隔）','list'],
    ['paper_experiences','论文经历（逗号分隔）','list'],
    ['project_experiences','项目经历（逗号分隔）','list'],
    ['career_goal','职业目标']
  ];
  const fieldLabels=Object.fromEntries(fields.map(([key,label])=>[key,label.replace('（逗号分隔）','')]));
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const plain = value => Array.isArray(value) ? value.join('、') : typeof value === 'object' && value !== null ? JSON.stringify(value) : String(value ?? '');
  function markdown(text) {
    // The answer is model output.  Escape it first, then recognise only this
    // small Markdown subset; arbitrary HTML is deliberately never accepted.
    const source=String(text??'').replace(/\r\n?/g,'\n');
    const inline=value=>{
      const code=[];
      let safe=escape(value).replace(/`([^`\n]+)`/g,(_,snippet)=>{
        const token=`\u0000CODE${code.length}\u0000`; code.push(`<code>${snippet}</code>`); return token;
      });
      safe=safe.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,(_,label,url)=>
        `<a href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`);
      safe=safe.replace(/(\*\*|__)(.+?)\1/g,'<strong>$2</strong>')
        .replace(/~~(.+?)~~/g,'<del>$1</del>')
        .replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g,'$1<em>$2</em>');
      return safe.replace(/\u0000CODE(\d+)\u0000/g,(_,index)=>code[Number(index)]);
    };
    const isTableDivider=line=>/^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(line);
    const cells=line=>line.trim().replace(/^\||\|$/g,'').split('|').map(cell=>cell.trim());
    const lines=source.split('\n'), blocks=[];
    for(let i=0;i<lines.length;){
      const line=lines[i];
      if(/^```/.test(line)){
        const language=line.slice(3).trim(), code=[]; i+=1;
        while(i<lines.length&&!/^```\s*$/.test(lines[i])) code.push(lines[i++]);
        if(i<lines.length) i+=1;
        blocks.push(`<pre><code${language?` class="language-${escape(language)}"`:''}>${escape(code.join('\n'))}</code></pre>`); continue;
      }
      if(line.includes('|')&&i+1<lines.length&&isTableDivider(lines[i+1])){
        const headers=cells(line), rows=[]; i+=2;
        while(i<lines.length&&lines[i].trim()&&lines[i].includes('|')) rows.push(cells(lines[i++]));
        const head=headers.map(cell=>`<th>${inline(cell)}</th>`).join('');
        const body=rows.map(row=>`<tr>${headers.map((_,index)=>`<td>${inline(row[index]||'')}</td>`).join('')}</tr>`).join('');
        blocks.push(`<div class="markdown-table-wrap"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`); continue;
      }
      const heading=line.match(/^(#{1,3})\s+(.+)$/);
      if(heading){const level=heading[1].length; blocks.push(`<h${level}>${inline(heading[2])}</h${level}>`);i+=1;continue;}
      if(/^\s*(?:[-*_])\1\1+\s*$/.test(line)){blocks.push('<hr>');i+=1;continue;}
      const list=line.match(/^\s*([-*+] |\d+\. )(.+)$/);
      if(list){
        const ordered=/^\d+\. /.test(list[1]), items=[];
        while(i<lines.length){const item=lines[i].match(/^\s*(?:[-*+] |\d+\. )(.+)$/);if(!item)break;items.push(`<li>${inline(item[1])}</li>`);i+=1;}
        blocks.push(`<${ordered?'ol':'ul'}>${items.join('')}</${ordered?'ol':'ul'}>`);continue;
      }
      if(/^>\s?/.test(line)){
        const quote=[];while(i<lines.length&&/^>\s?/.test(lines[i]))quote.push(lines[i++].replace(/^>\s?/,''));
        blocks.push(`<blockquote>${inline(quote.join('\n')).replace(/\n/g,'<br>')}</blockquote>`);continue;
      }
      if(!line.trim()){i+=1;continue;}
      const paragraph=[];while(i<lines.length&&lines[i].trim()&&!/^```|^(#{1,3})\s+|^\s*(?:[-*+] |\d+\. )|^>\s?/.test(lines[i])&&!(lines[i].includes('|')&&i+1<lines.length&&isTableDivider(lines[i+1])))paragraph.push(lines[i++]);
      blocks.push(`<p>${inline(paragraph.join('\n')).replace(/\n/g,'<br>')}</p>`);
    }
    return `<div class="markdown-body">${blocks.join('')}</div>`;
  }
  function toast(message) { const el=$('toast'); el.textContent=message; el.hidden=false; clearTimeout(toast.timer); toast.timer=setTimeout(()=>el.hidden=true,4500); }
  async function request(path, options={}, retry=true) {
    const headers = {'Accept':'application/json',...(options.body ? {'Content-Type':'application/json'} : {}),...options.headers};
    const response = await fetch('/api/v1'+path,{credentials:'same-origin',...options,headers});
    if (response.status===401 && retry && !path.startsWith('/auth/')) {
      const refreshed=await fetch('/api/v1/auth/refresh',{method:'POST',credentials:'same-origin'});
      if (refreshed.ok) return request(path,options,false);
      showAuth(); throw Error('登录已过期，请重新登录');
    }
    if (!response.ok) { const data=await response.json().catch(()=>({})); throw Error(data.detail||`请求失败 (${response.status})`); }
    return response.status===204 ? null : response.json();
  }
  function showAuth() { ui.app.hidden=true; ui.auth.hidden=false; state.user=null; }
  function showApp() { ui.auth.hidden=true; ui.app.hidden=false; $('account-email').textContent=state.user.email; }
  async function loadInitial() {
    try { state.user=await request('/auth/me'); showApp(); await Promise.all([loadConversations(),loadProfile(),loadPlan(),loadApprovals(),loadMemory(),loadConflicts(true)]); }
    catch (error) { if (!state.user) showAuth(); else toast(error.message); }
  }
  async function loadConversations() {
    state.conversations=await request('/conversations'); renderConversations();
    if (!state.conversationId && state.conversations.length) await openConversation(state.conversations[0].id);
  }
  function renderConversations() {
    ui.conversations.innerHTML=state.conversations.map(item=>`<button class="conversation ${item.id===state.conversationId?'active':''}" data-conversation="${escape(item.id)}" title="${escape(item.title)}">${escape(item.title)}</button>`).join('');
  }
  async function openConversation(id) {
    if(state.activeFollow)state.activeFollow.cancel();
    state.conversationId=id; renderConversations(); ui.messages.innerHTML=''; ui.steps.textContent=''; $('run-info').textContent='';
    showRunDiagnostics(null,null);
    const item=await request('/conversations/'+encodeURIComponent(id));
    if(state.conversationId!==id)return;
    $('conversation-title').textContent=item.title;
    item.messages.forEach(row=>{
      appendMessage(row.role,row.content);
      if(row.role==='user'&&row.run_id){
        const info=document.createElement('div');info.className='hint';
        info.innerHTML=`Run：<a href="/api/v1/runs/${encodeURIComponent(row.run_id)}" target="_blank" rel="noopener">${escape(row.run_id)}</a> · ${escape(row.run_status||'')}`;
        ui.messages.lastElementChild.append(info);
        const inspect=document.createElement('button');inspect.type='button';inspect.textContent='执行诊断';
        inspect.onclick=async()=>{try{const run=await request('/runs/'+encodeURIComponent(row.run_id));
          if(state.conversationId===id){showRunInfo(row.run_id,run.trace_id);showRunDiagnostics(row.run_id,run.failure_report);}}
          catch(error){toast(error.message);}};
        info.append(inspect);
        showRunInfo(row.run_id,row.trace_id);
      }
      if(row.role==='user'&&row.run_status==='failed')showRunFailure(row.run_id,row.run_error,row.content);
    });
    const active=[...item.messages].reverse().find(row=>row.role==='user'&&['queued','running'].includes(row.run_status));
    if(active&&state.conversationId===id){
      const run=await request('/runs/'+encodeURIComponent(active.run_id));
      if(state.conversationId!==id)return;
      state.running=true;$('chat-form').querySelector('button').disabled=true;
      followRun(active.run_id,active.content,id,run).catch(error=>toast(error.message));
    } else if(!active){
      const last=[...item.messages].reverse().find(row=>row.role==='user'&&row.run_id);
      if(last){try{const run=await request('/runs/'+encodeURIComponent(last.run_id));
        if(state.conversationId===id){showRunDiagnostics(last.run_id,run.failure_report);
          $('run-status').textContent=runLabel(run);}}
        catch(error){ /* Historical answers remain readable when diagnostics are unavailable. */ }}
    }
  }
  function runLabel(run){return run.status==='failed'?'执行失败':
    ({PARTIAL:'部分完成',NEED_USER:'需要补充信息',FAIL:'任务未完成'}[run.completion?.status]||
      (run.status==='completed'?'已完成':'正在处理'));}
  function showRunDiagnostics(id,report){
    const target=$('run-diagnostics');
    if(!report?.has_issues&&!report?.recovered_failures&&!report?.unverified_candidates?.length){target.hidden=true;target.innerHTML='';return;}
    const issues=(report.issues||[]).map(item=>`<li>${escape(item.message)} · ${Number(item.count)||0} 次`+
      (item.schools?.length?' · '+escape(item.schools.join('、')):'')+'</li>').join('');
    const reasons=(report.completion_reasons||[]).map(reason=>`<li>${escape(reason)}</li>`).join('');
    const candidates=(report.unverified_candidates||[]).map(item=>{
      let link=escape(item.title||item.url||'候选页面');
      try { const url=new URL(item.url); if(url.protocol==='https:'&&!url.username&&!url.password)
        link=`<a href="${escape(url.href)}" target="_blank" rel="noopener noreferrer">${link}</a>`;
      } catch (_) {}
      return `<li>${link} · ${escape(item.university||'')} · 可能不匹配（未计入合格结果）`+
        ` · ${escape(item.reason||'项目身份未确认')}</li>`;
    }).join('');
    target.innerHTML=`<details open><summary>${escape(report.summary||(report.recovered_failures?'查询已完成，工具异常已处理':'执行诊断'))}</summary><ul>${issues}${reasons}</ul>`+
      (report.recovered_failures?`<p>过程中发生 ${Number(report.recovered_failures)||0} 次工具异常，最终查询条件已满足。</p>`:'')+
      (report.tool_usage?.tool_limit?`<p>工具调用：${Number(report.tool_usage.tools_used)||0}/${Number(report.tool_usage.tool_limit)||0}；修复决策：${Number(report.tool_usage.decisions_used)||0}/${Number(report.tool_usage.decision_limit)||0}</p>`:'')+
      (report.extraction_timeout_seconds?`<p>模型提取超时累计：${Number(report.extraction_timeout_seconds).toFixed(1)} 秒</p>`:'')+
      (report.missing_fields?.length?`<p>仍缺少：${escape(report.missing_fields.map(x=>({deadline:'申请截止日期',gre_policy:'GRE 政策'}[x]||x)).join('、'))}</p>`:'')+
      (candidates?`<h4>未确认匹配的搜索结果</h4><ul>${candidates}</ul><p>这些页面仅供核对，不作为答案证据或合格项目。</p>`:'')+
      `<small>Run：${escape(id||'')}</small></details>`;
    target.hidden=false;
  }
  function showRunInfo(id,traceId) {
    $('run-info').innerHTML=`Run：<a href="/api/v1/runs/${encodeURIComponent(id)}" target="_blank" rel="noopener">${escape(id)}</a>`+
      (traceId?` · Trace：${escape(traceId)}`:'');
  }
  function showRunFailure(id,error,message) {
    appendMessage('assistant',`本次请求执行失败：${error||'服务暂时不可用'}\n\nRun ID：${id}。未生成完整回答。`);
    const button=document.createElement('button');button.type='button';button.textContent='重试这条消息';
    button.onclick=()=>{if(!state.running)sendMessage(message);};
    ui.messages.lastElementChild.append(button);
  }
  function appendMessage(role,content) {
    const article=document.createElement('article'); article.className='message '+(role==='user'?'user':'assistant');
    const rendered=role==='assistant'?markdown(content):`<div class="plain-text">${escape(content)}</div>`;
    article.innerHTML=`<div class="meta">${role==='user'?'你':'助手'}</div>${rendered}`;
    ui.messages.append(article); ui.messages.scrollTop=ui.messages.scrollHeight;
    return article;
  }
  async function createConversation() {
    const item=await request('/conversations',{method:'POST',body:JSON.stringify({title:'新对话'})});
    state.conversations.unshift(item); await openConversation(item.id);
  }
  async function sendMessage(message) {
    if (state.running) return;
    if (!state.conversationId) await createConversation();
    state.running=true; $('chat-form').querySelector('button').disabled=true; $('run-status').textContent='正在处理';
    appendMessage('user',message); ui.steps.textContent='开始分析…';
    showRunDiagnostics(null,null);
    const conversationId=state.conversationId;
    try {
      const run=await request('/conversations/'+encodeURIComponent(conversationId)+'/runs',{
        method:'POST',body:JSON.stringify({message,request_id:requestId()})});
      if(state.conversationId!==conversationId){finishRun();return;}
      showRunInfo(run.run_id);
      await followRun(run.run_id,message,conversationId);
    } catch(error) { toast(error.message); finishRun(); }
  }
  function finishRun(status='准备就绪') { state.running=false; $('chat-form').querySelector('button').disabled=false; $('run-status').textContent=status; }
  function followRun(id,message,conversationId,initial=null) {
    return new Promise(resolve=>{
      const cursor=Number(initial?.last_event_sequence||0);
      const stream=new EventSource('/api/v1/runs/'+encodeURIComponent(id)+'/events?after='+cursor);
      let done=false;
      let lastSequence=cursor, draft=null, stage='正在处理', lastPoll=0;
      const started=initial?.created_at?Date.parse(initial.created_at):Date.now();
      let terminalStatus='准备就绪';
      let polling=false;
      const owner={cancel(){if(done)return;done=true;stream.close();clearInterval(watchdog);
        if(state.activeFollow===owner){state.activeFollow=null;finishRun();}resolve();}};
      state.activeFollow=owner;
      const renderProgress=()=>{
        if(state.conversationId!==conversationId)return;
        const elapsed=Math.max(0,Math.floor((Date.now()-started)/1000));
        ui.steps.textContent=`${stage} · 已用时 ${Math.floor(elapsed/60)}分${elapsed%60}秒`;
      };
      const researchStage=data=>{
        const names={parse:'解析查询条件',sql:'查询本地项目库',search:'搜索官网',read:'读取官网页面',
          read_fallback:'尝试备用读取',extract:'提取并核验 GRE / 截止日期',school_done:'该项目处理结束',
          extraction_unavailable:'模型提取多次失败，停止重复等待并整理已有结果',
          repair_analyze:'分析工具失败原因',repair_adjust:'调整工具调用策略',repair_validate:'校验官网链接',
          repair_budget_exhausted:'修复预算耗尽，整理已有结果'};
        return (names[data.stage]||'检索中')+(data.school?' · '+data.school:'')+
          (data.program?' · '+data.program:'')+(data.total?` · 项目 ${data.current||0}/${data.total}`:'')+
          (data.tool?' · '+data.tool:'')+(data.tool_limit?` · 工具 ${data.tools_used||0}/${data.tool_limit}`:'')+
          (data.error_code?' · '+data.error_code:'');
      };
      const restoredStage=data=>data.stage?researchStage(data):
        ({run_preparing:'读取会话与相关记忆',goal_parse_started:'解析目标与成功标准',routing_started:'选择 Agent',
          agent_started:'Agent 执行中',completion_checked:'检查结果',repair_round_started:'补查',
          synthesis_started:'正在组织语言'}[data.event_type]||'正在处理');
      const renderDraft=text=>{
        if(state.conversationId!==conversationId)return;
        if(!text){if(draft){draft.remove();draft=null;}return;}
        if(!draft)draft=appendMessage('assistant','');
        draft.innerHTML=`<div class="meta">助手 · 正在生成（尚未完成引用校验）</div><div class="plain-text">${escape(text)}</div>`;
        ui.messages.scrollTop=ui.messages.scrollHeight;
      };
      if(initial?.draft_answer)renderDraft(initial.draft_answer);
      showRunDiagnostics(id,initial?.failure_report);
      if(initial?.progress)stage=restoredStage(initial.progress);
      renderProgress();
      const watchdog=setInterval(async()=>{
        if(done||polling)return;
        renderProgress();
        if(Date.now()-lastPoll<5000)return;
        lastPoll=Date.now();
        polling=true;
        try {const run=await request('/runs/'+encodeURIComponent(id));
          if(state.conversationId===conversationId)showRunInfo(id,run.trace_id);
          if(state.conversationId===conversationId&&run.failure_report?.has_issues)showRunDiagnostics(id,run.failure_report);
          if(run.status==='completed'||run.status==='failed')await complete();
          else if(stream.readyState!==1){
            if(run.progress)stage=restoredStage(run.progress)+' · 连接恢复中';
            if(run.draft_answer)renderDraft(run.draft_answer);
            renderProgress();
          }
        } catch(error) { /* SSE or the next poll can recover a transient failure. */ }
        finally{polling=false;}
      },1000);
      const complete=async()=>{
        if(done)return; done=true; stream.close();clearInterval(watchdog);
        try {
          let run;
          for(let attempt=0;attempt<180;attempt++){
            run=await request('/runs/'+encodeURIComponent(id));
            if(run.status==='completed'||run.status==='failed')break;
            await new Promise(resolve=>setTimeout(resolve,1000));
          }
          if(run.status!=='completed'&&run.status!=='failed')throw Error('处理时间过长，请稍后刷新对话查看结果');
          terminalStatus=runLabel(run);
          if(state.conversationId===conversationId){
            showRunInfo(id,run.trace_id);ui.steps.textContent=terminalStatus;
            showRunDiagnostics(id,run.failure_report);
            if(run.status==='failed'){renderDraft('');showRunFailure(id,run.error,message);}
            else if(run.answer){
              if(draft)draft.innerHTML=`<div class="meta">助手</div>${markdown(run.answer)}`;
              else appendMessage('assistant',run.answer);
            }
          }
          await Promise.allSettled([loadProfile(),loadPlan(),loadApprovals(),loadMemory(),loadConflicts(true)]);
        } catch(error) { terminalStatus='状态获取失败';toast(error.message);ui.steps.textContent=terminalStatus; }
        if(state.activeFollow===owner){state.activeFollow=null;finishRun(terminalStatus);}resolve();
      };
      const labels={run_preparing:'读取会话与相关记忆',run_started:'开始',goal_parse_started:'解析目标与成功标准',
        goal_parsed:'目标解析完成',routing_started:'选择 Agent',route_selected:'路由完成',agent_started:'Agent 执行中',
        agent_completed:'Agent 完成',completion_checked:'检查结果',repair_round_started:'补查',
        approval_required:'等待确认',profile_conflict:'画像信息待核对',memory_updated:'偏好已保存',
        synthesis_started:'正在组织语言',synthesizer_fallback:'正在生成已有结果摘要',final_answer:'回答已生成'};
      for(const name of [...Object.keys(labels),'run_diagnostics','research_progress','answer_snapshot','answer_reset','trace_started','run_completed','run_failed']) stream.addEventListener(name,event=>{
        if(done)return;
        let envelope;try{envelope=JSON.parse(event.data||'{}');}catch{return;}
        const sequence=Number(envelope.sequence||event.lastEventId||0);
        if(sequence&&sequence<=lastSequence)return;
        if(sequence)lastSequence=sequence;
        const data=envelope.payload||{};
        if(name==='trace_started'){if(state.conversationId===conversationId)showRunInfo(id,data.trace_id);return;}
        if(name==='run_completed'||name==='run_failed') { complete(); return; }
        if(state.conversationId!==conversationId)return;
        if(name==='run_diagnostics'){showRunDiagnostics(id,data);return;}
        if(name==='answer_snapshot'){renderDraft(data.text||'');return;}
        if(name==='answer_reset'){renderDraft('');return;}
        stage=name==='research_progress'?researchStage(data):
          (labels[name]||name)+(data.agent?' · '+data.agent:'')+
          (name==='repair_round_started'?` · 第 ${Number(data.round_id||0)+1} 轮`:'')+
          (name==='completion_checked'&&data.status?' · '+data.status:'');
        renderProgress();
      });
      stream.onerror=()=>{if(done)return;stage='进度连接暂时中断，正在自动重连';renderProgress();};
    });
  }
  async function loadProfile() { state.profile=await request('/profile'); renderProfile(); }
  function renderProfile() {
    const values=state.profile?.payload||{};
    const filled=fields.filter(([key])=>values[key]!==undefined && values[key]!==null && values[key]!=='' && (!Array.isArray(values[key])||values[key].length));
    ui.profile.innerHTML=`<h2>我的画像</h2><div class="card"><div class="meta">版本 ${state.profile?.version||1}</div>${filled.map(([key,label])=>`<div class="kv"><span>${escape(label)}</span><span>${escape(plain(values[key]))}</span></div>`).join('')||'<p class="empty">还没有画像信息，可以编辑资料或在聊天中告诉我。</p>'}<button id="edit-profile">编辑画像</button></div><div class="card"><h3>当前状态</h3>${Object.entries(state.profile?.state||{}).map(([key,value])=>`<div class="kv"><span>${escape(key)}</span><span>${escape(value)}</span></div>`).join('')}</div>`;
  }
  function editProfile() {
    const values=state.profile?.payload||{};
    $('profile-fields').innerHTML=fields.map(([key,label,type])=>`<label>${escape(label)}<input name="${key}" type="${type==='number'?'number':'text'}" value="${escape(type==='list'?(values[key]||[]).join(', '):values[key]??'')}"></label>`).join('');
    $('profile-dialog').showModal();
  }
  async function saveProfile(event) {
    event.preventDefault(); const payload={...(state.profile?.payload||{})};
    for(const [key,,type] of fields){const raw=String($('profile-form').elements[key].value).trim();
      payload[key]=!raw?null:type==='list'?raw.split(/[,，、;；]/).map(x=>x.trim()).filter(Boolean):type==='number'?Number(raw):raw;}
    for(const [key,,type] of fields) if(type==='list') payload[key] ||= [];
    payload.onboarding_completed=true;
    try {const result=await request('/profile',{method:'PATCH',body:JSON.stringify({payload,expected_version:state.profile.version,request_id:requestId()})});
      $('profile-dialog').close(); toast(result.status==='no_change'?'画像没有变化':'画像变更已提交，请确认后生效');
      if(result.approval_id){await loadApprovals();switchTab('approvals');}}
    catch(error){toast(error.message); if(error.message.includes('changed')) await loadProfile();}
  }
  async function loadPlan() {
    state.plans=await request('/plans');
    state.plan=await request('/plans/current').catch(error=>error.message==='no current plan'?null:Promise.reject(error));
    renderPlan();
  }
  function renderPlan() {
    const current=state.plan;
    if(!current){ui.plan.innerHTML='<h2>申请计划</h2><div class="empty">还没有已确认的计划。可以在聊天中说“请根据我的背景生成申请规划”。</div><button id="request-plan" class="primary">生成计划</button>';return;}
    const roadmap=current.roadmap||{}, phases=roadmap.timeline?.phases||[];
    const taskByKey=new Map((current.tasks||[]).map(item=>[item.stable_key,item]));
    ui.plan.innerHTML=`<h2>申请计划 <span class="badge">v${current.version}</span></h2><div class="button-row"><button id="request-plan">重新规划</button><select id="plan-version" aria-label="查看计划版本">${state.plans.map(item=>`<option value="${escape(item.id)}" ${item.id===current.id?'selected':''}>v${item.version} · ${escape(item.status)}</option>`).join('')}</select></div>
      <div class="card"><h3>${escape(roadmap.goal||'申请规划')}</h3><div class="meta">${escape(current.revision_reason||'')}</div>${(roadmap.unresolved_requirements||[]).map(item=>`<p class="hint">待核验：${escape(item)}</p>`).join('')}</div>
      <div class="timeline">${phases.map(phase=>`<div class="phase"><h3>${escape(phase.title)}</h3><div class="meta">${escape(phase.start_date)} 至 ${escape(phase.end_date)}</div><p class="hint">${escape(phase.plan?.summary||'')}</p>${(phase.plan?.tasks||[]).map(task=>taskCard(taskByKey.get(task.progress_key),task)).join('')}</div>`).join('')}</div>
      <details class="card"><summary>关键日期与事件</summary>${(roadmap.timeline?.events||[]).filter(event=>!['winter_break','summer_break'].includes(event.kind)).map(event=>taskCard(taskByKey.get(event.progress_key),{title:event.title,category:event.kind,due_date:event.event_date,reason:event.detail,execution_status:event.execution_status})).join('')}</details>
      <details class="card"><summary>查看详细路线图</summary><div class="article">${markdown(roadmap.article||'')}</div></details>`;
  }
  function taskCard(saved,task) {
    const status=saved?.status||task.execution_status||'planned';
    return `<div class="task"><strong>${escape(task.title)}</strong> <span class="badge ${escape(status)}">${escape(status)}</span><div class="meta">${escape(saved?.due_at?.slice(0,10)||task.due_date||'日期待定')} · ${escape(task.category||'')}</div><div class="hint">${escape(task.reason||'')}</div>${saved&&state.plan.status==='active'?`<div class="task-actions">${[['start','开始'],['complete','完成'],['postpone','延期'],['cancel','取消'],['reset','重置']].map(([action,label])=>`<button data-task="${escape(saved.id)}" data-action="${action}">${label}</button>`).join('')}</div>`:''}</div>`;
  }
  async function taskAction(id,action) {
    let evidence=''; let due_at=null;
    if(action==='postpone'){const date=prompt('新的日期（YYYY-MM-DD）');if(!date)return;if(!/^\d{4}-\d{2}-\d{2}$/.test(date)){toast('请输入完整日期');return;}due_at=date+'T00:00:00';}
    if(action==='complete') evidence=prompt('完成依据（可选）')||'';
    try {const result=await request('/tasks/'+encodeURIComponent(id)+'/commands',{method:'POST',body:JSON.stringify({action,evidence,due_at,request_id:requestId()})});
      toast('已生成待确认任务变更'); await loadApprovals(); switchTab('approvals'); return result;}
    catch(error){toast(error.message);}
  }
  async function loadApprovals(){state.approvals=await request('/approvals');$('approval-count').textContent=state.approvals.length||'';renderApprovals();}
  async function loadMemory(){
    state.preferences=await request('/memory/preferences');
    const labels={avoid_gre:'避开 GRE 要求',employment_priority:'就业优先',fallback_country:'备选国家',budget_preference:'预算偏好'};
    $('memory-view').innerHTML='<h2>已保存偏好</h2><p class="hint">明确偏好直接保存；推断偏好需确认。撤销后不再用于后续查询。</p>'+state.preferences.map(p=>`<div class="card"><h3>${escape(labels[p.key]||p.key)}</h3><p>${escape(typeof p.value==='boolean'?(p.value?'是':'否'):String(p.value))}</p><p class="hint">${escape(p.evidence||'')} · 版本 ${escape(p.version)}</p><button data-revoke-preference="${escape(p.key)}" data-version="${escape(p.version)}">撤销</button></div>`).join('');
  }
  async function loadConflicts(open=false){
    state.conflicts=await request('/profile/conflicts');
    $('conflict-items').innerHTML=state.conflicts.map(item=>`<article class="card"><h3>${escape(fieldLabels[item.field]||item.field)}</h3><p>旧信息：${escape(plain(item.old_value)||'未填写')}</p><p>新信息：${escape(plain(item.new_value))}</p><blockquote>${escape(item.new_evidence)}</blockquote><div class="button-row"><button class="primary" data-conflict="${escape(item.conflict_id)}" data-choice="new">使用新信息</button><button data-conflict="${escape(item.conflict_id)}" data-choice="old">保留旧信息</button></div></article>`).join('');
    if(open&&state.conflicts.length&&!$('conflict-dialog').open)$('conflict-dialog').showModal();
    if(!state.conflicts.length&&$('conflict-dialog').open)$('conflict-dialog').close();
  }
  function renderApprovals(){ui.approvals.innerHTML=`<h2>待确认</h2>${state.approvals.length?state.approvals.map(item=>`<div class="card approval-card"><h3>${escape(item.proposal_type)}</h3><p class="hint">${escape(item.reason||'请审阅变更')}</p>${approvalPreview(item)}<div class="button-row"><button class="primary" data-approval="${escape(item.id)}" data-decision="accept">确认</button><button data-approval="${escape(item.id)}" data-decision="reject">拒绝</button></div></div>`).join(''):'<p class="empty">没有待确认变更。</p>'}`;}
  function approvalPreview(item){const p=item.payload||{};
    if(item.proposal_type==='preference.change')return `<p>偏好：${escape(p.key)} → ${escape(plain(p.value))}</p><p class="hint">依据：${escape(p.evidence)}</p>`;
    if(item.proposal_type==='change_set')return (p.changes||[]).map(change=>approvalPreview({proposal_type:change.type,payload:change.payload})).join('');
    if(item.proposal_type==='plan.replace')return `<details open><summary>预览路线图 v${escape(p.roadmap?.version||'')}</summary><p>${escape(p.roadmap?.goal||'')}</p><p>${(p.tasks||[]).length} 项任务</p>${(p.roadmap?.timeline?.phases||[]).map(phase=>`<div class="phase"><h3>${escape(phase.title)}</h3><p class="meta">${escape(phase.start_date)} 至 ${escape(phase.end_date)}</p>${(phase.plan?.tasks||[]).map(task=>`<div class="task"><strong>${escape(task.title)}</strong><div class="meta">${escape(task.due_date||'日期待定')}</div></div>`).join('')}</div>`).join('')}<details><summary>预览路线图文章</summary><div class="article">${markdown(p.roadmap?.article||'')}</div></details></details>`;
    if(item.proposal_type==='profile.change')return [...new Set((p.facts||[]).map(f=>f.field))].map(field=>`<div class="kv"><span>${escape(fieldLabels[field]||field)}</span><span>${escape(plain(p.before?.[field]))} → ${escape(plain(p.after?.[field]))}</span></div>`).join('');
    if(item.proposal_type==='application.change')return `<p>${escape(p.university)} ${escape(p.program)}：${escape(p.before_status)} → ${escape(p.status)}</p>`;
    if(item.proposal_type==='task.command')return `<p>${escape(p.title||'任务')}：${escape(p.action)} · ${escape(p.evidence||'')}</p>`;
    return `<pre>${escape(JSON.stringify(p,null,2))}</pre>`;
  }
  async function decideApproval(id,decision){try{await request('/approvals/'+encodeURIComponent(id)+'/'+decision,{method:'POST',body:'{}'});
    toast(decision==='accept'?'变更已保存':'已拒绝变更');await Promise.all([loadProfile(),loadPlan(),loadApprovals(),loadMemory()]);}
    catch(error){toast(error.message);await loadApprovals();}}
  function switchTab(tab){state.activeTab=tab;document.querySelectorAll('.tabs button').forEach(button=>button.classList.toggle('active',button.dataset.tab===tab));
    if(tab==='approvals')loadApprovals().catch(error=>toast(error.message));
    if(tab==='memory')loadMemory().catch(error=>toast(error.message));
    for(const name of ['profile','plan','memory','approvals'])$(name+'-view').hidden=name!==tab;}
  $('auth-mode').onclick=()=>{state.authMode=state.authMode==='login'?'register':'login';$('auth-title').textContent=state.authMode==='login'?'登录':'注册';$('auth-mode').textContent=state.authMode==='login'?'没有账户？注册':'已有账户？登录';$('auth-error').textContent='';};
  $('auth-form').onsubmit=async event=>{event.preventDefault();try{const result=await request('/auth/'+state.authMode,{method:'POST',body:JSON.stringify({email:$('email').value,password:$('password').value})});state.user=result.user;showApp();await Promise.all([loadConversations(),loadProfile(),loadPlan(),loadApprovals(),loadMemory(),loadConflicts(true)]);}
    catch(error){$('auth-error').textContent=error.message;}};
  $('logout').onclick=async()=>{await request('/auth/logout',{method:'POST'}).catch(()=>{});showAuth();};
  $('new-conversation').onclick=()=>createConversation().catch(error=>toast(error.message));
  // Consolidation proposals can arrive after run_completed; refresh while idle.
  setInterval(()=>{if(state.user&&!state.running&&document.visibilityState==='visible')loadApprovals().catch(()=>{});},10000);
  $('memory-view').onclick=async event=>{
    const button=event.target.closest('[data-revoke-preference]');if(!button)return;
    button.disabled=true;
    try{await request('/memory/preferences/'+encodeURIComponent(button.dataset.revokePreference)+'/revoke',{method:'POST',body:JSON.stringify({request_id:requestId(),expected_version:Number(button.dataset.version)})});toast('偏好已撤销');}
    catch(error){toast(error.message);}
    finally{await loadMemory().catch(error=>toast(error.message));}
  };
  ui.conversations.onclick=event=>{const button=event.target.closest('[data-conversation]');if(button)openConversation(button.dataset.conversation).catch(error=>toast(error.message));};
  $('chat-form').onsubmit=event=>{event.preventDefault();const input=$('chat-input'),message=input.value.trim();if(message){input.value='';sendMessage(message);}};
  $('chat-input').onkeydown=event=>{if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();$('chat-form').requestSubmit();}};
  ui.profile.onclick=event=>{if(event.target.id==='edit-profile')editProfile();};
  $('profile-cancel').onclick=()=>$('profile-dialog').close();$('profile-form').onsubmit=saveProfile;
  $('conflict-later').onclick=()=>$('conflict-dialog').close();
  $('conflict-items').onclick=async event=>{const button=event.target.closest('[data-conflict]');if(!button)return;
    $('conflict-error').hidden=true;
    button.disabled=true;try{await request('/profile/conflicts/'+encodeURIComponent(button.dataset.conflict)+'/resolve',{method:'POST',body:JSON.stringify({choice:button.dataset.choice})});toast(button.dataset.choice==='new'?'已使用新信息':'已保留旧信息');await Promise.all([loadProfile(),loadApprovals(),loadConflicts(true)]);}
    catch(error){$('conflict-error').textContent=error.message;$('conflict-error').hidden=false;button.disabled=false;}};
  ui.plan.onclick=event=>{const task=event.target.closest('[data-task]');if(task)taskAction(task.dataset.task,task.dataset.action);
    if(event.target.id==='request-plan')sendMessage('请根据我的最新画像和申请进度生成申请规划。');};
  ui.plan.onchange=async event=>{if(event.target.id==='plan-version'){try{state.plan=await request('/plans/'+encodeURIComponent(event.target.value));renderPlan();}catch(error){toast(error.message);}}};
  ui.approvals.onclick=event=>{const button=event.target.closest('[data-approval]');if(button)decideApproval(button.dataset.approval,button.dataset.decision);};
  document.querySelectorAll('.tabs button').forEach(button=>button.onclick=()=>{switchTab(button.dataset.tab);if(button.dataset.tab==='approvals')loadConflicts(true).catch(error=>toast(error.message));});
  document.querySelectorAll('[data-panel]').forEach(button=>button.onclick=()=>{const panel=document.querySelector(button.dataset.panel==='left'?'.left-panel':'.right-panel');panel.classList.toggle('open');});
  loadInitial();
})();
