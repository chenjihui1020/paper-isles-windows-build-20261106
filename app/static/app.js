'use strict';
/* Paper Isles 0.3: offline-first, no external scripts. Two PDF modes share the same native annotations. */
const $ = s => document.querySelector(s);
const $$ = s => Array.from(document.querySelectorAll(s));
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt = n => Number(n||0).toLocaleString('zh-CN');
const clamp = (v,a,b) => Math.max(a,Math.min(b,v));
const pause = n => new Promise(r=>setTimeout(r,n));
let lib={folders:[],documents:[],features:{engine:false,pairs:[]}},view='all',folderId=null,active=null;
let readerPage=1,readerMode='continuous',zoom=100,tool='pan',tab='quote';
let pageCache=new Map(),frames=new Map(),thumbObserver=null,pageObserver=null,scrollerTimer=null,currentRender=0;
let selectedText=null,draw=null,autoFitApplied=false,undoStack=[],redoStack=[],noteItems=[],noteTimer=null,compassTimer=null;
let translating=false,fullPoll=null,listMode=false,motionOn=true,focusOn=false,notesHidden=false,thumbsHidden=false;
let voyage={duration:25*60,remaining:25*60,playing:false,startAt:0,interval:null};
const api={
 async get(url){const res=await fetch(url,{cache:'no-store'});const x=await res.json();if(!res.ok||x.error)throw Error(x.error||'请求失败');return x;},
 async post(url,data={}){const res=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json','X-Paper-Isles':'local-client'},body:JSON.stringify(data)});const x=await res.json();if(!res.ok||x.error)throw Error(x.error||'保存失败');return x;}
};
function pref(k,v){if(v===undefined){try{return localStorage.getItem(k)}catch{return null}}try{localStorage.setItem(k,v)}catch{}}
function toast(msg){const el=$('#toast');el.textContent=msg;el.classList.remove('hidden');clearTimeout(toast.timer);toast.timer=setTimeout(()=>el.classList.add('hidden'),4700)}
function fail(e){console.error(e);toast('未完成：'+(e?.message||e));}
function dialog(title,description,content='',buttons=[{text:'取消',cancel:true},{text:'确认',primary:true}],confirm=()=>true){
 $('#modalEyebrow').textContent='A LITTLE HARBOR · 纸屿';$('#modalTitle').textContent=title;$('#modalDesc').textContent=description||'';
 $('#modalContent').innerHTML=content;$('#modalActions').innerHTML=buttons.map((x,i)=>`<button class="button ${x.primary?'primary':'white'}" data-modal-button="${i}">${esc(x.text)}</button>`).join('');
 $('#modal').classList.remove('hidden');dialog.current={buttons,confirm};
}
function closeDialog(){dialog.current=null;$('#modal').classList.add('hidden')}
$('#modalClose').onclick=closeDialog;
$('#modal').onclick=e=>{if(e.target===$('#modal'))return closeDialog();const b=e.target.closest('[data-modal-button]');if(!b||!dialog.current)return;const obj=dialog.current.buttons[Number(b.dataset.modalButton)];if(obj.cancel)return closeDialog();const f=dialog.current.confirm;Promise.resolve().then(f).then(ok=>{if(ok!==false)closeDialog()}).catch(fail)};
function folderName(id){return lib.folders.find(x=>x.id===id)?.name||'未分类'}
function folderOptions(current=null){return '<option value="">未分类</option>'+lib.folders.filter(x=>!x.deleted_at).map(x=>`<option value="${esc(x.id)}" ${x.id===current?'selected':''}>${esc(x.name)}</option>`).join('')}
function folderHTML(parent=null,level=0,seen=new Set()){
 if(level>14)return '';
 return lib.folders.filter(x=>!x.deleted_at&&(x.parent_id||null)===parent&&!seen.has(x.id)).map(x=>{
  const kids=lib.folders.some(y=>y.parent_id===x.id&&!y.deleted_at),marked=view==='folder'&&folderId===x.id;
  return `<div class="folder-entry"><button class="folder-item ${marked?'selected':''}" data-folder="${esc(x.id)}"><span class="folder-icon">${kids?'▤':'▱'}</span><span class="folder-name">${esc(x.name)}</span></button><button class="folder-dots" data-folder-menu="${esc(x.id)}" title="重命名或删除空文件夹">···</button></div>`+(kids?`<div class="folder-nest">${folderHTML(x.id,level+1,new Set([...seen,x.id]))}</div>`:'');
 }).join('');
}
function renderNav(){
 $('#folderTree').innerHTML=folderHTML();$('#allCount').textContent=lib.documents.filter(x=>!x.deleted_at).length;$('#trashCount').textContent=lib.documents.filter(x=>x.deleted_at).length;
 $$('.nav-link').forEach(x=>x.classList.toggle('active',x.dataset.view===view));
 $('#heroDocs').textContent=lib.documents.filter(x=>!x.deleted_at).length;$('#heroFolders').textContent=lib.folders.filter(x=>!x.deleted_at).length;$('#heroRead').textContent=lib.documents.filter(x=>!x.deleted_at&&x.reading_status==='reading').length;
}
function card(d){const tags=(d.tags||[]).slice(0,2).map(x=>`<span class="tag">${esc(x)}</span>`).join('');
 return `<article class="doc-card" tabindex="0" data-id="${esc(d.id)}"><div class="doc-cover"><span class="cover-fallback">◇</span><img loading="lazy" alt="PDF封面缩略图" src="/api/doc/${encodeURIComponent(d.id)}/page/1?scale=0.45" onerror="this.style.display='none'"></div><div class="doc-content"><div class="doc-info"><span>${d.language==='zh'?'中文':d.language==='en'?'ENGLISH':'PDF'}</span><span>${esc(d.year||'年份待补')}</span><span>${fmt(d.pages)}页</span></div><h3>${esc(d.title)}</h3><span class="doc-author">${esc(d.authors||'作者待补')}</span><p class="doc-summary">${esc(d.summary||'这篇文献还没有摘要。点开它，为未来的阅读留下第一条线索。')}</p><div class="doc-foot">${tags}<span>${d.reading_status==='done'?'✓ 已读':d.reading_status==='reading'?'◴ 在读':'未开始'}</span><span>${d.favorite?'♥':''}</span><span class="doc-arrow">→</span></div></div></article>`;
}
function trashCard(d){return `<article class="doc-card" style="min-height:135px;cursor:default"><div class="doc-cover"><span class="cover-fallback">♻</span></div><div class="doc-content"><h3>${esc(d.title)}</h3><p class="doc-summary">移入回收站：${esc((d.deleted_at||'').slice(0,10))}</p><div class="doc-foot"><button class="button tiny white" data-restore="${esc(d.id)}">恢复文献</button><button class="button tiny white" data-purge="${esc(d.id)}">永久删除</button></div></div></article>`}
function filteredDocs(){let docs=lib.documents.filter(x=>view==='trash'?x.deleted_at:!x.deleted_at);
 if(view==='reading')docs=docs.filter(x=>x.reading_status==='reading');else if(view==='fav')docs=docs.filter(x=>x.favorite);else if(view==='folder')docs=docs.filter(x=>x.folder_id===folderId);
 const q=$('#search').value.trim().toLowerCase();if(q)docs=docs.filter(x=>[x.title,x.authors,x.year,x.summary,x.notebook,(x.tags||[]).join(' ')].join(' ').toLowerCase().includes(q));
 const sort=$('#sort').value;if(sort==='year')docs.sort((a,b)=>String(b.year).localeCompare(String(a.year)));else if(sort==='title')docs.sort((a,b)=>a.title.localeCompare(b.title,'zh'));else docs.sort((a,b)=>String(b.updated_at).localeCompare(String(a.updated_at)));
 return docs;
}
function renderMain(){const isSettings=view==='settings';$('#homeHero').classList.toggle('hidden',view!=='all');$('#libraryArea').classList.toggle('hidden',isSettings);$('#settingsArea').classList.toggle('hidden',!isSettings);
 if(isSettings){$('#libraryLocation').textContent=lib.library_path;$('#modelStatus').textContent=lib.features.engine?(lib.features.pairs.length?'已安装：'+lib.features.pairs.join(' · '):'翻译引擎已安装，仍需导入中英语言模型'):'翻译引擎暂未安装。基础阅读与批注可正常离线使用。';return}
 const names={all:['我的文献群岛','在纸页之间，发现新的连接。'],reading:['正在阅读','把未完成的思考留在这里。'],fav:['我的珍藏','值得再次翻开的思想。'],trash:['回收站','可以恢复；永久删除后无法撤回。']};const [title,sub]=view==='folder'?[folderName(folderId),'从这座岛屿继续探索。']:(names[view]||names.all);
 $('#sectionTitle').textContent=title;$('#sectionSubtitle').textContent=sub;$('#topLocation').textContent=view==='folder'?folderName(folderId):'你的私人阅读群岛';
 const docs=filteredDocs();$('#resultInfo').textContent=`${fmt(docs.length)} 篇文献 · ${view==='trash'?'回收站中的原文、笔记仍被保留':'本地保存，不依赖云端'}`;
 $('#cards').classList.toggle('list',listMode);$('#cards').innerHTML=docs.length?docs.map(x=>view==='trash'?trashCard(x):card(x)).join(''):`<div class="empty-state"><span class="empty-symbol">⌁</span><b>${view==='trash'?'回收站暂时是空的':'这里还没有文献停泊'}</b><p>${view==='trash'?'移入回收站的文献会暂时保留在这里。':'可以从右上角导入PDF，也可以把PDF直接拖进这片空白区域。'}</p>${view!=='trash'?'<button class="button primary" id="emptyImport">＋ 导入文献</button>':''}</div>`;
}
async function refresh(){lib=await api.get('/api/bootstrap');renderNav();renderMain();if(active){const x=lib.documents.find(y=>y.id===active.id);if(x)active=x}}
function setView(v,id=null){view=v;folderId=id;renderNav();renderMain();$('#main').scrollTop=0;window.scrollTo(0,0)}
$$('.nav-link').forEach(x=>x.onclick=()=>setView(x.dataset.view));$('#brandBtn').onclick=()=>setView('all');
$('#folderTree').onclick=e=>{const menu=e.target.closest('[data-folder-menu]'),f=e.target.closest('[data-folder]');if(menu)manageFolder(menu.dataset.folderMenu);else if(f)setView('folder',f.dataset.folder)};
$('#search').oninput=renderMain;$('#sort').onchange=renderMain;$('#toggleLayout').onclick=()=>{listMode=!listMode;$('#toggleLayout').textContent=listMode?'▦ 网格视图':'▥ 卡片视图';renderMain()};
function manageFolder(id){const f=lib.folders.find(x=>x.id===id);if(!f)return;
 dialog('整理这座岛屿','可以重命名文件夹；只有空文件夹才能删除，避免误删其中的文献。',`<label>岛屿名称</label><input id="folderRename" value="${esc(f.name)}"><button class="button white" id="folderRemove">删除空文件夹</button>`,[{text:'取消',cancel:true},{text:'保存新名称',primary:true}],async()=>{const name=$('#folderRename').value.trim();if(!name)return false;await api.post(`/api/folder/${id}/rename`,{name});await refresh();toast('名称已保存')});
 $('#folderRemove').onclick=async()=>{try{await api.post(`/api/folder/${id}/remove`);closeDialog();if(view==='folder'&&folderId===id)setView('all');await refresh();toast('空文件夹已移除')}catch(e){fail(e)}};
}
function newFolder(){dialog('新建一座岛屿','按课程、研究项目或任何主题收纳文献。',`<label>岛屿名称</label><input id="newFolderName" placeholder="例如：传播与社会" maxlength="100"><label>上级文件夹</label><select id="newFolderParent">${folderOptions(view==='folder'?folderId:null)}</select>`,[{text:'暂不创建',cancel:true},{text:'建立岛屿',primary:true}],async()=>{const name=$('#newFolderName').value.trim();if(!name)return toast('请输入文件夹名称'),false;const r=await api.post('/api/folder',{name,parent_id:$('#newFolderParent').value||null});await refresh();setView('folder',r.id);toast('一座新的知识岛屿已经出现 ✦')})}
$('#folderBtn').onclick=newFolder;$('#coverFolder').onclick=newFolder;
function doImport(){$('#pdfPicker').value='';$('#pdfPicker').click()}
$('#importBtn').onclick=doImport;$('#coverImport').onclick=doImport;
async function importFiles(files){if(!files||!files.length)return;let added=0,duplicated=0;
 for(const file of Array.from(files)){if(!/\.pdf$/i.test(file.name)){toast('已跳过非PDF文件：'+file.name);continue}
  try{toast(`正在导入「${file.name.slice(0,25)}…」`);const url=`/api/import?name=${encodeURIComponent(file.name)}&folder=${encodeURIComponent(view==='folder'?folderId:'')}`;
    const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/pdf','X-Paper-Isles':'local-client'},body:file});const result=await r.json();if(!r.ok||result.error)throw Error(result.error||'导入失败');if(result.duplicate)duplicated++;else added++;
  }catch(e){fail(e)}}await refresh();toast(`已导入 ${added} 篇文献${duplicated?'，跳过'+duplicated+'个重复文件':''}`)}
$('#pdfPicker').onchange=e=>importFiles(e.target.files);$('#main').ondragover=e=>{if(e.dataTransfer?.types?.includes('Files'))e.preventDefault()};$('#main').ondrop=e=>{e.preventDefault();importFiles(e.dataTransfer.files)};
$('#cards').onclick=e=>{const b=e.target.closest('[data-restore]'),x=e.target.closest('[data-purge]'),c=e.target.closest('[data-id]');if(b){api.post(`/api/doc/${b.dataset.restore}/restore`).then(()=>refresh()).then(()=>toast('已恢复原文、笔记和批注')).catch(fail);return}if(x)return confirmPermanent(x.dataset.purge);if(e.target.id==='emptyImport')return doImport();if(c)return openDoc(c.dataset.id)};
$('#cards').onkeydown=e=>{if(e.key==='Enter'&&e.target.closest('[data-id]'))openDoc(e.target.closest('[data-id]').dataset.id)};
function confirmPermanent(id){const doc=lib.documents.find(x=>x.id===id);dialog('永久删除文献？',`「${doc?.title||'该文献'}」的PDF、笔记、批注与翻译缓存都将永久删除。`, '<p>操作不可撤回；建议先做完整备份。</p>',[{text:'取消',cancel:true},{text:'仍然永久删除',primary:true}],async()=>{await api.post(`/api/doc/${id}/permanent-delete`);await refresh();toast('已永久删除')})}
function moveToTrash(id){return api.post(`/api/doc/${id}/trash`).then(()=>{if(active?.id===id)closeReader();return refresh()}).then(()=>toast('已移入回收站，可以随时恢复'))}
$('#backupBtn').onclick=$('#settingsBackup').onclick=()=>{window.open('/api/backup','_blank','noopener');toast('正在准备本地完整备份…')};
$('#importModelBtn').onclick=()=>{$('#modelPicker').value='';$('#modelPicker').click()};
$('#modelPicker').onchange=async e=>{const file=e.target.files?.[0];if(!file)return;try{toast('正在安装模型…');const r=await fetch('/api/translate/model',{method:'POST',headers:{'X-Paper-Isles':'local-client'},body:file});const x=await r.json();if(!r.ok||x.error)throw Error(x.error||'模型安装失败');await refresh();toast('本地语言模型安装成功')}catch(err){fail(err)}};
/* Distinct preferences: ambience turns off decoration; focus hides sidebars during reading. */
function applyMotion(){document.body.classList.toggle('motion-off',!motionOn);$('#motionBtn').classList.toggle('is-ambient-on',motionOn);$('#motionBtn').innerHTML=motionOn?'◉ <span>暂停海岸动画</span>':'◌ <span>开启海岸动画</span>';$('#motionBtn').setAttribute('aria-pressed',String(motionOn));pref('paper_isles_motion',motionOn?'1':'0')}
$('#motionBtn').onclick=()=>{motionOn=!motionOn;applyMotion()};motionOn=pref('paper_isles_motion')!=='0';applyMotion();
/* ---------------------- Document reader ---------------------- */
function infoFromActive(){if(!active)return;$('#readerTitle').textContent=active.title;$('#readerMeta').textContent=`${active.authors||'作者待补'} · ${active.year||'年份待补'} · ${fmt(active.pages)}页 · ${folderName(active.folder_id)}`;$('#favoriteBtn').textContent=active.favorite?'♥ 已收藏':'♡ 收藏'}
async function openDoc(id){const item=lib.documents.find(x=>x.id===id&&!x.deleted_at);if(!item)return;
 active=item;readerPage=clamp(item.read_page||1,1,item.pages);zoom=100;autoFitApplied=false;readerMode=pref('paper_isles_reader_mode')||'continuous';tab='quote';pageCache.clear();frames.clear();undoStack=[];redoStack=[];selectedText=null;currentRender++;
 $('#reader').classList.remove('hidden');document.body.style.overflow='hidden';infoFromActive();focusOn=false;notesHidden=false;thumbsHidden=false;
 $('#editTitle').value=item.title||'';$('#editAuthors').value=item.authors||'';$('#editYear').value=item.year||'';$('#editSummary').value=item.summary||'';
 $('#notebook').value=item.notebook||'';$('#quoteOriginal').value='';$('#thoughtEntry').value='';$('#thoughtPage').value=readerPage;$('#quotePage').value=readerPage;
 $('#numPages').textContent=item.pages;$('#pageNumber').max=item.pages;$('#pageNumber').value=readerPage;$('#zoom').value=100;$('#zoomNumber').textContent='100%';$('#pdfSaved').textContent='原始PDF保留不变；所有批注写入本地工作副本。';$('#pdfSearch').value='';
 $('#originalText').textContent='';$('#translatedText').textContent='本页尚未翻译。';$('#translationStatus').textContent='';
 setTool('pan');setTab('quote');applyReaderLayout();updateUndoButtons();
 await api.post(`/api/doc/${id}`,{reading_status:item.reading_status==='unread'?'reading':item.reading_status}).catch(()=>{});
 renderThumbs();setReaderMode(readerMode,true);await loadNotes();
}
function closeReader(){if(!active)return;saveCompass();saveNotebook();active=null;currentRender++;if(pageObserver)pageObserver.disconnect();if(thumbObserver)thumbObserver.disconnect();if(fullPoll)clearInterval(fullPoll);fullPoll=null;translating=false;
 $('#reader').classList.add('hidden');document.body.style.overflow='';$('#selectionBar').classList.add('hidden');$('#continuousStack').innerHTML='';$('#singleStack').innerHTML='';pageCache.clear();frames.clear();refresh().catch(fail);
}
$('#closeReader').onclick=closeReader;
function applyReaderLayout(){const el=$('#reader');el.classList.toggle('focus-mode',focusOn);el.classList.toggle('hide-notes',notesHidden);el.classList.toggle('hide-thumbs',thumbsHidden);el.classList.toggle('show-notes',!notesHidden);el.classList.toggle('show-thumbs',!thumbsHidden);
 $('#focusBtn').textContent=focusOn?'⛶ 退出专注':'⛶ 专注阅读';$('#noteToggle').textContent=notesHidden?'▤ 显示笔记':'▤ 笔记';$('#thumbToggle').textContent=thumbsHidden?'▦ 显示缩略图':'▦ 缩略图';
 setTimeout(()=>{applyZoom();positionCurrentIfVisible();},140)}
$('#focusBtn').onclick=()=>{focusOn=!focusOn;applyReaderLayout()};$('#noteToggle').onclick=()=>{focusOn=false;notesHidden=!notesHidden;applyReaderLayout()};$('#thumbToggle').onclick=()=>{focusOn=false;thumbsHidden=!thumbsHidden;applyReaderLayout()};
$('#favoriteBtn').onclick=async()=>{if(!active)return;try{active=await api.post(`/api/doc/${active.id}`,{favorite:active.favorite?0:1});infoFromActive();toast(active.favorite?'已加入珍藏':'已取消珍藏')}catch(e){fail(e)}};
$('#exportPdfBtn').onclick=()=>{if(active)window.open(`/api/doc/${active.id}/export`,'_blank','noopener')};$('#viewOriginal').onclick=()=>{if(active)window.open(`/api/doc/${active.id}/pdf`,'_blank','noopener')};
$('#manageDocBtn').onclick=()=>{if(!active)return;const id=active.id;
 dialog('整理这篇文献','修改标签、阅读进度与所属岛屿，或移入回收站。',`<label>所属岛屿</label><select id="managerFolder">${folderOptions(active.folder_id)}</select><label>阅读状态</label><select id="managerStatus"><option value="unread" ${active.reading_status==='unread'?'selected':''}>未开始</option><option value="reading" ${active.reading_status==='reading'?'selected':''}>正在阅读</option><option value="done" ${active.reading_status==='done'?'selected':''}>已读完</option></select><label>主题标签（逗号分隔）</label><input id="managerTags" value="${esc((active.tags||[]).join(', '))}"><button class="button white" id="managerTrash">移入回收站</button>`,[{text:'取消',cancel:true},{text:'保存',primary:true}],async()=>{active=await api.post(`/api/doc/${id}`,{folder_id:$('#managerFolder').value||null,reading_status:$('#managerStatus').value,tags:$('#managerTags').value.split(/[,，]/).map(x=>x.trim()).filter(Boolean)});infoFromActive();toast('文献信息已保存')});
 $('#managerTrash').onclick=async()=>{closeDialog();try{await moveToTrash(id)}catch(e){fail(e)}};
};
/* thumbnail atlas: images are loaded lazily and not tied to the scrolling PDF renderer */
function renderThumbs(){if(thumbObserver)thumbObserver.disconnect();const root=$('#thumbGrid');root.innerHTML='';if(!active)return;
 const frag=document.createDocumentFragment();for(let n=1;n<=active.pages;n++){const b=document.createElement('button');b.type='button';b.className='thumb-card'+(n===readerPage?' current':'');b.dataset.page=n;b.innerHTML=`<img alt="第${n}页缩略图" loading="lazy"><span class="annot-dot"></span><small>第 ${n} 页</small>`;b.onclick=()=>jump(n);frag.appendChild(b)}root.appendChild(frag);
 thumbObserver=new IntersectionObserver(entries=>{for(const e of entries){if(!e.isIntersecting)continue;const b=e.target,img=b.querySelector('img');if(!img.src)img.src=`/api/doc/${active.id}/page/${b.dataset.page}?scale=0.20`;thumbObserver.unobserve(b)}},{root:$('.compass-scroll'),rootMargin:'160px'});
 $$('.thumb-card').forEach(el=>thumbObserver.observe(el));highlightThumb();
}
function highlightThumb(){for(const x of $$('.thumb-card'))x.classList.toggle('current',Number(x.dataset.page)===readerPage)}
$('#thumbTop').onclick=()=>{$('.compass-scroll').scrollTo({top:0,behavior:'smooth'})};
/* PDF layout: creates lightweight placeholders for all pages; loads actual page images/text near viewport only. */
function desiredPageWidth(w=595){return clamp(w*1.18*zoom/100,240,1680)}
function makeSheet(n,mode='continuous'){
 const el=document.createElement('div');el.className='pdf-sheet';el.dataset.page=n;el.dataset.mode=mode;el.style.width=desiredPageWidth()+'px';el.style.aspectRatio='595 / 842';
 el.innerHTML=`<div class="page-loading">第 ${n} 页 · 靠近时自动加载</div><img alt="PDF第${n}页"><div class="text-layer" role="document" aria-label="第${n}页可选择文字"></div><canvas class="draw-layer"></canvas><span class="page-edge">${n}</span>`;
 const can=el.querySelector('canvas');can.addEventListener('pointerdown',onDrawDown);can.addEventListener('pointermove',onDrawMove);can.addEventListener('pointerup',onDrawUp);can.addEventListener('pointercancel',onDrawCancel);
 el.addEventListener('mouseup',onTextMouseUp);el.addEventListener('touchend',onTextMouseUp);el.addEventListener('click',onPdfTextEditClick);return el;
}
function visibleModeStack(){return readerMode==='continuous'?$('#continuousStack'):$('#singleStack')}
function setReaderMode(mode,initial=false){if(!active)return;readerMode=mode==='paged'?'paged':'continuous';pref('paper_isles_reader_mode',readerMode);$$('[data-mode]').forEach(x=>x.classList.toggle('active',x.dataset.mode===readerMode));
 $('#continuousStack').classList.toggle('hidden',readerMode!=='continuous');$('#singleStack').classList.toggle('hidden',readerMode!=='paged');$('#selectionBar').classList.add('hidden');
 if(pageObserver)pageObserver.disconnect();const serial=++currentRender;
 if(readerMode==='continuous'){
  const stack=$('#continuousStack');if(!stack.childElementCount){const frag=document.createDocumentFragment();for(let n=1;n<=active.pages;n++){const sheet=makeSheet(n);frames.set(n,sheet);frag.appendChild(sheet)}stack.appendChild(frag)}
  pageObserver=new IntersectionObserver(entries=>{if(serial!==currentRender||!active)return;for(const entry of entries){if(entry.isIntersecting)loadSheet(entry.target)}},{root:$('#canvasWrap'),rootMargin:'800px 0px'});
  for(const el of stack.children)pageObserver.observe(el);
  requestAnimationFrame(()=>{const f=frames.get(readerPage);if(f)f.scrollIntoView({block:'start'});setTimeout(syncPageFromScroll,120)});
 }else{
  $('#singleStack').replaceChildren();const sheet=makeSheet(readerPage,'paged');$('#singleStack').appendChild(sheet);loadSheet(sheet);$('#canvasWrap').scrollTop=0;
 }
 setTool(tool);if(!initial)toast(readerMode==='continuous'?'已切换至连续滚动阅读':'已切换至逐页精细批注');
}
$$('[data-mode]').forEach(b=>b.onclick=()=>setReaderMode(b.dataset.mode));
async function fetchPage(n){if(!active)return null;const key=active.id+'@'+n;if(pageCache.has(key))return pageCache.get(key);
 const value=await api.get(`/api/doc/${active.id}/text?page=${n}`);pageCache.set(key,value);return value;
}
async function loadSheet(el){if(!active||el.dataset.loading==='1')return;const n=Number(el.dataset.page),docid=active.id;
 const img=el.querySelector('img');if(el.dataset.loaded==='1'){if(!img.getAttribute('src'))img.src=`/api/doc/${docid}/page/${n}?scale=2.05`;return}
 el.dataset.loading='1';el.querySelector('.page-loading').textContent='PDF第 '+n+' 页正在抵达…';
 try{const d=await fetchPage(n);if(!active||active.id!==docid)return;
  el.dataset.w=d.width;el.dataset.h=d.height;el.style.aspectRatio=`${d.width} / ${d.height}`;el.style.width=desiredPageWidth(d.width)+'px';
  img.onload=()=>{el.querySelector('.page-loading')?.remove();fitCanvas(el)};
  img.onerror=()=>{el.querySelector('.page-loading').textContent='本页图像加载失败'};
  img.src=`/api/doc/${docid}/page/${n}?scale=2.05&v=${Date.now()}`;
  buildTextLayer(el,d);fitCanvas(el);el.dataset.loaded='1';
  if(!autoFitApplied && n===readerPage){
    autoFitApplied=true;
    const space=Math.max(280,$('#canvasWrap').clientWidth-75);
    zoom=clamp(Math.round((space/(d.width*1.18)*100)/5)*5,75,175);
    $('#zoom').value=zoom;
    requestAnimationFrame(applyZoom);
  }
  if(d.scanned||d.suspicious){el.title='本页文字层可能无法准确识别，可使用「识别本页」尝试本机OCR';}
 }catch(e){el.querySelector('.page-loading').textContent='页面读取失败 · '+e.message;console.warn(e)}finally{el.dataset.loading='0'}
}
function buildTextLayer(el,d){const layer=el.querySelector('.text-layer');if(!layer)return;layer.replaceChildren();const frag=document.createDocumentFragment();const scale=el.clientWidth/d.width || 1;
 for(const [x0,y0,x1,y1,word] of d.words||[]){if(!word?.trim())continue;const span=document.createElement('span');span.className='pdf-word';span.textContent=word+' ';span.style.left=(100*x0/d.width)+'%';span.style.top=(100*y0/d.height)+'%';span.style.width=Math.max(0,100*(x1-x0)/d.width)+'%';span.style.height=Math.max(0,100*(y1-y0)/d.height)+'%';span.style.fontSize=Math.max(3,(y1-y0)*scale*.82)+'px';span.dataset.txt=word;span.dataset.x=((x0+x1)/2).toFixed(3);span.dataset.y=((y0+y1)/2).toFixed(3);frag.appendChild(span)}layer.appendChild(frag);
}
function fitCanvas(el){const cv=el.querySelector('canvas');const w=Math.max(1,Math.round(el.clientWidth)),h=Math.max(1,Math.round(el.clientHeight));if(cv.width!==w||cv.height!==h){cv.width=w;cv.height=h}}
function applyZoom(){for(const el of $$('.pdf-sheet')){const w=Number(el.dataset.w)||595,h=Number(el.dataset.h)||842;el.style.width=desiredPageWidth(w)+'px';el.style.aspectRatio=`${w} / ${h}`;fitCanvas(el);const d=pageCache.get(active?.id+'@'+el.dataset.page);if(d&&el.dataset.loaded==='1')buildTextLayer(el,d)}$('#zoomNumber').textContent=zoom+'%'}
$('#zoom').oninput=e=>{zoom=Number(e.target.value);applyZoom()};$('#fitWidth').onclick=()=>{const space=$('#canvasWrap').clientWidth-65;zoom=clamp(Math.round(space/(595*1.18)*100/5)*5,75,175);$('#zoom').value=zoom;applyZoom();toast('已按阅读区域适配PDF宽度')};
function syncPageFromScroll(){if(!active||readerMode!=='continuous')return;const root=$('#canvasWrap').getBoundingClientRect();let best=readerPage,score=Infinity;
 for(const x of frames.values()){const box=x.getBoundingClientRect();const y=box.top-root.top;if(y>root.height*1.4||box.bottom<root.top-30)continue;const s=Math.abs(y-25);if(s<score){score=s;best=Number(x.dataset.page)}}if(best!==readerPage)setCurrentPage(best,false);
}
function setCurrentPage(n,save=true){if(!active)return;readerPage=clamp(Number(n)||1,1,active.pages);$('#pageNumber').value=readerPage;$('#quotePage').value=readerPage;$('#thoughtPage').value=readerPage;highlightThumb();
 if(save){const id=active.id;clearTimeout(scrollerTimer);scrollerTimer=setTimeout(()=>{if(active?.id===id)api.post(`/api/doc/${id}`,{read_page:readerPage}).catch(()=>{})},280)}
 if(tab==='translate')showCachedTranslation().catch(()=>{});
}
async function jump(n){if(!active)return;setCurrentPage(n,true);if(readerMode==='continuous'){const el=frames.get(readerPage);if(el){el.scrollIntoView({block:'start',behavior:pref('paper_isles_motion')==='0'?'auto':'smooth'});loadSheet(el)}}else{setReaderMode('paged',true)}
}
function positionCurrentIfVisible(){if(active&&readerMode==='paged')applyZoom()}
$('#prevPage').onclick=()=>jump(readerPage-1);$('#nextPage').onclick=()=>jump(readerPage+1);$('#pageNumber').onchange=e=>jump(e.target.value);
$('#canvasWrap').addEventListener('scroll',()=>{if(!active||readerMode!=='continuous')return;clearTimeout(syncPageFromScroll.t);syncPageFromScroll.t=setTimeout(()=>{syncPageFromScroll();setCurrentPage(readerPage,true)},130)},{passive:true});
/* tools and native annotations */
function setTool(name){tool=name;$$('[data-tool]').forEach(b=>b.classList.toggle('active',b.dataset.tool===name));for(const el of $$('.pdf-sheet')){el.classList.toggle('ink-on',name==='ink');el.classList.toggle('rect-on',name==='rect');el.classList.toggle('pdf-edit-on',name==='text-edit')}
 const hint={pan:'选词、阅读或从工具栏切换批注模式。', 'text-edit':'点击 PDF 中需要修改的一行文字。只修改工作副本，原始文献不变。', 'text-highlight':'选择文字后将高亮自动贴合文字位置。',rect:'拖动矩形高亮图表、扫描页或任意区域。', 'text-underline':'选择文字后自动沿文字位置添加下划线。',ink:'在PDF上拖动手写，支持颜色、粗细与透明度。'};
 $('#pdfSaved').textContent=hint[name]||'';
}
$('#toolSet').onclick=e=>{const b=e.target.closest('[data-tool]');if(b)setTool(b.dataset.tool)};
$('#markWidth').oninput=()=>{$('#markWidthValue').textContent=$('#markWidth').value};$('#markOpacity').oninput=()=>{$('#markOpacityValue').textContent=$('#markOpacity').value+'%'};
function annSettings(){return{color:$('#markColor').value,width:Number($('#markWidth').value),opacity:Number($('#markOpacity').value)/100}}
function pdfCoords(el,event){const r=el.getBoundingClientRect(),w=Number(el.dataset.w)||595,h=Number(el.dataset.h)||842;return [clamp((event.clientX-r.left)/r.width*w,0,w),clamp((event.clientY-r.top)/r.height*h,0,h)].map(x=>Math.round(x*100)/100)}
function onDrawDown(e){const el=e.currentTarget.closest('.pdf-sheet');if(!active||!['ink','rect'].includes(tool)||!el)return;e.preventDefault();$('#selectionBar').classList.add('hidden');const pt=pdfCoords(el,e);draw={el,pts:[pt],page:Number(el.dataset.page),tool};e.currentTarget.setPointerCapture(e.pointerId);drawPreview()}
function onDrawMove(e){if(!draw||e.currentTarget.closest('.pdf-sheet')!==draw.el)return;draw.pts.push(pdfCoords(draw.el,e));if(draw.pts.length%2===0)drawPreview()}
function onDrawCancel(){if(!draw)return;const el=draw.el;draw=null;const cv=el.querySelector('canvas');cv.getContext('2d').clearRect(0,0,cv.width,cv.height)}
function drawPreview(){if(!draw)return;const cv=draw.el.querySelector('canvas'),ctx=cv.getContext('2d'),w=Number(draw.el.dataset.w)||595,h=Number(draw.el.dataset.h)||842;fitCanvas(draw.el);ctx.clearRect(0,0,cv.width,cv.height);ctx.save();ctx.strokeStyle=$('#markColor').value;ctx.fillStyle=$('#markColor').value;ctx.globalAlpha=Number($('#markOpacity').value)/100;ctx.lineWidth=Number($('#markWidth').value)*cv.width/w;ctx.lineCap='round';ctx.lineJoin='round';const pts=draw.pts.map(p=>[p[0]/w*cv.width,p[1]/h*cv.height]);if(draw.tool==='ink'){ctx.beginPath();ctx.moveTo(...pts[0]);for(const p of pts.slice(1))ctx.lineTo(...p);ctx.stroke()}else{const a=pts[0],b=pts[pts.length-1];ctx.fillRect(Math.min(a[0],b[0]),Math.min(a[1],b[1]),Math.abs(a[0]-b[0]),Math.abs(a[1]-b[1]))}ctx.restore()}
async function onDrawUp(e){if(!draw||!active)return;const last=pdfCoords(draw.el,e);draw.pts.push(last);const d=draw;draw=null;const cv=d.el.querySelector('canvas');cv.getContext('2d').clearRect(0,0,cv.width,cv.height);const args={type:d.tool,page:d.page,...annSettings()};
 if(d.tool==='ink'){if(d.pts.length<2)return;args.points=d.pts}else{const a=d.pts[0],b=d.pts[d.pts.length-1];if(Math.abs(a[0]-b[0])<2||Math.abs(a[1]-b[1])<2)return;args.rect=[Math.min(a[0],b[0]),Math.min(a[1],b[1]),Math.max(a[0],b[0]),Math.max(a[1],b[1])];}
 await saveAnnotation(args);
}
async function saveAnnotation(args,recordUndo=true){if(!active)return;const id=active.id;try{$('#pdfSaved').textContent='正在写入PDF原生批注…';const result=await api.post(`/api/doc/${id}/annotate`,args);
  if(recordUndo){undoStack.push({args,xref:result.xref,marker:result.marker});redoStack=[]}updateUndoButtons();$('#pdfSaved').textContent='✓ 已保存到PDF的原生批注层';await rerenderPage(args.page);return result;
 }catch(e){fail(e);$('#pdfSaved').textContent='批注未保存；请检查PDF是否损坏或受保护';return null}}
async function rerenderPage(n){for(const el of $$('.pdf-sheet')){if(Number(el.dataset.page)!==n)continue;const img=el.querySelector('img');if(el.dataset.loaded==='1')img.src=`/api/doc/${active.id}/page/${n}?scale=2.05&t=${Date.now()}`}for(const t of $$('.thumb-card'))if(Number(t.dataset.page)===n){t.classList.add('has-annotations');const img=t.querySelector('img');if(img.getAttribute('src'))img.src=`/api/doc/${active.id}/page/${n}?scale=0.20&t=${Date.now()}`}}
function updateUndoButtons(){$('#undoAnnot').disabled=undoStack.length===0;$('#redoAnnot').disabled=redoStack.length===0}
$('#undoAnnot').onclick=async()=>{if(!active||!undoStack.length)return;const item=undoStack.pop();try{
 if(item.kind==='pdf-text-edit'){await api.post(`/api/doc/${active.id}/undo-text-edit`,{edit_id:item.editId});redoStack.push(item);await refreshEditedPdfPage(item.page);toast('已安全撤销 PDF 文字修改')}
 else{await api.post(`/api/doc/${active.id}/delete-annotation`,{page:item.args.page,xref:item.xref,marker:item.marker});redoStack.push(item.args);await rerenderPage(item.args.page);toast('已撤销上一个新增批注')}
 }catch(e){undoStack.push(item);fail(e)}updateUndoButtons()};
$('#redoAnnot').onclick=async()=>{if(!active||!redoStack.length)return;const item=redoStack.pop();try{
 if(item.kind==='pdf-text-edit'){
  const info=await api.get(`/api/doc/${active.id}/text-edit-preview?page=${item.page}&x=${item.x}&y=${item.y}`);
  if(info.text!==item.original)throw Error('原文已发生变化，无法安全重做此修改');
  const result=await api.post(`/api/doc/${active.id}/text-edit`,{page:item.page,x:item.x,y:item.y,original:item.original,replacement:item.replacement,stamp:info.stamp});
  item.editId=result.edit_id;undoStack.push(item);await refreshEditedPdfPage(item.page);toast('已重新应用 PDF 文字修改');
 }else{const result=await saveAnnotation(item,true);if(!result)redoStack.push(item)}
 }catch(e){redoStack.push(item);fail(e)}updateUndoButtons()};
$('#deleteAnnotations').onclick=async()=>{if(!active)return;const page=readerPage;try{const result=await api.get(`/api/doc/${active.id}/annotations?page=${page}`),items=result.items||[];
 dialog('管理第 '+page+' 页批注',`本页共有 ${items.length} 个原生PDF标注。删除仅影响工作副本。`,items.length?items.map(x=>`<div class="annot-row"><span>${esc(x.type)}</span><span>${esc((x.text||'').slice(0,48))}</span><button class="button tiny white" data-xref="${x.xref}">删除</button></div>`).join(''):'<p>本页尚无批注。</p>',[{text:'关闭',cancel:true}],()=>true);
 $('#modalContent').onclick=async e=>{const btn=e.target.closest('[data-xref]');if(!btn)return;try{await api.post(`/api/doc/${active.id}/delete-annotation`,{page,xref:Number(btn.dataset.xref)});undoStack=[];redoStack=[];updateUndoButtons();closeDialog();await rerenderPage(page);toast('已删除所选批注')}catch(err){fail(err)}};
 }catch(e){fail(e)}};
/* Direct PDF text editing: click a word, inspect its native text line, and
   atomically replace it in the working PDF. No WebKit text selection needed. */
async function refreshEditedPdfPage(page,id=active?.id){if(!active||active.id!==id)return;
 pageCache.delete(id+'@'+page);selectedText=null;window.getSelection()?.removeAllRanges();$('#selectionBar').classList.add('hidden');
 const matches=$$('.pdf-sheet').filter(x=>Number(x.dataset.page)===page);
 for(const el of matches){el.dataset.loaded='0';el.dataset.loading='0';el.querySelector('.text-layer')?.replaceChildren();
  if(!el.querySelector('.page-loading')){const loading=document.createElement('div');loading.className='page-loading';el.insertBefore(loading,el.firstChild)}
 }
 await Promise.all(matches.map(x=>loadSheet(x)));await rerenderPage(page);
}
async function beginPdfTextEdit(page,x,y){if(!active)return;const id=active.id;
 try{
  const info=await api.get(`/api/doc/${id}/text-edit-preview?page=${page}&x=${encodeURIComponent(x)}&y=${encodeURIComponent(y)}`);
  if(!active||active.id!==id)return;
  const content=`<label>当前 PDF 原文（第 ${info.page} 页）</label><textarea class="field" readonly rows="3">${esc(info.text)}</textarea><label>替换为（仅限一行）</label><textarea class="field" id="pdfTextReplacement" maxlength="500" rows="3">${esc(info.text)}</textarea><p class="pdf-edit-note">只修改 PDF 工作副本；系统会留存可撤销的完整备份。字号、颜色尽量匹配，特殊字体可能存在差异。扫描件、公式、混排及旋转文字不直接覆盖。</p>`;
  dialog('修改 PDF 原文','这是 PDF 页面中的真实文字修改，不是编辑阅读笔记。',content,[{text:'取消',cancel:true},{text:'修改并保存',primary:true}],async()=>{
   if(!active||active.id!==id)return false;
   const replacement=$('#pdfTextReplacement').value;
   if(replacement===info.text){toast('文字没有变化');return true}
   const result=await api.post(`/api/doc/${id}/text-edit`,{page,x,y,original:info.text,replacement,stamp:info.stamp});
   undoStack.push({kind:'pdf-text-edit',editId:result.edit_id,page,x,y,original:info.text,replacement});redoStack=[];updateUndoButtons();
   await refreshEditedPdfPage(page,id);
   $('#pdfSaved').textContent='✓ PDF 原文已修改并保存到工作副本（⌘Z 可撤销）';toast('PDF 原文已更新，原始文献已保留');
   return true;
  });
  $('#pdfTextReplacement')?.focus();
 }catch(error){fail(error)}
}
function onPdfTextEditClick(e){if(!active||tool!=='text-edit')return;
 const word=e.target.closest('.pdf-word');if(!word)return;
 const sheet=word.closest('.pdf-sheet');if(!sheet)return;
 e.preventDefault();e.stopPropagation();
 beginPdfTextEdit(Number(sheet.dataset.page),Number(word.dataset.x),Number(word.dataset.y));
}
/* A selectable word-position layer above each original PDF. Its geometry follows the PDF page. */
function onTextMouseUp(e){if(!active||['rect','ink','text-edit'].includes(tool))return;setTimeout(()=>captureSelection(e),40)}
function captureSelection(e){const sel=window.getSelection(),text=sel?.toString()?.trim();if(!text||!sel.rangeCount)return;const range=sel.getRangeAt(0),node=range.commonAncestorContainer;
 const host=node.nodeType===1?node:node.parentElement;if(!host?.closest('.text-layer'))return;
 const sheet=host.closest('.pdf-sheet');if(!sheet)return;const frameRect=sheet.getBoundingClientRect(),w=Number(sheet.dataset.w)||595,h=Number(sheet.dataset.h)||842;
 const boxes=[];for(const r of range.getClientRects()){const x0=Math.max(r.left,frameRect.left),y0=Math.max(r.top,frameRect.top),x1=Math.min(r.right,frameRect.right),y1=Math.min(r.bottom,frameRect.bottom);if(x1-x0<1||y1-y0<2)continue;boxes.push([clamp((x0-frameRect.left)/frameRect.width*w,0,w),clamp((y0-frameRect.top)/frameRect.height*h,0,h),clamp((x1-frameRect.left)/frameRect.width*w,0,w),clamp((y1-frameRect.top)/frameRect.height*h,0,h)].map(x=>Math.round(x*100)/100))}
 if(!boxes.length)return;selectedText={page:Number(sheet.dataset.page),quote:text,rects:boxes.slice(0,100)};setCurrentPage(selectedText.page,true);
 if(tool==='text-highlight'||tool==='text-underline'){
  const kind=tool;window.getSelection()?.removeAllRanges();$('#selectionBar').classList.add('hidden');saveAnnotation({page:selectedText.page,type:kind,rects:selectedText.rects,...annSettings()});return;
 }
 const bar=$('#selectionBar');bar.classList.remove('hidden');const left=clamp((e?.clientX||frameRect.left+100)-115,12,window.innerWidth-390),top=clamp((e?.clientY||frameRect.top+40)-48,10,window.innerHeight-65);bar.style.left=left+'px';bar.style.top=top+'px';
}
$('#selectionBar').onclick=async e=>{const b=e.target.closest('[data-select-action]');if(!b||!selectedText)return;const item=selectedText,a=b.dataset.selectAction;$('#selectionBar').classList.add('hidden');window.getSelection()?.removeAllRanges();
 if(a==='highlight'||a==='underline')return saveAnnotation({page:item.page,type:a==='highlight'?'text-highlight':'text-underline',rects:item.rects,...annSettings()});
 if(a==='edit'){const r=item.rects[0];return beginPdfTextEdit(item.page,(r[0]+r[2])/2,(r[1]+r[3])/2)}
 if(a==='quote'){setTab('quote');$('#quotePage').value=item.page;$('#quoteOriginal').value=item.quote;$('#quoteOriginal').focus();return toast('已填入原文摘录，请点击保存')}
 if(a==='translate'){setTab('translate');$('#originalText').textContent=item.quote;$('#translationStatus').textContent='正在进行离线选词翻译…';try{const x=await api.post('/api/translate/selection',{text:item.quote,direction:$('#langDirection').value});$('#translatedText').textContent=x.translation;$('#translationStatus').textContent='选词翻译完成'}catch(err){$('#translationStatus').textContent='翻译未完成：'+err.message;fail(err)}return}
 if(a==='copy'){try{await navigator.clipboard.writeText(item.quote);toast('原文已复制')}catch{toast('复制未成功，请重新选中文字按 ⌘C')}}
};
$('#pdfFindBtn').onclick=async()=>{if(!active)return;const q=$('#pdfSearch').value.trim();if(q.length<2)return toast('至少输入两个字符');try{const result=await api.get(`/api/doc/${active.id}/search?q=${encodeURIComponent(q)}`);const hits=result.hits||[];if(!hits.length)return toast('没有找到匹配文本，可能需要OCR');const next=hits.find(x=>x.page>=readerPage)||hits[0];await jump(next.page);toast(`找到 ${hits.length} 处匹配，已定位到第 ${next.page} 页（可继续搜索）`)}catch(e){fail(e)}};
$('#pdfSearch').onkeydown=e=>{if(e.key==='Enter')$('#pdfFindBtn').click()};
$('#ocrPageBtn').onclick=async()=>{if(!active)return;const id=active.id,p=readerPage;dialog('识别当前PDF页面？','仅在当前电脑运行OCR。扫描件、字体映射乱码的PDF可尝试；需要额外安装 Tesseract OCR 和英语语言包。',`<p>即将识别第 ${p} 页。原始PDF不会被更改。</p>`,[{text:'取消',cancel:true},{text:'开始识别',primary:true}],async()=>{closeDialog();try{toast('正在对第 '+p+' 页执行本地文字识别…');const x=await api.post(`/api/doc/${id}/ocr-page`,{page:p});pageCache.delete(id+'@'+p);for(const el of $$('.pdf-sheet'))if(Number(el.dataset.page)===p){el.dataset.loaded='0';el.dataset.loading='0';el.querySelector('.text-layer')?.replaceChildren();loadSheet(el)}toast(`本页OCR完成：${x.characters}字符，现可尝试选词与翻译`)}catch(err){fail(err)}return true})};
/* ---------------------- Reading compass and research notes ---------------------- */
function compassPayload(){return{title:$('#editTitle').value,authors:$('#editAuthors').value,year:$('#editYear').value,summary:$('#editSummary').value}}
async function saveCompass(){if(!active)return;const id=active.id;try{const u=await api.post(`/api/doc/${id}`,compassPayload());if(active?.id===id){active={...active,...u};infoFromActive();$('#compassSaved').textContent='✓ 已保存到本地'}}catch(e){$('#compassSaved').textContent='尚未保存';fail(e)}}
for(const x of $$('#editTitle,#editAuthors,#editYear,#editSummary'))x.addEventListener('input',()=>{$('#compassSaved').textContent='正在保存…';clearTimeout(compassTimer);compassTimer=setTimeout(saveCompass,750)});
$('#autoAbstract').onclick=async()=>{if(!active)return;try{const data=await api.get(`/api/doc/${active.id}/abstract`);if(!data.draft)return toast('未提取到摘要，请先检查原文文字层或使用OCR');$('#editSummary').value=data.draft;saveCompass();toast('已填入原文摘录草稿，请自行核对；不是AI分析结论')}catch(e){fail(e)}};
async function saveNotebook(){if(!active)return;const id=active.id;try{const d=await api.post(`/api/doc/${id}`,{notebook:$('#notebook').value});if(active?.id===id){active.notebook=d.notebook;$('#notesSaved').textContent='✓ 已自动保存在本地'}}catch(e){fail(e);$('#notesSaved').textContent='未保存成功'}}
$('#notebook').oninput=()=>{clearTimeout(noteTimer);$('#notesSaved').textContent='正在保存…';noteTimer=setTimeout(saveNotebook,680)};
const kindLabel={quote:'原文摘录',thought:'我的理解',question:'追问／疑惑',critique:'反驳／批评',mixed:'旧版摘录与思考'};
function noteCard(item){const type=item.kind||'mixed',isQuote=type==='quote'||type==='mixed';const val=isQuote?item.quote:item.thought;
 const other=type==='mixed'&&item.thought?`<p><strong>我的思考：</strong>${esc(item.thought)}</p>`:'';
 const link=noteItems.find(x=>x.id===item.linked_extract);const linked=link?`<div class="linked">关联原文 · ${esc((link.quote||'').slice(0,44))}…</div>`:'';
 return `<article class="note-card ${esc(type)}" data-note-id="${esc(item.id)}"><div class="note-meta"><span>${esc(kindLabel[type]||'研究笔记')}</span><span>PDF第 ${item.page} 页</span></div>${isQuote&&item.quote?`<blockquote>${esc(item.quote)}</blockquote>`:''}${!isQuote?`<p>${esc(item.thought)}</p>`:''}${other}${linked}<div class="note-actions"><button data-note-goto="${item.page}">跳到原文</button><button data-note-edit="${esc(item.id)}">编辑</button><button data-note-delete="${esc(item.id)}">删除</button></div></article>`;
}
function renderNotes(){const q=noteItems.filter(x=>x.kind==='quote'||x.kind==='mixed'),ideas=noteItems.filter(x=>['thought','question','critique'].includes(x.kind)||(x.kind==='mixed'&&x.thought));
 $('#quoteList').innerHTML=q.map(noteCard).join('')||'<p class="panel-tip">这里会保存你的原文摘录。</p>';
 $('#ideaList').innerHTML=ideas.map(noteCard).join('')||'<p class="panel-tip">这里会保存你的个人理解、追问和反驳。</p>';
 const search=$('#noteSearch').value.trim().toLowerCase(),filtered=noteItems.filter(x=>(x.quote+' '+x.thought+' '+kindLabel[x.kind]+' '+x.page).toLowerCase().includes(search));
 $('#allNotesList').innerHTML=filtered.map(noteCard).join('')||'<p class="panel-tip">没有符合条件的笔记。</p>';
 const last=$('#linkedQuote').value;$('#linkedQuote').innerHTML='<option value="">不关联</option>'+q.map(x=>`<option value="${esc(x.id)}">第 ${x.page} 页 · ${esc((x.quote||'').slice(0,35))}</option>`).join('');if(q.some(x=>x.id===last))$('#linkedQuote').value=last;
}
async function loadNotes(){if(!active)return;const id=active.id;try{const r=await api.get(`/api/doc/${id}/extracts`);if(active?.id===id){noteItems=r.items||[];renderNotes()}}catch(e){fail(e)}}
$('#noteSearch').oninput=renderNotes;
$('#addQuote').onclick=async()=>{if(!active)return;const quote=$('#quoteOriginal').value.trim();if(!quote)return toast('请先选择或输入原文摘录');try{await api.post(`/api/doc/${active.id}/extract`,{page:Number($('#quotePage').value)||readerPage,kind:'quote',quote});$('#quoteOriginal').value='';await loadNotes();toast('原文摘录已保存，可随时跳回对应页面')}catch(e){fail(e)}};
$('#addThought').onclick=async()=>{if(!active)return;const thought=$('#thoughtEntry').value.trim();if(!thought)return toast('请先写下你的思考');try{await api.post(`/api/doc/${active.id}/extract`,{page:Number($('#thoughtPage').value)||readerPage,kind:$('#thoughtKind').value,thought,linked_extract:$('#linkedQuote').value||null});$('#thoughtEntry').value='';await loadNotes();toast('新的想法已经写进研究手记')}catch(e){fail(e)}};
for(const el of [$('#quoteList'),$('#ideaList'),$('#allNotesList')])el.onclick=async e=>{const goto=e.target.closest('[data-note-goto]'),edit=e.target.closest('[data-note-edit]'),del=e.target.closest('[data-note-delete]');
 if(goto)return jump(Number(goto.dataset.noteGoto));if(edit)return editNote(edit.dataset.noteEdit);if(del)return deleteNote(del.dataset.noteDelete)};
function editNote(id){const r=noteItems.find(x=>x.id===id);if(!r)return;const isQuote=r.kind==='quote'||r.kind==='mixed';dialog('编辑阅读笔记','你可以调整页码、原文与个人思考。两类内容仍然分开保存。',`<label>类型</label><select id="editedKind"><option value="quote">原文摘录</option><option value="thought">我的理解</option><option value="question">追问／疑惑</option><option value="critique">反驳／批评</option><option value="mixed">原文 + 思考</option></select><label>PDF页码</label><input id="editedPage" type="number" min="1" value="${Number(r.page)}"><label>作者原文</label><textarea id="editedQuote">${esc(r.quote)}</textarea><label>我的思考</label><textarea id="editedThought">${esc(r.thought)}</textarea>`,[{text:'取消',cancel:true},{text:'保存修改',primary:true}],async()=>{await api.post(`/api/doc/${active.id}/edit-extract`,{id,kind:$('#editedKind').value,page:Number($('#editedPage').value),quote:$('#editedQuote').value,thought:$('#editedThought').value});await loadNotes();toast('已保存笔记修改')});$('#editedKind').value=r.kind||'mixed'}
function deleteNote(id){dialog('删除这条研究笔记？','只删除这条摘录或想法，不会改动PDF上的标注或原文。','', [{text:'取消',cancel:true},{text:'删除这条',primary:true}],async()=>{await api.post(`/api/doc/${active.id}/delete-extract`,{id});await loadNotes();toast('笔记已删除')})}
function downloadFile(text,name,mime='text/plain'){const blob=new Blob([text],{type:mime+';charset=utf-8'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=name;document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),12000)}
$('#exportMarkdown').onclick=async()=>{if(!active)return;await saveCompass();await saveNotebook();const a=active;
 let markdown=`# ${a.title}\n\n- 作者：${a.authors||'待补'}\n- 年份：${a.year||'待补'}\n- 所属岛屿：${folderName(a.folder_id)}\n\n## 摘要\n${a.summary||''}\n\n## 整篇研究手记\n${a.notebook||''}\n\n## 原文摘录与批评性思考\n`;
 for(const x of noteItems){markdown+=`\n### 第${x.page}页 · ${kindLabel[x.kind]||'笔记'}\n`;if(x.quote)markdown+=`\n> ${x.quote.replace(/\n/g,'\n> ')}\n`;if(x.thought)markdown+=`\n${x.thought}\n`}
 downloadFile(markdown,`${a.title.slice(0,35).replace(/[\\/:*?"<>|]/g,'')}_纸屿手记.md`,'text/markdown');toast('已导出Markdown笔记')};
/* ---------------------- Offline bilingual reading ---------------------- */
function setTab(next){tab=next;$$('[data-tab]').forEach(b=>b.classList.toggle('on',b.dataset.tab===next));for(const n of ['quote','idea','all','translate'])$('#'+n+'Tab').classList.toggle('hidden',next!==n);
 $('#reader').classList.toggle('bilingual',next==='translate');if(next==='translate'){showCachedTranslation().catch(()=>{});setTimeout(applyZoom,80)}}
$$('[data-tab]').forEach(b=>b.onclick=()=>setTab(b.dataset.tab));
async function showCachedTranslation(){if(!active)return;const id=active.id,p=readerPage;
 const d=await fetchPage(p);if(active?.id!==id||readerPage!==p||tab!=='translate')return;
 $('#originalText').textContent=d.text||'本页没有可提取文字，请尝试本地OCR';
 if(d.suspicious)$('#translationStatus').textContent='此页字体映射可能异常，建议先尝试OCR，再进行翻译。';
 const r=await api.get(`/api/doc/${id}/translation?page=${p}&direction=${$('#langDirection').value}`);
 if(active?.id===id&&readerPage===p&&tab==='translate')$('#translatedText').textContent=r.translation||'尚未翻译本页。点击「翻译本页」或启动全文翻译。';
}
$('#langDirection').onchange=()=>showCachedTranslation().catch(fail);
$('#translateThis').onclick=async()=>{if(!active)return;const id=active.id,page=readerPage;$('#translateThis').disabled=true;$('#translationStatus').textContent='正在本地翻译本页…';
 try{const x=await api.post(`/api/doc/${id}/translate-page`,{page,direction:$('#langDirection').value});if(active?.id===id&&readerPage===page){$('#originalText').textContent=x.original||'';$('#translatedText').textContent=x.text||'翻译结果为空';$('#translationStatus').textContent=x.cached?'✓ 已载入本机缓存译文':'✓ 本页离线翻译完成，已写入缓存'}}catch(e){$('#translationStatus').textContent='未完成：'+e.message;fail(e)}finally{$('#translateThis').disabled=false}};
$('#translateAll').onclick=async()=>{if(!active)return;const id=active.id,direction=$('#langDirection').value;
 if(translating){try{await api.post(`/api/doc/${id}/cancel-translation`,{direction});$('#translationStatus').textContent='正在停止全文翻译…';}catch(e){fail(e)}return}
 try{const j=await api.post(`/api/doc/${id}/translate-all`,{direction});translating=true;$('#translateAll').textContent='停止任务';$('#translationStatus').textContent=`正在翻译：${j.done} / ${j.total}页`;
  if(fullPoll)clearInterval(fullPoll);fullPoll=setInterval(async()=>{if(active?.id!==id){clearInterval(fullPoll);return}try{const state=await api.get(`/api/translate/status?doc=${id}&direction=${direction}`);
    $('#translationStatus').textContent=`全文翻译 ${state.done} / ${state.total} 页 · ${state.status==='running'?'正在进行':state.status}`;
    if(tab==='translate')showCachedTranslation().catch(()=>{});
    if(state.status!=='running'){clearInterval(fullPoll);fullPoll=null;translating=false;$('#translateAll').textContent='翻译全文';if(state.status==='done')toast('全文翻译完成，可逐页对照阅读')}}catch(e){clearInterval(fullPoll);fullPoll=null;translating=false;$('#translateAll').textContent='翻译全文';fail(e)}},2000);
 }catch(e){$('#translationStatus').textContent='无法开始全文翻译：'+e.message;fail(e)}};
$('#translateSelection').onclick=async()=>{if(!selectedText)return toast('请先从中间PDF页面选中文字');setTab('translate');$('#originalText').textContent=selectedText.quote;$('#translationStatus').textContent='正在离线翻译所选内容…';try{const x=await api.post('/api/translate/selection',{text:selectedText.quote,direction:$('#langDirection').value});$('#translatedText').textContent=x.translation;$('#translationStatus').textContent='选词翻译已完成'}catch(e){fail(e);$('#translationStatus').textContent=e.message}};
/* ---------------------- The little voyage ---------------------- */

/* v0.3.9: independent, time-accurate travel. Only the painted boat moves.
   The timer remains authoritative. No 350 ms hop or CSS one-second catch-up:
   when running we calculate travel on each animation frame, while paused
   we freeze at its genuine elapsed-time location. */
let voyageMotionFrame = 0;
function voyageBoatPosition(){
  const scene = $('#voyageScene');
  const boat = $('#voyageShip');
  if(!scene || !boat || !voyage.duration)return;
  const elapsed = voyage.playing
    ? (Date.now() - voyage.startAt) / 1000
    : voyage.duration - voyage.remaining;
  const progress = clamp(elapsed / voyage.duration, 0, 1);
  const distance = Math.max(0,scene.clientWidth * .66 * progress);
  boat.style.left = '8%';
  boat.style.setProperty('--voyage-distance', distance.toFixed(3) + 'px');
  boat.dataset.progress = progress.toFixed(4);
}
function voyageMotionLoop(){
  voyageBoatPosition();
  if(voyage.playing && !document.hidden){
    voyageMotionFrame = requestAnimationFrame(voyageMotionLoop);
  }else{
    voyageMotionFrame = 0;
  }
}
function syncVoyageBoatMotion(){
  if(voyage.playing){
    if(!voyageMotionFrame)voyageMotionFrame=requestAnimationFrame(voyageMotionLoop);
  }else{
    if(voyageMotionFrame)cancelAnimationFrame(voyageMotionFrame);
    voyageMotionFrame=0;
    voyageBoatPosition();
  }
}
document.addEventListener('visibilitychange',()=>{
  if(!document.hidden && voyage.playing)syncVoyageBoatMotion();
});
window.addEventListener('resize',()=>{
  if(!voyage.playing)voyageBoatPosition();
});

function clockDisplay(){if(voyage.playing){voyage.remaining=Math.max(0,voyage.duration-Math.floor((Date.now()-voyage.startAt)/1000));if(voyage.remaining===0){voyage.playing=false;clearInterval(voyage.interval);toast('已抵达今天的小岛屿 ✦')}}
 const m=Math.floor(voyage.remaining/60),s=voyage.remaining%60;$('#timerDisplay').textContent=String(m).padStart(2,'0')+':'+String(s).padStart(2,'0');syncVoyageBoatMotion();
 $('#timerToggle').textContent=voyage.playing?'暂停航行':voyage.remaining===voyage.duration?'开始航行':'继续航行';$('.voyage').classList.toggle('is-running',voyage.playing);
 if(voyage.remaining===0){$('#quietQuote').textContent='一段航程完成啦。愿下一页也有新发现。';$('#timerToggle').textContent='再次出发';}
}
$('#timerToggle').onclick=()=>{if(voyage.remaining===0)voyage.remaining=voyage.duration;
 if(voyage.playing){voyage.playing=false;clearInterval(voyage.interval)}else{voyage.playing=true;voyage.startAt=Date.now()-(voyage.duration-voyage.remaining)*1000;voyage.interval=setInterval(clockDisplay,350)}clockDisplay()};
$('#timerReset').onclick=()=>{voyage.playing=false;clearInterval(voyage.interval);voyage.remaining=voyage.duration;$('#quietQuote').textContent='慢慢读，也是在前进。';clockDisplay()};clockDisplay();
window.addEventListener('keydown',e=>{
 const command=e.metaKey||e.ctrlKey;
 const editing=e.target instanceof Element&&e.target.closest('input,textarea,select,[contenteditable=true]');
 if(command&&!e.altKey&&e.key.toLowerCase()==='o'&&!editing){e.preventDefault();doImport();return}
 if(e.key==='Escape'){if(!$('#modal').classList.contains('hidden'))return closeDialog();if(!$('#selectionBar').classList.contains('hidden'))return $('#selectionBar').classList.add('hidden');if(active)return closeReader()}
 if(!active||!$('#modal').classList.contains('hidden')||editing)return;
 if(command&&e.key.toLowerCase()==='f'){e.preventDefault();$('#pdfSearch').focus();return}
 if(command&&e.key.toLowerCase()==='z'){e.preventDefault();if(e.shiftKey)$('#redoAnnot').click();else $('#undoAnnot').click();return}
 if(e.key==='ArrowLeft'&&readerMode==='paged')jump(readerPage-1);if(e.key==='ArrowRight'&&readerMode==='paged')jump(readerPage+1);
});
window.addEventListener('beforeunload',()=>{if(active){saveCompass();saveNotebook()}});
refresh().catch(fail);


/* Paper Isles 0.3.6 | lighthouse interaction. The coast remains a real
   independent scene: animation is CSS/SVG; this adds an optional tiny
   click interaction without blocking text, buttons, or sea ripples. */
(() => {
  const beacon = document.getElementById('coastLighthouseControl');
  const cover = document.getElementById('homeHero');
  if (!beacon || !cover) return;
  beacon.addEventListener('click', (e) => {
    e.preventDefault();
    e.stopPropagation();
    const enabled = cover.classList.toggle('lighthouse-on');
    beacon.setAttribute('aria-pressed', String(enabled));
    beacon.setAttribute('aria-label', enabled ? '熄灭灯塔' : '点亮灯塔');
    beacon.title = enabled ? '点击熄灭灯塔' : '点击点亮灯塔';
  });
})();
