"use strict";
const $ = id => document.getElementById(id);
const token = new URLSearchParams(location.search).get("token") || "";
const client = crypto.randomUUID();
let fontScale = 100;
let view = "papers", selectedTags = new Set(), data = null, settingsData = null, openReport = null;
let refreshing = false, toastTimer, filterTimer, page = 1;
let tagsExpanded=false, openPaperId=null, progressSignature="";
const names = {papers:"论文库",daily:"日报",weekly:"周报",monthly:"月报",history:"任务记录",settings:"设置与数据"};
const descriptions = {papers:"把值得关注的研究，留在一个安静的地方。",daily:"每天留下一份研究速览。",weekly:"回看一周里值得关注的论文。",monthly:"把一个月的研究线索串起来。",history:"查看已经完成与未完成的获取任务。",settings:"管理自动运行、阅读方式和本地资料。"};
function node(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "class") el.className = value;
    else if (key === "text") el.textContent = value;
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else el.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child !== null && child !== undefined) el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}
function notify(message) {
  $("toast").textContent = message; $("toast").hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => $("toast").hidden = true, 6000);
}
async function api(path, payload) {
  const options = {headers: {"X-Session-Token": token}, cache: "no-store"};
  if (payload !== undefined) {
    options.method = "POST"; options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(payload);
  }
  const response = await fetch(path, options);
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "请求失败");
  return result;
}
function action(id, callback) {
  $(id).addEventListener("click", async () => {
    $(id).disabled = true;
    try { await callback(); } catch (err) { notify(err.message); }
    finally { $(id).disabled = false; }
  });
}
function applyFontScale(scale) {
  fontScale = scale;
  document.documentElement.dataset.fontScale = String(scale);
  for (const field of document.querySelectorAll("[data-font-scale-control]")) field.value = String(scale);
}
async function loadAppearance() {
  const result = await api("/api/appearance");
  applyFontScale(result.font_scale);
}
for (const field of document.querySelectorAll("[data-font-scale-control]")) field.addEventListener("change", async () => {
  const previous = fontScale;
  const controls = [...document.querySelectorAll("[data-font-scale-control]")];
  for (const item of controls) item.disabled = true;
  applyFontScale(Number(field.value));
  try {
    const result = await api("/api/appearance", {font_scale:fontScale});
    applyFontScale(result.font_scale);
    notify("字号已保存，下次打开仍会使用");
  } catch (err) {
    applyFontScale(previous);
    notify("字号保存失败，已恢复原设置：" + err.message);
  } finally {
    for (const item of controls) item.disabled = false;
  }
});
function params() {
  const p = new URLSearchParams();
  for (const key of ["start", "end", "state"]) if ($(key).value) p.set(key, $(key).value);
  if ($("query").value.trim()) p.set("q", $("query").value.trim());
  if (["daily","weekly","monthly"].includes(view)) p.set("kind", view);
  if (selectedTags.size) p.set("tags", [...selectedTags].join(","));
  p.set("tag_mode", $("tag-mode").value);
  return p;
}
async function loadData() {
  if (refreshing) return;
  refreshing = true;
  try {
    data = await api("/api/data?" + params());
    $("stat-papers").textContent = data.stats.papers;
    $("stat-read").textContent = data.stats.read;
    $("stat-reports").textContent = data.stats.reports;
    $("stat-calls").textContent = data.stats.usage.calls;
    $("nav-count").textContent = data.stats.papers;
    $("retention-note").textContent = "保留 " + data.stats.cutoff + " 起的榜单";
    $("disk-state").textContent = "论文数据库约 " + (data.stats.db_bytes / 1024 / 1024).toFixed(2) + " MB；所有解读仅基于摘要。";
    renderTags(); renderResults(); renderStatus(data.status); refreshOpenPaper();
    if(!progressSignature) progressSignature=JSON.stringify(["fetch","ai"].map(k=>[data.status[k]?.running,data.status[k]?.done,data.status[k]?.finished_at]));
  } finally { refreshing = false; }
}
function renderTags() {
  const counts=data.tag_counts || {};
  const groups=data.tag_groups || [];
  const button=tag => node("button", {
    class:"tag"+(selectedTags.has(tag) ? " selected" : ""),
    "aria-pressed":String(selectedTags.has(tag)),
    onclick:()=>{selectedTags.has(tag) ? selectedTags.delete(tag) : selectedTags.add(tag);page=1;loadData().catch(e=>notify(e.message));}
  },tag+" · "+(counts[tag] || 0));
  const rows=[node("div",{class:"tag-group"},
    node("span",{class:"tag-group-label"},"分类状态"),
    node("div",{class:"tag-group-options"},button("未分类")))];
  for(const group of groups) {
    const all=group.tags.filter(tag=>(counts[tag] || 0)>0 || selectedTags.has(tag));
    all.sort((a,b)=>(counts[b] || 0)-(counts[a] || 0));
    const shown=tagsExpanded ? all : all.filter((tag,index)=>index<5 || selectedTags.has(tag));
    if(shown.length) rows.push(node("div",{class:"tag-group"},node("span",{class:"tag-group-label"},group.name),
      node("div",{class:"tag-group-options"},...shown.map(button))));
  }
  $("tag-list").replaceChildren(...rows);
  $("tag-toggle").textContent=tagsExpanded ? "收起分类" : "展开全部已用分类";
  $("tag-toggle").setAttribute("aria-expanded",String(tagsExpanded));
}
function tagNodes(tags) { return node("div", {class:"tags"}, ...(tags.length ? tags : ["未分类"]).map(t => node("span", {class:"tag"}, t))); }
function empty(title, message, button) {
  const content = node("div", {class:"empty"}, node("strong", {}, title), node("p", {}, message));
  if (button) content.append(node("button", {class:"primary", onclick: () => $("update").click()}, button));
  return content;
}
function paperCard(paper) {
  return node("article", {class:"card"},
    node("div", {class:"card-meta"}, node("span", {class:"badge"}, paper.ai_state === "done" ? "已完成摘要解读" : "原始摘要"),
      node("span", {}, paper.selected_date || ""), node("span", {}, "arXiv " + paper.id)),
    node("h2", {tabindex:"0", onclick:() => showPaper(paper), onkeydown:e => {if (e.key==="Enter") showPaper(paper);}}, paper.title),
    node("p", {class:"abstract-preview"}, paper.intro || paper.translation || paper.abstract || "来源未提供摘要"),
    node("div", {class:"card-bottom"}, tagNodes(paper.tags), node("button", {class:"quiet", onclick:() => showPaper(paper)}, "查看摘要与解读 ↗")));
}
function reportCard(report) {
  const card = node("article", {class:"card"});
  card.append(node("div", {class:"report-head"},
    node("div", {}, node("div", {class:"card-meta"}, node("span", {class:"badge"}, names[report.kind]), node("span", {}, report.period)),
      node("h2", {onclick:() => {openReport = openReport === report.id ? null : report.id; renderResults();}}, report.start + (report.start===report.end ? "" : " — " + report.end))),
    node("button", {class:"secondary", onclick:() => {openReport = openReport === report.id ? null : report.id; renderResults();}}, openReport===report.id ? "收起" : "查看论文")));
  card.append(node("p", {class:"muted"}, "本期保存 " + report.count + " 篇 · 当前匹配 " + report.matched + " 篇 · 获取于 " + report.fetched_at.slice(0,16).replace("T"," ")));
  if (report.top_tags.length) card.append(tagNodes(report.top_tags.map(([tag, count]) => tag + " · " + count)));
  else card.append(node("p", {class:"note"}, "尚无分类标签。配置 API 后可按需生成解读，也可手动添加标签。"));
  if (openReport===report.id) {
    const list = node("div", {class:"report-papers"});
    const select = node("select", {}, node("option", {value:"match"}, "只看匹配论文（" + report.matched + "）"), node("option", {value:"all"}, "查看本期全部论文（" + report.count + "）"));
    const container = node("div");
    function fill() {
      const papers = select.value==="all" ? report.all_papers : report.papers;
      container.replaceChildren(...papers.map(paper => node("div", {class:"mini-paper",tabindex:"0",
        onclick:() => showPaper(paper), onkeydown:e => {if (e.key==="Enter") showPaper(paper);}},
        String(paper.rank).padStart(2,"0") + "  " + paper.title,
        node("small", {}, paper.tags.join(" / ") || "未分类"))));
    }
    select.addEventListener("change", fill); list.append(select, container); fill(); card.append(list);
  }
  return card;
}
function renderResults() {
  if (!data) return;
  const reportMode = view!=="papers";
  const entries = reportMode ? data.reports : data.papers;
  const size = reportMode ? 12 : 25;
  page = Math.max(1, Math.min(page, Math.ceil(entries.length / size) || 1));
  $("result-count").textContent = reportMode ? "找到 " + entries.length + " 期" + names[view] : "找到 " + entries.length + " 篇论文 · 已去重";
  if (!entries.length) {
    const fresh = data.stats.papers===0;
    $("results").replaceChildren(empty(fresh ? "从第一份论文速览开始" : "没有匹配的内容",
      fresh ? "点击「立即获取」，保存最近日、周、月榜。没有 DeepSeek API 也可以先收集作者摘要。自动获取默认关闭。" : "试试调整日期、标签或处理状态。没有标签的论文可以在「未分类」中找到。",
      fresh ? "获取最新榜单" : null));
    return;
  }
  $("results").replaceChildren(...entries.slice((page-1)*size,page*size).map(reportMode ? reportCard : paperCard));
  if (entries.length>size) {
    const pager = node("div", {class:"actions"}, node("button", {class:"secondary", onclick:() => {page = Math.max(1,page-1); renderResults();}}, "上一页"),
      node("span", {class:"note"}, page + " / " + Math.ceil(entries.length/size)),
      node("button", {class:"secondary", onclick:() => {page = Math.min(Math.ceil(entries.length/size),page+1); renderResults();}}, "下一页"));
    $("results").append(pager);
  }
}
function section(title, content) { return node("section", {class:"paper-section"}, node("h3", {}, title), node("p", {}, content)); }
function showPaper(paper) {
  openPaperId=paper.id;
  const content=$("paper-content");
  const read=node("button",{id:"paper-read-action",class:"primary",onclick:async()=>{
    read.disabled=true;
    try {await api("/api/read",{ids:[paper.id]});notify("AI 已开始，只处理这篇论文；失败不重试");}
    catch(err){notify(err.message);}
    finally{read.disabled=false;}
  }},paper.ai_state==="done" ? "重新生成翻译与简介" : "AI 翻译与简介");
  const selected=new Set(paper.tags);
  const editor=node("div",{class:"tag-editor"});
  for(const group of data.tag_groups || []) {
    const fieldset=node("fieldset",{class:"tag-editor-group"},node("legend",{},group.name));
    for(const tag of group.tags) {
      const check=node("input",{type:"checkbox","aria-label":tag});
      check.checked=selected.has(tag);
      check.addEventListener("change",()=>{
        if(check.checked && selected.size>=4){check.checked=false;notify("每篇最多选择 4 个固定分类");return;}
        check.checked ? selected.add(tag) : selected.delete(tag);
      });
      fieldset.append(node("label",{class:"tag-option"},check,tag));
    }
    editor.append(fieldset);
  }
  const save=node("button",{class:"secondary",onclick:async()=>{
    try{
      const tags=[...selected];
      await api("/api/tags",{id:paper.id,tags});
      paper.tags=tags;notify("固定分类已保存");await loadData();
    }catch(err){notify(err.message);}
  }},"保存分类");
  const translation=section("中文翻译",paper.translation || "尚未生成。可点击下方按钮处理，或在设置中开启获取后自动翻译与简介。");
  translation.querySelector("p").id="paper-translation";
  const intro=section("讲解简介",paper.intro || "尚未生成简介。");
  intro.querySelector("p").id="paper-intro";
  content.replaceChildren(node("h2",{},paper.title),node("div",{id:"paper-tags"},tagNodes(paper.tags)),
    node("div",{class:"paper-links"},node("a",{href:paper.url,target:"_blank",rel:"noopener noreferrer"},"Hugging Face ↗"),
      node("a",{href:paper.paper_url,target:"_blank",rel:"noopener noreferrer"},"论文原文 ↗")),
    node("p",{class:"note"},"解读仅基于作者摘要，未读取全文。获取和 AI 可以分别启动、分别停止。"),
    section("原始摘要",paper.abstract || "来源未提供摘要"),translation,intro,
    node("div",{class:"actions"},read),
    section("原始关键词（可通过搜索框查找）",(paper.keywords || []).join("、") || "无历史关键词。新分类统一使用固定词表。"),
    node("div",{class:"paper-section"},node("h3",{},"手动调整固定分类"),
      node("p",{class:"note"},"每篇最多 4 个分类。这里的修改不会删除原始关键词或已有翻译。"),editor,save));
  if(!$("paper-dialog").open) $("paper-dialog").showModal();
}
function refreshOpenPaper() {
  if(!$("paper-dialog").open || !openPaperId || !data) return;
  const paper=data.papers.find(p=>p.id===openPaperId) ||
    data.reports.flatMap(report=>report.all_papers).find(p=>p.id===openPaperId);
  if(!paper) return;
  $("paper-tags")?.replaceChildren(tagNodes(paper.tags));
  if($("paper-translation")) $("paper-translation").textContent=paper.translation || "尚未生成翻译。";
  if($("paper-intro")) $("paper-intro").textContent=paper.intro || "尚未生成简介。";
  if($("paper-read-action")) $("paper-read-action").textContent=paper.ai_state==="done" ? "重新生成翻译与简介" : "AI 翻译与简介";
}
function pendingPapers() {
  return (data?.papers || []).filter(p=>p.ai_state!=="done" && p.abstract);
}
function renderStatus(status) {
  for(const kind of ["fetch","ai"]) {
    const value=status[kind] || {running:false,message:"尚未开始",done:0,total:0,errors:[]};
    $(kind+"-message").textContent=value.message+(value.total ? " · "+value.done+" / "+value.total : "")+
      (value.errors?.length ? " · "+value.errors.join("；") : "");
    $("stop-"+kind).disabled=!value.running || value.cancelling;
  }
  $("update").disabled=!!status.fetch?.running;
  $("start-ai").disabled=!!status.ai?.running || !pendingPapers().length;
  const remaining=Math.max(0,(settingsData?.settings.daily_ai_limit || 30)-(data?.stats.usage.calls || 0));
  const automatic=!!settingsData?.settings.ai_after_fetch;
  $("fetch-description").textContent="从 Hugging Face 获取实际发布的最新日榜及已结束的周月榜，每期最多 20 篇。"+(automatic ? "完成后自动翻译与简介，每轮最多 20 篇。" : "当前只保存摘要，不自动调用 AI。");
  $("task-message").textContent="当前筛选待解读 "+pendingPapers().length+" 篇 · 每批最多 20 篇 · 今日剩余 "+remaining+" 次 AI 调用。获取后自动解读："+(automatic ? "已开启。" : "已关闭。");
}
async function loadSettings(fields=null) {
  settingsData = await api("/api/settings");
  for (const [key,value] of Object.entries(settingsData.settings)) {
    if(fields && !fields.includes(key)) continue;
    const field = $("cfg-"+key); if (!field) continue;
    if (field.type==="checkbox") field.checked = value; else field.value = value;
  }
  $("key-state").textContent = settingsData.has_key ? "已保存加密密钥；界面不会回显。" : "未设置密钥，仅获取原始摘要。";
  $("schedule-state").textContent = settingsData.task_installed ? "Windows 计划任务已登记（以总开关控制执行）。" : "未登记 Windows 计划任务。";
  if(!fields || fields.includes("daily_ai_limit")) $("ai-settings-state").textContent="已保存：每日最多 "+settingsData.settings.daily_ai_limit+" 次 AI 请求；获取后自动解读"+(settingsData.settings.ai_after_fetch ? "已开启。" : "已关闭。");
  $("manual-period").value ||= settingsData.latest[0].period;
  if(data) renderStatus(data.status);
}
async function loadHistory() {
  const result = await api("/api/history");
  $("history-list").replaceChildren(...(result.attempts.length ? result.attempts.map(row => node("div",{class:"history-row"},
    row.report_id + " · " + ({done:"已保存",failed:"未完成",running:"已开始",cancelled:"已停止",unavailable:"来源无对应榜单，已跳过",checked:"已检查最新日榜"}[row.state] || row.state),
    node("small",{},row.run_date + (row.message ? " · " + (row.message.includes("has no attribute") ? "旧版获取错误已修复，可手动重新获取" : row.message) : "")))) : [node("p",{class:"muted"},"暂时没有获取记录。")]));
}
async function switchView(next) {
  view=next; page=1; openReport=null;
  $("page-title").textContent=names[view]; $("page-desc").textContent=descriptions[view];
  for (const btn of document.querySelectorAll("nav button")) btn.classList.toggle("active",btn.dataset.view===view);
  $("reading").hidden=["settings","history"].includes(view);
  $("settings").hidden=view!=="settings"; $("history").hidden=view!=="history";
  if(view==="settings") await loadSettings();
  else if(view==="history") await loadHistory();
  else await loadData();
}
for (const btn of document.querySelectorAll("nav button")) btn.addEventListener("click",()=>switchView(btn.dataset.view).catch(e=>notify(e.message)));
for (const id of ["query","start","end","state","tag-mode"]) $(id).addEventListener(id==="query" ? "input" : "change",()=>{
  clearTimeout(filterTimer); page=1; filterTimer=setTimeout(()=>loadData().catch(e=>notify(e.message)),250);
});
action("reset",async()=>{selectedTags.clear(); for(const id of ["query","start","end","state"]) $(id).value=""; $("tag-mode").value="any"; page=1; await loadData();});
action("update",async()=>{await api("/api/run",{});notify("已开始检查实际发布的榜单；获取后按设置决定是否自动解读");});
action("start-ai",async()=>{
  const ids=pendingPapers().slice(0,20).map(p=>p.id);
  if(!ids.length) throw new Error("当前筛选没有待解读的论文");
  await api("/api/read",{ids});notify("已开始处理 "+ids.length+" 篇论文，可单独停止 AI");
});
action("stop-fetch",async()=>{await api("/api/stop",{kind:"fetch"});notify("正在停止获取论文；AI 任务不受影响");});
action("stop-ai",async()=>{await api("/api/stop",{kind:"ai"});notify("正在停止 AI；获取论文不受影响");});
action("tag-toggle",async()=>{tagsExpanded=!tagsExpanded;renderTags();});

action("save-settings",async()=>{
  const values={};
  for(const key of ["auto","daily","weekly","monthly"]) values[key]=$("cfg-"+key).checked;
  values.backfill_days=Number($("cfg-backfill_days").value);
  values.time=$("cfg-time").value;
  await api("/api/settings",values); notify("自动获取设置已保存"); await loadSettings(Object.keys(values));
});
const aiSettingKeys=["model","intro_limit","daily_ai_limit","ai_after_fetch"];
for(const key of aiSettingKeys) $("cfg-"+key).addEventListener("input",()=>{
  $("ai-settings-state").textContent="有未保存的修改，请点击「保存 AI 设置」。";
});
action("save-ai-settings",async()=>{
  for(const key of aiSettingKeys) if(!$("cfg-"+key).reportValidity()) {
    $("ai-settings-state").textContent="尚未保存：请检查模型名称，简介字数需为 100～1000 的整数，每日上限需为 1～100 的整数。";
    return;
  }
  const values={model:$("cfg-model").value.trim(),intro_limit:Number($("cfg-intro_limit").value),daily_ai_limit:Number($("cfg-daily_ai_limit").value),ai_after_fetch:$("cfg-ai_after_fetch").checked};
  try {
    await api("/api/settings",values);
    await loadSettings(aiSettingKeys);
    notify("AI 设置已保存，每日上限 "+values.daily_ai_limit+" 次");
  } catch(err) {
    $("ai-settings-state").textContent="保存失败："+err.message;
    throw err;
  }
});
action("save-key",async()=>{await api("/api/key",{key:$("api-key").value}); $("api-key").value=""; notify("密钥已加密保存"); await loadSettings([]);});
action("delete-key",async()=>{await api("/api/key",{delete:true}); $("api-key").value=""; notify("已删除本地密钥"); await loadSettings([]);});
action("test-key",async()=>{
  const result=await api("/api/models",{}); $("models").replaceChildren(...result.models.map(model=>node("option",{value:model})));
  notify("连接成功，可用模型："+result.models.join("、"));
});
$("manual-kind").addEventListener("change",()=>{const kind=$("manual-kind").value; const found=settingsData?.latest.find(s=>s.kind===kind); if(found) $("manual-period").value=found.period;});
action("manual-run",async()=>{await api("/api/run",{kind:$("manual-kind").value,period:$("manual-period").value.trim()}); notify("已开始获取指定榜单；获取后按设置决定是否自动解读");});
action("disable",async()=>{await api("/api/disable",{}); notify("已停止自动获取，资料保留；AI 可单独停止"); await loadSettings(["auto"]);});
action("export",async()=>{const result=await api("/api/export",{}); notify("已导出到 "+result.path);});
action("uninstall-preview",async()=>{
  const result=await api("/api/uninstall-preview",{}); $("delete-preview").textContent="根目录："+result.root+"\n计划任务："+result.task+"\n\n"+result.files.join("\n");
  $("delete-confirm").value=""; $("uninstall-dialog").showModal();
});
action("uninstall-confirm",async()=>{
  await api("/api/uninstall",{confirm:$("delete-confirm").value}); $("uninstall-dialog").close();
  $("results").replaceChildren(empty("已启动卸载","程序正在退出并清理登记文件。如有文件被占用会保留，请稍后检查 D:\\classproject\\game\\paper-reader。"));
  notify("已启动卸载，请关闭此页面"); clearInterval(pollTimer); clearInterval(heartbeatTimer);
});
action("exit",async()=>{await api("/api/exit",{}); clearInterval(pollTimer); clearInterval(heartbeatTimer); $("task-message").textContent="程序已退出，可以关闭此页面。"; $("update").disabled=true; notify("阅读助手已退出；已开启的 Windows 自动任务不受影响");});
$("close-paper").addEventListener("click",()=>$("paper-dialog").close());
$("close-uninstall").addEventListener("click",()=>$("uninstall-dialog").close());
async function heartbeat(){try{await api("/api/heartbeat",{client});}catch{}}
const heartbeatTimer=setInterval(heartbeat,15000);
window.addEventListener("pagehide",()=>navigator.sendBeacon("/api/closing",new Blob([JSON.stringify({token,client})],{type:"application/json"})));
const pollTimer=setInterval(async()=>{
  try{
    const status=await api("/api/status"); renderStatus(status);
    const signature=JSON.stringify(["fetch","ai"].map(k=>[status[k]?.running,status[k]?.done,status[k]?.finished_at]));
    if(progressSignature && signature!==progressSignature){await loadData();if(view==="history") await loadHistory();}
    progressSignature=signature;
  }catch{}
},3000);
(async()=>{try{await loadAppearance(); await heartbeat(); await loadData(); await loadSettings();}catch(err){notify(err.message); $("task-message").textContent=err.message;}})();
