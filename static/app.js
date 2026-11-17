"use strict";

const $ = id => document.getElementById(id);
const labels = {engine: "Compiled engine", rag: "RAG baseline"};
const state = {index: {}, model: {}, examples: [], demo: "", busy: false, ready: false};
const drafts = new Map();
let reportSnapshot = "";
let reportVersion = 0;
const fmt = value => value != null && Number.isFinite(Number(value)) ? Number(value).toLocaleString() : "Unavailable";
const leaf = path => String(path || "").split(/[\\/]/).filter(Boolean).pop() || path;
const sameFolder = (a, b) => {
  if (!a || !b) return false;
  const normalize = value => {
    const cleaned = String(value).replace(/[\\/]+$/, "").replaceAll("\\", "/");
    return /^[A-Z]:\//i.test(cleaned) ? cleaned.toLowerCase() : cleaned;
  };
  return normalize(a) === normalize(b);
};
function el(tag, text, cls) {
  const n = document.createElement(tag);
  if (text != null) n.textContent = String(text);
  if (cls) n.className = cls;
  return n;
}
function message(id, text = "", error = false) {
  const n = $(id);
  n.textContent = text;
  n.classList.toggle("error", error);
}
async function api(path, body) {
  const session = sessionStorage.getItem("fieldnote-session");
  const headers = session ? {"X-Fieldnote-Session":session} : {};
  const options = body === undefined ? {} : {
    method: "POST", body: JSON.stringify(body)
  };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch("/api/" + path, {...options, headers, cache: "no-store"});
  let data;
  try { data = await response.json(); }
  catch { throw Error("The local server returned an unreadable response (" + response.status + ")."); }
  if (!response.ok || data.error) throw Error(data.error || "Request failed (" + response.status + ").");
  return data;
}
function view() {
  const id = ["workbench", "benchmark", "sources"].includes(location.hash.slice(1)) ? location.hash.slice(1) : "workbench";
  for (const name of ["workbench", "benchmark", "sources"]) {
    $(name + "-view").hidden = name !== id;
    const link = document.querySelector('nav a[href="#' + name + '"]');
    if (name === id) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current");
  }
  document.title = $(id+"-heading").textContent + " · Fieldnote";
  if(id === "benchmark") restoreReport();
}
function controls() {
  const hasIndex = Boolean(state.index.snapshot) && !state.index.knowledge?.needs_reindex;
  const demoMismatch = $("suite").value === "demo" && !sameFolder(state.index.folder, state.demo);
  for (const id of ["ask-button","compare-rag","run-bench","index-button","demo-button","browse","suite"]) {
    const blocked = (id === "ask-button" || id === "run-bench") && (!hasIndex || (id === "run-bench" && demoMismatch));
    $(id).disabled = state.busy || blocked;
    $(id).toggleAttribute("data-blocked", !state.busy && blocked);
  }
  $("suite-note").textContent = $("suite").value === "custom"
    ? "Edit the references to match the folder you indexed."
    : sameFolder(state.index.folder,state.demo) ? "Graded against the bundled demo references." : "Index the demo folder to run its references, or select Custom questions.";
  if (state.ready && !state.model.available) $("suite-note").append(" Running a paired benchmark connects the configured local model.");
  $("custom-block").hidden = $("suite").value !== "custom";
}
function folderPrompt() {
  const notice = $("ask-message");
  notice.textContent = "Index a local folder to ask a question. ";
  const b = el("button","Choose folder","text-btn");
  b.type = "button";
  b.onclick = () => $("folder").focus();
  notice.append(b);
}
function updateStatus(s) {
  const previous = state.index.snapshot;
  const oldFolder = state.index.folder;
  state.index = s.index || {};
  state.model = s.model || {};
  state.examples = s.examples || [];
  state.demo = s.demo_folder || "";
  state.ready = true;
  const x = state.index;
  if (x.folder && !sameFolder(oldFolder, x.folder)) {
    if (oldFolder) drafts.set(oldFolder, {items:readFields(), json:$("cases").value, jsonMode:!$("json-editor").hidden});
    const draft = drafts.get(x.folder);
    setFields(draft?.items || [{}]);
    $("cases").value = draft?.json || "";
    showEditor(Boolean(draft?.jsonMode));
    $("suite").value = sameFolder(x.folder, state.demo) ? "demo" : "custom";
  }
  if (previous !== x.snapshot) {
    reportSnapshot = "";reportVersion++;
    $("answers").hidden = true;
    $("benchmark-results").hidden = true;
    message("ask-message"); message("bench-message");
  }
  $("active-folder").textContent = x.folder || "No folder indexed";
  $("ask-scope").textContent = x.snapshot ? leaf(x.folder) : "No active index";
  if (!$("folder").value) $("folder").value = x.folder || "";
  $("demo-button").hidden = !state.demo;
  $("metrics").replaceChildren(...[["files","Files"],["documents","Documents"],["tables","Tables"],["rows","Table rows"]].map(([key,label])=>{
    const d = el("div"); d.append(el("b",fmt(x[key] ?? 0)),el("span",label)); return d;
  }));
  $("runtime-state").textContent = state.model.available ? "Connected" : state.model.on_demand ? "On demand" : "Unavailable";
  $("runtime-state").className = "pill " + (state.model.available ? "good" : state.model.on_demand ? "" : "warn");
  $("model-name").textContent = state.model.name || "No local model connected";
  $("model-detail").textContent = state.model.detail || (state.model.available ? "" : "Connect a local model for RAG generation. Structured queries can run directly on the index.");
  const examples = state.examples;
  $("suggestions").hidden = !examples.length;
  $("examples").replaceChildren(...examples.map(question=>{
    const b = el("button",question); b.type="button";
    b.onclick = () => { $("question").value = question; $("question").focus(); }; return b;
  }));
  if (!x.snapshot) folderPrompt();
  if (x.knowledge?.needs_reindex) message("folder-message","Click Index folder to compile this folder for the updated engine.");
  const inv = $("inventory"); inv.replaceChildren();
  if (x.knowledge?.version) {
    const k = x.knowledge;
    inv.append(el("h2","Compilation","small-heading"),pairs([["Active facts",fmt(k.facts)],["Active rules",fmt(k.rules)],["Withheld assertions",fmt(k.blocked)],["Unsupported source statements or blocks",fmt(k.omissions)]]));
    if (k.gaps?.length) {
      const detail=el("details",null,"evidence");
      detail.append(el("summary","Inspect "+fmt(k.gaps.length)+" of "+fmt(k.omissions)+" omissions"));
      const list=el("ol");
      for (const gap of k.gaps) {
        const li=el("li");li.append(el("strong",gap.source+" · "+gap.anchor),el("p",gap.reason));list.append(li);
      }
      detail.append(list);inv.append(detail);
    }
  }
  const issues = x.issues || [];
  if (issues.length) {
    inv.append(el("h2","Reading issues","small-heading"));
    for (const i of issues) {
      const d = el("div",null,"issue");d.append(el("strong",i.source),el("p",i.reason));inv.append(d);
    }
  }
  if (!x.inventory?.length) inv.append(el("p", x.snapshot ? "No indexed files in this snapshot." : "Choose a local folder to view its files.","muted"));
  else {
    inv.append(el("h2","Indexed files","small-heading"));
    for (const file of x.inventory) {
      const row = el("div",null,"inv-row"); const n=el("div");
      n.append(el("div",file.source,"inv-name"),el("small",fmt(file.units)+" extracted units","muted"));
      row.append(n,el("span",file.status,"pill"));inv.append(row);
    }
  }
  controls();
  if (!$("benchmark-view").hidden) restoreReport();
}
async function restoreReport() {
  const snapshot=state.index.snapshot;
  if (!snapshot || reportSnapshot===snapshot) return;
  const version=reportVersion;
  reportSnapshot=snapshot;
  try {
    const {report}=await api("results");
    if (version!==reportVersion || snapshot!==state.index.snapshot || !report || report.snapshot!==snapshot) return;
    showBench(report);
    message("bench-message","Showing the last saved benchmark for this snapshot.");
  } catch (error) {
    if (version===reportVersion) {
      reportSnapshot="";
      message("bench-message","Could not load saved results. "+error.message,true);
    }
  }
}
async function action(id, loading, run) {
  if (state.busy) return;
  state.busy=true; controls();message(id,loading);
  try { await run(); }
  catch(error) { message(id,error.message,true); }
  finally { state.busy=false;controls(); }
}
function pairs(data) {
  const dl = el("dl",null,"token-table");
  for (const [key,value] of data) dl.append(el("dt",key),el("dd",value));
  return dl;
}
function answerCard(method, value) {
  const article=el("article",null,"answer "+method);
  const result = value || {status:"unavailable",answer:"No result returned."};
  const heading=el("div",null,"answer-heading");
  const kind = result.status === "answered" ? "good" : result.status === "error" ? "bad" : "warn";
  heading.append(el("h3",labels[method]),el("span",result.status,"pill "+kind));
  const body=el("p",result.answer || "No answer returned.","answer-body");body.dir="auto";
  article.append(heading,el("p",result.route,"route"),body);
  if (result.scope) article.append(el("p",result.scope,"field-note"));
  article.append(pairs([["Input tokens",fmt(result.input_tokens)],["Output tokens",fmt(result.output_tokens)],["Model calls",fmt(result.calls)],
    ["Metering",result.usage_kind || "Unavailable"],["Elapsed",result.elapsed_ms == null ? "Unavailable" : fmt(result.elapsed_ms)+" ms"]]));
  if (result.proof) {
    const details=el("details",null,"evidence");
    details.append(el("summary","Derivation"),el("pre",JSON.stringify(result.proof,null,2),"derivation"));
    article.append(details);
  }
  const evidence=result.evidence || [];
  if(evidence.length) {
    const details=el("details",null,"evidence");details.append(el("summary","Evidence ("+evidence.length+")"));
    const ol=el("ol");
    for(const item of evidence) {
      const li=el("li");const text=el("p",item.text,"snippet");text.dir="auto";
      li.append(el("p",[item.source,item.anchor].filter(Boolean).join(" · "),"locator"),text);ol.append(li);
    }
    details.append(ol);article.append(details);
  }
  return article;
}
function stamp(id, value) {
  $(id).textContent = value ? "Snapshot "+String(value).slice(0,12) : "";
  $(id).title = String(value || "");
}
function mark(value) {
  if(value===true || value===1) return "matched";
  if(value===false || value===0) return "missed";
  return "unscored";
}
function makeField(label, value, className, multiline=false) {
  const wrapper=el("label",label);
  const input=el(multiline ? "textarea" : "input");
  input.className=className;input.dir="auto";input.value=value || "";
  if (multiline) input.rows=2;
  wrapper.append(input);
  return wrapper;
}
function appendCase(item={}) {
  const fields=el("fieldset",null,"case-fields");
  const legend=el("legend");
  const q=makeField("Question",item.question,"case-question");
  q.firstElementChild.maxLength=4000;
  const answer=makeField("Expected answer",item.expected,"case-expected");
  const expectation=el("label",null,"compare-option");
  const abstain=el("input");abstain.type="checkbox";abstain.className="case-abstain";abstain.checked=item.answerable===false;
  expectation.append(abstain,document.createTextNode("The sources cannot answer this question"));
  const toggle=()=>{answer.hidden=abstain.checked;};
  abstain.onchange=toggle;toggle();
  const options=el("div",null,"case-actions");
  const aliases=el("details");
  aliases.append(el("summary","Accepted alternatives"),makeField("One alternative per line",(item.aliases || []).join("\n"),"case-aliases",true));
  const remove=el("button","Remove","text-btn");
  remove.type="button";
  remove.onclick=()=>{
    const next=fields.nextElementSibling || fields.previousElementSibling;
    fields.remove();numberCases();(next?.querySelector(".case-question") || $("add-case")).focus();
  };
  options.append(aliases,remove);fields.append(legend,q,expectation,answer,options);$("case-list").append(fields);
}
function numberCases() {
  const rows=[...$("case-list").children];
  rows.forEach((row,index)=>{
    row.querySelector("legend").textContent="Entry "+(index+1);
    const remove=row.querySelector("button");
    remove.hidden=rows.length===1;
    remove.setAttribute("aria-label","Remove entry "+(index+1));
  });
  $("add-case").disabled=rows.length>=100;
}
function setFields(items) {
  $("case-list").replaceChildren();
  for (const item of items) appendCase(item);
  numberCases();
}
function readFields() {
  return [...$("case-list").children].map(row=>({
    question:row.querySelector(".case-question").value,
    expected:row.querySelector(".case-abstain").checked ? "[unanswerable]" : row.querySelector(".case-expected").value,
    answerable:!row.querySelector(".case-abstain").checked,
    aliases:row.querySelector(".case-aliases").value.split("\n").map(x=>x.trim()).filter(Boolean)
  }));
}
function validateCases(items, allowIncomplete=false) {
  if (!Array.isArray(items) || !items.length || items.length>100) throw Error("Provide between 1 and 100 questions.");
  for (const [n, item] of items.entries()) {
    if (item?.answerable!==undefined && typeof item.answerable!=="boolean") throw Error("Answerable must be true or false for entry "+(n+1)+".");
    for (const key of ["question","expected"]) {
      if (typeof item?.[key]!=="string" || (!allowIncomplete && !item[key].trim())) {
        throw Error("Enter "+(key==="question"?"a question":"an expected answer")+" for entry "+(n+1)+".");
      }
    }
    if (item.question.length>4000) throw Error("The question for entry "+(n+1)+" exceeds 4,000 characters.");
    if (item.aliases!==undefined && (!Array.isArray(item.aliases) || item.aliases.some(x=>typeof x!=="string" || !x.trim()))) {
      throw Error("Supply a list of nonempty alternatives for entry "+(n+1)+".");
    }
  }
  return items;
}
function showEditor(jsonMode) {
  $("field-editor").hidden=jsonMode;$("json-editor").hidden=!jsonMode;
  $("switch-editor").textContent=jsonMode ? "Edit as fields" : "Edit as JSON";
}
$("add-case").onclick=()=>{
  appendCase();numberCases();$("case-list").lastElementChild.querySelector(".case-question").focus();
};
$("switch-editor").onclick=()=>{
  if ($("json-editor").hidden) {
    $("cases").value=JSON.stringify(readFields(),null,2);showEditor(true);$("cases").focus();
  } else {
    try {setFields(validateCases(JSON.parse($("cases").value),true));}
    catch(error){message("bench-message",error.message,true);$("cases").focus();return;}
    showEditor(false);$("case-list").querySelector(".case-question").focus();
  }
  message("bench-message");
};
function showBench(data) {
  $("download").onclick=()=>{
    const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:"application/json"}));
    const link=el("a");link.href=url;link.download="fieldnote-benchmark-"+String(data.snapshot||"results").replace(/[^a-z0-9_-]/gi,"_")+".json";
    link.click();setTimeout(()=>URL.revokeObjectURL(url),3000);
  };
  stamp("bench-snapshot",data.snapshot);
  const board=$("scoreboard");board.replaceChildren();
  for (const method of Object.keys(labels)) {
    const m=data.summary[method];
    const d=el("div",null,"scorecard "+method);
    const percent=m.total ? Math.round(100*m.correct/m.total)+"%" : "—";
    d.append(el("h3",labels[method]),el("p",percent,"accuracy"),el("p",fmt(m.correct)+" / "+fmt(m.total)+" correct outcomes","coverage"));
    const rawMatches = m.value_matches == null ? "Unreported" : fmt(m.value_matches)+" / "+fmt(m.answerable ?? m.total);
    d.append(pairs([["Raw value matches",rawMatches],["Correct abstentions",fmt(m.correct_abstentions ?? 0)+" / "+fmt(m.unanswerable ?? 0)],["Answered",fmt(m.answered)],["Errors",fmt(m.errors)],["Metered input tokens",fmt(m.input_tokens)],["Metered output tokens",fmt(m.output_tokens)],
      ["Model calls",fmt(m.calls)],["Unmetered calls",fmt(m.unmetered_calls)]]));
    board.append(d);
  }
  const runs=$("bench-runs");runs.replaceChildren();
  for(const r of data.runs) {
    const d=el("details",null,"bench-case");
    const title=el("summary",r.question);
    title.append(el("span",labels.engine+" "+mark(r.engine_score)+" · RAG "+mark(r.rag_score),"outcomes"));
    const content=el("div",null,"case-body");
    const cards=el("div",null,"answer-grid");cards.append(answerCard("engine",r.engine),answerCard("rag",r.rag));
    content.append(el("p","Reference answer · "+r.expected), cards);d.append(title,content);runs.append(d);
  }
  $("benchmark-results").hidden=false;
}
window.addEventListener("hashchange",()=>{
  if (location.hash.startsWith("#pair=")) init();
  else view();
});
$("suite").onchange=controls;
$("browse").onclick=()=>action("folder-message","Choose a folder in the system dialog…",async()=>{
  const d=await api("browse",{});if(d.folder) $("folder").value=d.folder;
  message("folder-message",d.folder ? "Folder selected. Click Index folder to use it." : "Folder selection canceled.");
});
async function indexFolder(folder) {
  if(!folder) { $("folder").focus();return; }
  await action("folder-message","Indexing files…",async()=>{
    await api("index",{folder});const s=await api("status");$("folder").value=s.index?.folder || folder;updateStatus(s);
    const issues=s.index?.issues?.length || 0;
    message("folder-message","Index ready. "+fmt(s.index?.files)+" files · "+fmt(issues)+" reading issues.");
  });
}
$("folder-form").onsubmit=e=>{ e.preventDefault();indexFolder($("folder").value.trim()); };
$("demo-button").onclick=()=>indexFolder(state.demo);
$("ask-form").onsubmit=e=>{
  e.preventDefault();const question=$("question").value.trim();
  if(!question){message("ask-message","Enter a question.",true);$("question").focus();return;}
  const paired=$("compare-rag").checked;
  action("ask-message",paired ? "Running both methods on the active snapshot…" : "Querying the compiled index…",async()=>{
    const data=await api("ask",{question,mode:paired ? "both" : "engine"});
    $("asked-question").textContent=data.question;stamp("ask-snapshot",data.snapshot);
    $("answer-grid").replaceChildren(answerCard("engine",data.engine));
    if (paired) $("answer-grid").append(answerCard("rag",data.rag));
    $("answers").hidden=false;message("ask-message");
    if (paired) updateStatus(await api("status"));
  });
};
$("bench-form").onsubmit=e=>{
  e.preventDefault();let payload={};
  if($("suite").value==="custom") {
    try{
      const items=$("json-editor").hidden ? readFields() : JSON.parse($("cases").value);
      payload={cases:validateCases(items)};
    }catch(err){
      message("bench-message",err.message,true);
      if ($("json-editor").hidden) [...$("case-list").querySelectorAll("input")].find(field=>!field.value.trim())?.focus();
      else $("cases").focus();
      return;
    }
  }
  action("bench-message","Benchmark running. Both methods receive each question in turn…",async()=>{
    reportVersion++;
    try { const data=await api("benchmark",payload);showBench(data);message("bench-message","Benchmark complete."); }
    finally { updateStatus(await api("status")); }
  });
};
async function init() {
  const pairing = new URLSearchParams(location.hash.slice(1)).get("pair");
  if (pairing) history.replaceState(null, "", location.pathname + location.search);
  setFields([{}]);view();controls();
  try {
    if (pairing) {
      const paired = await api("session", {token: pairing});
      sessionStorage.setItem("fieldnote-session", paired.session);
    }
    updateStatus(await api("status"));
  } catch(err) { message("folder-message",err.message,true); }
}
init();
