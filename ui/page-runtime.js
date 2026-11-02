/* 四頁共用：小版號、登入期間資料快取、保留頁面狀態的切頁。 */
(()=>{'use strict';
if(window.rtRuntime)return;
const raw=window.fetch.bind(window),PREFIX='rt:data:1:',FRESH=5000,KEEP=600000;
const host=window.frameElement&&parent.rtDataHub?parent:window;
const hub=host.rtDataHub||(host.rtDataHub={meta:null,metaAt:0,metaPending:null,requests:new Map(),windows:new Set()});
hub.generation??=0;
hub.latest??=new Map();
hub.inflight??=0;hub.done??=0; // 2.22.3 預載要等「第一批資料抓完、畫好」才開始：數進行中的資料請求與已完成的次數
hub.windows.add(window);
// 外殼先設 hidden 再給 src 的 iframe（預先載入）一開始就算藏著，頁面的輪詢計時器不會起來。
let inactive=false;try{inactive=!!(window.frameElement&&window.frameElement.hidden)}catch(e){}
window.rtPageHidden=()=>document.hidden||inactive;
function clear(){hub.generation++;hub.requests.clear();hub.latest.clear();hub.meta=null;hub.metaAt=0;hub.metaPending=null;try{for(const k of Object.keys(sessionStorage))if(k.startsWith(PREFIX))sessionStorage.removeItem(k)}catch(e){}}
function emit(type,detail){for(const w of hub.windows)try{if(!w.closed)w.dispatchEvent(new w.CustomEvent(type,{detail}))}catch(e){}}
async function versions(force=false){
 if(!force&&hub.meta&&Date.now()-hub.metaAt<30000)return hub.meta;
 if(hub.metaPending)return hub.metaPending;
 const generation=hub.generation;
 const work=(async()=>{const r=await raw('/board/api/ui-version',{headers:{Accept:'application/json'},cache:'no-store'});
  if(generation!==hub.generation)throw new Error('資料已變更，請重試');
  if(!r.ok){if(r.status===401||r.status===403){clear();hub.meta=null;hub.metaAt=0}throw new Error('版本資料 '+r.status)}
  const value=await r.json();if(generation!==hub.generation)throw new Error('資料已變更，請重試');if(!value.session||!value.stable||!value.preview)throw new Error('版本資料格式不對');
  if(hub.meta&&hub.meta.session!==value.session)clear();hub.meta=value;hub.metaAt=Date.now();return value;
 })().finally(()=>{if(hub.metaPending===work)hub.metaPending=null});hub.metaPending=work;return work;
}
function edition(path){return /^\/board\/preview(?:\/|$)/.test(path)?'preview':'stable'}
function pageKind(path){const p=path.replace(/^\/board(?:\/preview)?/,'').replace(/\/$/,'');return p===''?'board':['/memory','/monitor','/usage'].includes(p)?p.slice(1):null}
function publicHref(href){const url=new URL(href,location.href);url.searchParams.delete('rt_shell');return url.href}
function read(key){try{const value=JSON.parse(sessionStorage.getItem(key));return value&&Date.now()-value.at<KEEP?value:null}catch(e){return null}}
function save(key,value){try{
 sessionStorage.removeItem(key); // 新資料放不下時不可繼續保留同 key 的舊資料。
 const rows=Object.keys(sessionStorage).filter(k=>k.startsWith(PREFIX)).map(k=>({k,at:read(k)?.at||0})).sort((a,b)=>a.at-b.at);
 while(rows.length>=12)sessionStorage.removeItem(rows.shift().k);
 if(value.text.length<750000)sessionStorage.setItem(key,JSON.stringify(value));
}catch(e){/* 存滿、停用儲存時照常走網路。 */}}
function response(value,cached){return new Response(value.text,{status:200,headers:{'Content-Type':'application/json','X-RT-Saved-At':String(value.at),'X-RT-Cached':cached?'1':'0'}})}
function badge(at){if(!document.body)return;let node=document.getElementById('rt-cache-note');if(!node){node=document.createElement('div');node.id='rt-cache-note';node.setAttribute('role','status');node.style.cssText='position:fixed;bottom:8px;left:50%;transform:translateX(-50%);z-index:120;background:#29364b;color:#fff;padding:6px 12px;border-radius:8px;font:13px system-ui;max-width:90vw';document.body.append(node)}node.textContent='先顯示 '+Math.max(1,Math.round((Date.now()-at)/60000))+' 分鐘前的資料，正在更新';}
function hideBadge(){document.getElementById('rt-cache-note')?.remove()}
const allowed=/^\/board\/api\/(feed|usage|monitor\/series|memory-data|memory-writes|experience|review\/list)$/;
window.fetch=async(input,init={})=>{
 const req=input instanceof Request?input:null,url=new URL(req?req.url:String(input),location.href),method=String(init.method||req?.method||'GET').toUpperCase();
 if(url.origin!==location.origin)return raw(input,init);
 if(method!=='GET'){
  const r=await raw(input,init);if(r.ok){clear();emit('rt:cache-invalidated',url.pathname)}else if(r.status===401||r.status===403)clear();return r;
 }
 const headers=new Headers(init.headers||req?.headers),kind=pageKind(url.pathname);
 // 舊頁面原本只為比版號而抓整頁；維持其 meta 介面，實際只取一次小 JSON。
 if(kind&&!url.search&&!/text\/html/i.test(headers.get('Accept')||'')){
  try{const m=await versions(),p=m[edition(url.pathname)][kind];if(p)return new Response('<meta name="'+kind+'-version" content="'+p.version+'"><meta name="'+kind+'-revision" content="'+p.revision+'">',{headers:{'Content-Type':'text/html'}})}catch(e){}
  return raw(input,init);
 }
 if(!allowed.test(url.pathname))return raw(input,init);
 let meta;try{meta=await versions()}catch(e){return raw(input,init)}
 const which=edition(location.pathname),key=PREFIX+meta.session+':'+which+':'+JSON.stringify(meta[which])+':'+url.pathname+url.search;
 const cached=read(key),force=headers.get('X-RT-Fresh')==='1'||init.cache==='reload';
 const network=()=>{
  const generation=hub.generation,shared=!(init.signal||req?.signal);
  if(!force&&shared&&hub.requests.has(key))return hub.requests.get(key);
  const marker={};hub.latest.set(key,marker);hub.inflight++;
  const work=raw(input,init).then(async r=>{
   if(generation!==hub.generation||hub.latest.get(key)!==marker)return Response.json({error:'資料已變更，請重試'},{status:409});
   if(!r.ok){if(r.status===401||r.status===403){clear();hub.meta=null;hub.metaAt=0}return r}
   if(!/json/i.test(r.headers.get('Content-Type')||''))return r;
   const text=await r.clone().text();let json;try{json=JSON.parse(text)}catch(e){return r}if(json?.error)return r;
   if(generation!==hub.generation||hub.latest.get(key)!==marker)return Response.json({error:'資料已變更，請重試'},{status:409});
   const value={text,at:Date.now()};save(key,value);hideBadge();if(cached&&cached.text!==text)emit('rt:data-updated',url.pathname+url.search);
   return response(value,false);
  }).finally(()=>{hub.inflight--;hub.done++;if(hub.requests.get(key)===work)hub.requests.delete(key);if(hub.latest.get(key)===marker)hub.latest.delete(key)});if(shared)hub.requests.set(key,work);return work;
 };
 if(cached&&!force){hub.done++;if(Date.now()-cached.at>=FRESH){badge(cached.at);network().catch(()=>{})}else hideBadge();return response(cached,true)}
 return (await network()).clone();
};
// 原生 document.hidden 加上此頁是否藏在其他頁後面，既有計時器可沿用。
function active(value){inactive=!value;document.dispatchEvent(new Event('visibilitychange'))}
window.rtRuntime={versions,clear,active};
document.addEventListener('submit',e=>{if(/\/logout\/?$/.test(new URL(e.target.action||location.href,location.href).pathname)){clear();e.target.target='_top'}},true);
// 不重建各頁的程式狀態：首次開該頁才載入，回來直接顯示原頁，草稿和捲動位置都保留。
if(host===window){
 const initial=location.pathname,original={path:initial,title:document.title,scroll:0},frames=new Map(),TITLES={board:'公告欄',memory:'記憶',monitor:'監控',usage:'額度'};let selected=initial,container=null,preloading=true,pre=null;
 // 2.22.3 正式版外殼也預先載入（2.22.2 只限預覽外殼）。改看「外殼這一頁自己有沒有 <meta name="rt-preload">」：這個檔正式版與預覽版共用，
 // 換上去當下正式版還是舊頁（沒有這個 meta）就維持舊行為（不預載、藏法 display:none）；使用者按採用、正式版頁也帶 meta 後才一起預載。
 // 藏起來的頁用 visibility 而不是 display:none，保留尺寸，背景先建好的頁就在正確的寬度畫圖。（meta 在本檔之後才解析，所以第一次用到才判斷）
 // meta 的 content 寫要預載哪幾頁（逗號隔開，例如 "monitor,usage"）：清單跟著頁面走，按「回滾」就能退，不用動這個共用檔。
 const PRE=()=>pre??=!!document.querySelector('meta[name="rt-preload"]');
 const GAP={monitor:1500,usage:1500,memory:8000};
 function preList(){const v=(document.querySelector('meta[name="rt-preload"]')?.content||'').split(/[\s,]+/).filter(k=>GAP[k]);return v.length?v:['monitor','usage']}
 // 有還沒送出的字（留言、搜尋框、可編輯區）就不自動重載，免得草稿不見；改成在那一頁跳提示，讓使用者先複製再按重新載入。
 function draft(doc){try{return [...doc.querySelectorAll('textarea,input:not([type]),input[type=text],input[type=search],input[type=url],input[type=email],[contenteditable="true"],[contenteditable=""]')].some(el=>!el.disabled&&!el.readOnly&&(el.isContentEditable?el.textContent.trim()!=='':el.value.trim()!==''&&el.value!==el.defaultValue))}catch(e){return false}}
 function staleNote(doc,reload){if(!doc||!doc.body||doc.getElementById('rt-stale-note'))return;const n=doc.createElement('div');n.id='rt-stale-note';n.setAttribute('role','alert');n.style.cssText='position:fixed;bottom:8px;left:50%;transform:translateX(-50%);z-index:130;background:#5a3a12;color:#fff;padding:8px 12px;border-radius:8px;font:14px system-ui;max-width:92vw;display:flex;gap:10px;align-items:center';const t=doc.createElement('span');t.textContent='這頁有新版了。你還有沒送出的字，先複製起來，再按「重新載入」。';const b=doc.createElement('button');b.type='button';b.textContent='重新載入';b.style.cssText='font:inherit;padding:4px 10px;border-radius:6px;border:0;cursor:pointer';b.addEventListener('click',reload);n.append(t,b);doc.body.append(n)}
 // 外框一建好就依目前狀態藏起來：預載時使用者還停在外殼自己的頁，外框（fixed、不透明底）不能蓋住它。
 function ensureContainer(){if(container)return container;container=document.createElement('div');container.id='rt-page-host';container.style.cssText='position:fixed;inset:0;z-index:110;background:var(--bg,#1e2128)';container.hidden=selected===initial;document.body.append(container);const style=document.createElement('style');style.textContent='body.rt-soft-nav>:not(#rt-page-host){display:none!important}'+(PRE()?'#rt-page-host[hidden]{display:block!important;visibility:hidden!important;pointer-events:none}#rt-page-host iframe[hidden]{display:block!important;visibility:hidden!important;position:absolute;inset:0;pointer-events:none}':'#rt-page-host[hidden],#rt-page-host iframe[hidden]{display:none!important}');document.head.append(style);history.replaceState({...history.state,rtPage:initial},'',location.href);return container}
 function createFrame(path,hidden){
  const frame=document.createElement('iframe');frame.title=TITLES[pageKind(path)];frame.style.cssText='border:0;width:100%;height:100%;display:block';frame.hidden=hidden;frame.src=new URL(path+(pageKind(path)==='memory'?'?rt_shell=1':''),location.href).href;frames.set(path,frame);ensureContainer().append(frame);
  frame.addEventListener('load',()=>{const current=selected===path;let win=null,ok=false,target=new URL(path,location.href).href;
   try{win=frame.contentWindow;ok=!!win.rtRuntime&&edition(win.location.pathname)===edition(initial);target=publicHref(win.location.href)}catch(e){}
   // 登入過期、錯誤頁、換了版：正在看的那頁照舊整個跟著跳；背景預載的頁只拆掉、不再預載，不准把使用者正在看的畫面帶走。
   if(!ok){if(current){location.assign(target);return}frame.remove();frames.delete(path);preloading=false;return}
   frame.dataset.rtLoaded='1';
   win.rtRuntime.active(current);if(current){document.title=frame.contentDocument.title;frame.focus()}});
  return frame;
 }
 // 預載的頁可能比線上舊（中途按了「採用」「回滾」，只有按的那一頁自己重載）：比對「外殼這一版」（正式或預覽）的版次，不一樣就重載那一頁。
 // 兩個時機：切過去時；任何一頁採用／回滾成功（runtime 清掉版本快取並送 rt:cache-invalidated）時，背景藏著的頁也一起比、舊的先在背景重載。
 // 有草稿的頁不重載：藏著時只略過（切過去時會再比一次），正在看時跳提示。
 function stale(frame,path){versions().then(m=>{const k=pageKind(path),want=m[edition(initial)]?.[k]?.revision;let have=null,doc=null;try{doc=frame.contentDocument;have=doc?.querySelector('meta[name="'+k+'-revision"]')?.content}catch(e){}
  if(want==null||!have||String(want)===String(have)||frames.get(path)!==frame)return;
  const reload=()=>{delete frame.dataset.rtLoaded;frame.contentWindow.location.reload()};
  if(!draft(doc))reload();else if(selected===path)staleNote(doc,reload)}).catch(()=>{})}
 // 外殼自己那一頁不在 frames 裡：切回外殼時也比版次，舊了就重載（有草稿就提示）；不然送出會被伺服器以版本不符擋下。
 function selfStale(){versions().then(m=>{const k=pageKind(initial),want=m[edition(initial)]?.[k]?.revision,have=document.querySelector('meta[name="'+k+'-revision"]')?.content;
  if(want==null||!have||String(want)===String(have)||selected!==initial)return;
  if(draft(document))staleNote(document,()=>location.reload());else location.reload()}).catch(()=>{})}
 addEventListener('rt:cache-invalidated',e=>{if(!PRE())return;if(!/^\/board\/(?:preview\/)?(?:promote|rollback)/.test(String(e.detail||'')))return;setTimeout(()=>{for(const [path,frame] of frames)if(path!==selected&&frame.dataset.rtLoaded)stale(frame,path)},1500)});
 function navigate(href,push=true){
  const url=new URL(href,location.href);if(url.origin!==location.origin||!pageKind(url.pathname)||edition(url.pathname)!==edition(initial)||url.search)return false;
  if(url.pathname===selected){if(url.hash)location.hash=url.hash;return true}
  ensureContainer();
  if(selected===initial){original.scroll=scrollY;original.title=document.title}
  for(const [path,frame] of frames){if(path===url.pathname)continue;frame.hidden=true;try{frame.contentWindow.rtRuntime?.active(false)}catch(e){}}
  selected=url.pathname;active(selected===initial);container.hidden=selected===initial;document.body.classList.toggle('rt-soft-nav',selected!==initial);
  if(selected===initial){document.title=original.title;scrollTo(0,original.scroll);if(PRE())selfStale()}else{
   const cached=frames.get(selected),frame=cached||createFrame(selected,false);
   frame.hidden=false;try{const win=frame.contentWindow;if(win.rtRuntime){win.rtRuntime.active(true);document.title=frame.contentDocument.title}}catch(e){}frame.focus();
   if(cached&&PRE())stale(cached,selected);
  }
  if(push)history.pushState({...history.state,rtPage:url.pathname},'',url.href);return true;
 }
 // 預先載入：外殼畫完、瀏覽器閒下來之後，把其他頁先在背景建好（藏著、計時器不跑），之後切過去直接顯示，不用從頭載入。
 // 一頁一頁錯開（等上一頁 load → 再閒置 → 再隔一段）；省流量模式、記憶體小的裝置、分頁在背景時不做。
 // 公告欄永遠不預載（500 則留言、2 萬多個節點、0.4–0.6 秒長任務）：直接開監控／額度／記憶時，佇列只剩另外兩頁，不會補上公告欄。
 // 要預載哪幾頁看 meta 清單（見上）；記憶頁首輪資料最大（後端每次整批撈共用記憶），清單有列才預載、排最後、前面多等 8 秒，
 // 而且裝置記憶體要讀得到、至少 4GB（iPhone Safari、Firefox 讀不到就不預載它）。觸控小螢幕（手機）三頁都不預載。
 const idle=fn=>window.requestIdleCallback?requestIdleCallback(fn,{timeout:4000}):setTimeout(fn,1500);
 function preload(){
  const con=navigator.connection,mem=navigator.deviceMemory,phone=matchMedia('(pointer:coarse)').matches&&Math.min(innerWidth,screen.width||innerWidth)<768;
  if(!PRE()||!pageKind(initial)||(con&&con.saveData)||mem<2||phone)return;
  const base=edition(initial)==='preview'?'/board/preview':'/board';
  const queue=preList().filter(k=>k!==pageKind(initial)&&!(k==='memory'&&!(mem>=4))).map(k=>({path:base+'/'+k,gap:GAP[k]}));
  let i=0;const step=()=>{if(!preloading||i>=queue.length)return;
   if(document.hidden){document.addEventListener('visibilitychange',()=>idle(step),{once:true});return}
   const {path}=queue[i++];if(frames.has(path)){step();return}
   const next=queue[i];createFrame(path,true).addEventListener('load',()=>idle(()=>setTimeout(step,next?next.gap:0)),{once:true})};
  idle(step);
 }
 window.rtNavigate=navigate;
 // 等外殼自己的第一批資料抓完、畫好（例如公告欄留言）才開始預載，不跟它搶頻寬；最多等 15 秒。
 function settled(fn){const t0=Date.now(),chk=()=>{if((hub.done>0&&hub.inflight===0)||Date.now()-t0>15000)idle(fn);else setTimeout(chk,250)};chk()}
 const go=()=>{if(PRE())settled(preload)};
 if(document.readyState==='complete')go();else addEventListener('load',go,{once:true});
 addEventListener('popstate',()=>{if(!navigate(location.href,false))location.reload()});
}
document.addEventListener('click',e=>{
 if(e.defaultPrevented||e.button!==0||e.ctrlKey||e.metaKey||e.shiftKey||e.altKey)return;
 const link=e.target.closest('a[href]');if(!link||link.target||link.hasAttribute('download'))return;
 const url=new URL(link.href,location.href);if(url.origin===location.origin&&url.pathname===location.pathname&&url.search===location.search)return;
 const root=window.frameElement?parent:window;
 if(pageKind(url.pathname)&&!url.hash&&!url.search&&edition(url.pathname)===edition(location.pathname)&&root.rtNavigate?.(url.href)){e.preventDefault();e.stopImmediatePropagation();return}
 // 帳號、完整版本頁與跨正式／預覽屬一般導覽，離開內層頁面並保留真正的目的地。
 if(window.frameElement&&/^https?:$/.test(url.protocol)){e.preventDefault();e.stopImmediatePropagation();root.location.assign(publicHref(url.href))}
},true);
})();
