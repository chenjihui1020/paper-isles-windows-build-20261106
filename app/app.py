# -*- coding: utf-8 -*-
"""纸屿 Paper Isles: local-first academic PDF workstation.
Bound to loopback only; it never uploads documents to third-party services.
Requires PyMuPDF. pywebview and Argos Translate are optional enhancements.
"""
from __future__ import annotations
import argparse
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
from contextlib import contextmanager
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse as urlparse
import uuid
import webbrowser
import zipfile
import pdf_text_edit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import pymupdf as fitz
except ImportError:
    try:
        import fitz
    except ImportError:
        raise SystemExit('无法加载内置 PDF 引擎，请重新安装纸屿。')

APP_DIR = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
# On macOS, use Application Support to avoid Documents privacy prompts and
# optional iCloud Desktop/Documents syncing; keep a stable, user-owned folder.
DEFAULT_LIBRARY = (
    Path.home() / 'Library' / 'Application Support' / 'Paper Isles' / '文献库'
    if sys.platform == 'darwin'
    else Path.home() / 'Documents' / '纸屿文献库'
)
DATA_DIR = Path(os.environ.get('PAPER_ISLES_HOME', DEFAULT_LIBRARY)).expanduser()
STATIC_DIR = APP_DIR / 'static'
DB_PATH = DATA_DIR / 'paper_isles.sqlite3'
for folder in ('originals', 'papers', 'trash', 'archives'):
    (DATA_DIR / folder).mkdir(parents=True, exist_ok=True)
LOCK = threading.RLock()
JOBS = {}


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with db() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS folders (
            id TEXT PRIMARY KEY, parent_id TEXT, name TEXT NOT NULL,
            created_at TEXT NOT NULL, deleted_at TEXT,
            FOREIGN KEY(parent_id) REFERENCES folders(id) ON DELETE SET NULL);
        CREATE TABLE IF NOT EXISTS documents (
            id TEXT PRIMARY KEY, legacy_id TEXT UNIQUE,
            folder_id TEXT, filename TEXT NOT NULL, sha256 TEXT,
            title TEXT NOT NULL, authors TEXT NOT NULL DEFAULT '', year TEXT NOT NULL DEFAULT '',
            venue TEXT NOT NULL DEFAULT '', tags TEXT NOT NULL DEFAULT '[]',
            pages INTEGER NOT NULL DEFAULT 0, language TEXT NOT NULL DEFAULT 'en',
            summary TEXT NOT NULL DEFAULT '', question TEXT NOT NULL DEFAULT '',
            method TEXT NOT NULL DEFAULT '', result TEXT NOT NULL DEFAULT '',
            value TEXT NOT NULL DEFAULT '', limits TEXT NOT NULL DEFAULT '',
            notebook TEXT NOT NULL DEFAULT '', favorite INTEGER DEFAULT 0,
            reading_status TEXT NOT NULL DEFAULT 'unread', read_page INTEGER DEFAULT 1,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, deleted_at TEXT,
            FOREIGN KEY(folder_id) REFERENCES folders(id) ON DELETE SET NULL);
        CREATE INDEX IF NOT EXISTS ix_documents_folder ON documents(folder_id);
        CREATE INDEX IF NOT EXISTS ix_documents_deleted ON documents(deleted_at);
        CREATE TABLE IF NOT EXISTS extracts (
            id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, page INTEGER NOT NULL,
            quote TEXT NOT NULL DEFAULT '', thought TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY(doc_id) REFERENCES documents(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS ocr_pages (
            doc_id TEXT NOT NULL, page INTEGER NOT NULL, text TEXT NOT NULL,
            words TEXT NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY(doc_id, page), FOREIGN KEY(doc_id) REFERENCES documents(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS translations (
            doc_id TEXT NOT NULL, page INTEGER NOT NULL, direction TEXT NOT NULL,
            source_hash TEXT NOT NULL, translation TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(doc_id, page, direction),
            FOREIGN KEY(doc_id) REFERENCES documents(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS pdf_text_edits (
            id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, page INTEGER NOT NULL,
            old_text TEXT NOT NULL, new_text TEXT NOT NULL,
            before_sha TEXT NOT NULL, after_sha TEXT NOT NULL,
            backup_file TEXT NOT NULL, created_at TEXT NOT NULL,
            undone INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(doc_id) REFERENCES documents(id) ON DELETE CASCADE);
        CREATE INDEX IF NOT EXISTS ix_pdf_text_edits_doc ON pdf_text_edits(doc_id,created_at);
        ''')
        # Non-destructive v0.2 -> v0.3 migrations. Existing notes / PDFs remain intact.
        cols = {r['name'] for r in c.execute('PRAGMA table_info(extracts)').fetchall()}
        if 'kind' not in cols:
            c.execute("ALTER TABLE extracts ADD COLUMN kind TEXT NOT NULL DEFAULT 'mixed'")
        if 'linked_extract' not in cols:
            c.execute('ALTER TABLE extracts ADD COLUMN linked_extract TEXT')
        if 'updated_at' not in cols:
            c.execute('ALTER TABLE extracts ADD COLUMN updated_at TEXT')


def rows(conn, sql, params=()):
    return [dict(x) for x in conn.execute(sql, params).fetchall()]


def safe_doc(doc_id):
    if not isinstance(doc_id, str) or not (2 <= len(doc_id) <= 64) or not all(c.isalnum() or c in '-_' for c in doc_id):
        raise ValueError('无效文献编号')
    with db() as c:
        r = c.execute('SELECT * FROM documents WHERE id=?', (doc_id,)).fetchone()
    if not r:
        raise FileNotFoundError('文献不存在')
    return dict(r)


def work_path(doc_id):
    safe_doc(doc_id)
    return DATA_DIR / 'papers' / f'{doc_id}.pdf'


def original_path(doc_id):
    return DATA_DIR / 'originals' / f'{doc_id}.pdf'


def clean_name(name):
    name = Path(str(name).replace('\\', '/')).name
    return ''.join(c for c in name if ord(c) > 31 and c not in '<>:"|?*').strip()[:150] or '文献.pdf'


def normalize_document(d):
    d = dict(d)
    try:
        d['tags'] = json.loads(d.get('tags') or '[]')
    except Exception:
        d['tags'] = []
    d['favorite'] = bool(d.get('favorite'))
    return d


def bootstrap():
    with db() as c:
        folder_items = rows(c, 'SELECT * FROM folders ORDER BY name')
        docs = [normalize_document(x) for x in rows(c, 'SELECT * FROM documents ORDER BY updated_at DESC')]
    return {'folders': folder_items, 'documents': docs, 'app_version': '0.4.2',
            'library_path': str(DATA_DIR), 'features': translation_status()}


def detect_language(text):
    if not text.strip():
        return 'unknown'
    sample = text[:4000]
    zh = sum(1 for c in sample if '\u4e00' <= c <= '\u9fff')
    letters = sum(c.isascii() and c.isalpha() for c in sample)
    return 'zh' if zh > max(20, letters / 3) else 'en'


def smart_title(pdf, filename):
    info = pdf.metadata or {}
    title = (info.get('title') or '').strip()
    if title and title.lower() not in ('untitled', 'unknown', 'document', 'microsoft word') and len(title) <= 200:
        return title
    return Path(filename).stem.replace('_', ' ').strip()


def copy_into_library(source: Path, filename: str, folder_id=None, meta=None, legacy_id=None):
    filename = clean_name(filename)
    if source.stat().st_size > 250 * 1024 * 1024:
        raise ValueError('单份PDF上限250MB，可调整配置后再导入')
    with source.open('rb') as f:
        if not f.read(5).startswith(b'%PDF-'):
            raise ValueError('文件不是有效的PDF开头')
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    with db() as c:
        existing = c.execute('SELECT id FROM documents WHERE sha256=?', (digest,)).fetchone()
        if existing:
            return {'id': existing['id'], 'duplicate': True}
        if folder_id and not c.execute('SELECT id FROM folders WHERE id=? AND deleted_at IS NULL', (folder_id,)).fetchone():
            raise ValueError('目标文件夹不存在')
    with fitz.open(source) as pdf:
        if pdf.needs_pass:
            raise ValueError('加密PDF暂不支持自动导入，请先在有权使用的情况下解锁')
        page_count = len(pdf)
        text = pdf[0].get_text('text', sort=True)[:3000] if page_count else ''
        title = smart_title(pdf, filename)
        a = (pdf.metadata or {}).get('author') or ''
    doc_id = uuid.uuid4().hex[:16]
    shutil.copy2(source, original_path(doc_id))
    shutil.copy2(source, DATA_DIR / 'papers' / f'{doc_id}.pdf')
    meta = meta or {}
    tag_values = meta.get('tags', [])
    if isinstance(tag_values, str):
        tag_values = [x.strip() for x in tag_values.split(',') if x.strip()]
    stamp = now()
    with db() as c:
        c.execute('''INSERT INTO documents
        (id,legacy_id,folder_id,filename,sha256,title,authors,year,venue,tags,pages,language,
         summary,question,method,result,value,limits,notebook,created_at,updated_at)
         VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
         (doc_id, legacy_id,folder_id,filename,digest,meta.get('title') or title,
          meta.get('authors') or a, str(meta.get('year') or ''),meta.get('venue') or '',
          json.dumps(tag_values,ensure_ascii=False), page_count,detect_language(text),
          meta.get('summary') or '',meta.get('question') or '',meta.get('method') or '',
          meta.get('result') or '',meta.get('value') or '',meta.get('limits') or '',
          meta.get('notebook') or '',stamp,stamp))
    return {'id': doc_id, 'duplicate': False, 'pages': page_count}


def update_doc(doc_id, updates):
    allowed = {'folder_id','title','authors','year','venue','tags','summary','question',
               'method','result','value','limits','notebook','favorite','reading_status','read_page'}
    vals = {k:v for k,v in updates.items() if k in allowed}
    if 'tags' in vals:
        vals['tags'] = json.dumps(vals['tags'] if isinstance(vals['tags'], list) else [], ensure_ascii=False)
    if 'read_page' in vals:
        vals['read_page'] = max(1,int(vals['read_page']))
    if 'reading_status' in vals and vals['reading_status'] not in ('unread','reading','done'):
        raise ValueError('无效阅读状态')
    if not vals:
        return normalize_document(safe_doc(doc_id))
    vals['updated_at'] = now()
    with db() as c:
        if not c.execute('SELECT id FROM documents WHERE id=?', (doc_id,)).fetchone():
            raise FileNotFoundError('文献不存在')
        if vals.get('folder_id') and not c.execute('SELECT id FROM folders WHERE id=? AND deleted_at IS NULL',(vals['folder_id'],)).fetchone():
            raise ValueError('目标文件夹不存在')
        c.execute('UPDATE documents SET '+','.join(k+'=?' for k in vals)+' WHERE id=?',tuple(vals.values())+(doc_id,))
    return normalize_document(safe_doc(doc_id))


def render_page(doc_id,page,scale=1.35):
    file = work_path(doc_id)
    with LOCK, fitz.open(file) as pdf:
        if page < 1 or page > len(pdf):
            raise ValueError('页码超出范围')
        p = pdf[page-1]
        scale = max(.45,min(2.25,float(scale)))
        pix = p.get_pixmap(matrix=fitz.Matrix(scale,scale), alpha=False, annots=True)
        return pix.tobytes('png')


def _page_words(page, textpage=None):
    src = page.get_text('words', sort=False, textpage=textpage) if textpage else page.get_text('words', sort=False)
    out=[]
    for row in src[:15000]:
        if len(row) < 5:continue
        x0,y0,x1,y1,val=row[:5]
        if not str(val).strip():continue
        out.append([round(float(x0),2),round(float(y0),2),round(float(x1),2),round(float(y1),2),str(val)])
    return out


def get_page_text(doc_id,page):
    file = work_path(doc_id)
    with LOCK, fitz.open(file) as pdf:
        if not (1 <= page <= len(pdf)):raise ValueError('页码超出范围')
        p = pdf[page-1]
        text = p.get_text('text', sort=True)
        words = _page_words(p)
        w,h = p.rect.width,p.rect.height
        # PDF fonts without proper Unicode maps can yield U+FFFD / control symbols.
        suspicious = (text.count('\ufffd') + text.count('\x00')) > max(3, len(text)//30)
    with db() as c:
        stored = c.execute('SELECT text,words FROM ocr_pages WHERE doc_id=? AND page=?',(doc_id,page)).fetchone()
    if stored:
        return {'page':page,'pages':safe_doc(doc_id)['pages'],'text':stored['text'],
                'words':json.loads(stored['words']),'width':w,'height':h,'scanned':False,
                'text_source':'ocr','suspicious':False}
    return {'page':page,'pages':safe_doc(doc_id)['pages'],'text':text,'words':words,
            'width':w,'height':h,'scanned':len(text.strip())<15,
            'text_source':'pdf','suspicious':suspicious}


def ocr_page(doc_id,page):
    # Local-only, opt-in OCR. Tesseract and its language data must be installed.
    with LOCK,fitz.open(work_path(doc_id)) as pdf:
        if not 1 <= page <= len(pdf):raise ValueError('页码超出范围')
        p=pdf[page-1]
        try:
            tp=p.get_textpage_ocr(language='eng', dpi=165, full=True)
            text=p.get_text('text',textpage=tp,sort=True)
            words=_page_words(p,tp)
        except Exception as ex:
            raise RuntimeError('本地OCR未能运行。请先安装Tesseract OCR及英文语言包，然后重启纸屿。详情：'+str(ex)[:160]) from ex
    if not text.strip():raise ValueError('本页OCR没有识别出可用文字')
    with db() as c:
        c.execute('INSERT OR REPLACE INTO ocr_pages VALUES (?,?,?,?,?)',
                  (doc_id,page,text,json.dumps(words,ensure_ascii=False),now()))
    return {'ok':True,'words':len(words),'characters':len(text)}


def search_pdf(doc_id,query,max_hits=80):
    query=str(query or '').strip()[:90]
    if len(query)<2:raise ValueError('至少输入两个字符')
    out=[]
    with LOCK,fitz.open(work_path(doc_id)) as pdf:
        for i,p in enumerate(pdf):
            for r in p.search_for(query)[:12]:
                out.append({'page':i+1,'rect':[round(v,1) for v in r]})
                if len(out)>=max_hits:return out
    return out

def hex_rgb(code):
    c = str(code).lstrip('#')
    if len(c)!=6:return (0.89,0.68,0.21)
    try:return tuple(int(c[i:i+2],16)/255 for i in (0,2,4))
    except Exception:return (0.89,0.68,0.21)


def annotate(doc_id,item):
    page_n = int(item.get('page',1))
    kind = item.get('type')
    color = hex_rgb(item.get('color','#e2b858'))
    opacity = max(.08,min(.85,float(item.get('opacity',.38))))
    width = max(.35,min(14,float(item.get('width',1.8))))
    path = work_path(doc_id)
    with LOCK, fitz.open(path) as pdf:
        if not 1 <= page_n <= len(pdf):raise ValueError('页码不合法')
        p=pdf[page_n-1]
        annot=None
        if kind in ('highlight','underline','rect'):
            coords=item.get('rect')
            if not isinstance(coords,list) or len(coords)!=4:raise ValueError('缺少矩形坐标')
            r = fitz.Rect(*map(float,coords)) & p.rect
            if r.is_empty or r.width<2 or r.height<2:raise ValueError('标注区域无效')
            annot = p.add_highlight_annot(r) if kind=='highlight' else (p.add_underline_annot(r) if kind=='underline' else p.add_rect_annot(r))
        elif kind in ('text-highlight','text-underline'):
            rects=item.get('rects') or []
            if not isinstance(rects,list) or not 1<=len(rects)<=100:raise ValueError('缺少选中文字的坐标')
            quads=[]
            for rect in rects:
                if not isinstance(rect,list) or len(rect)!=4:continue
                r=fitz.Rect(*map(float,rect)) & p.rect
                if not r.is_empty and r.width>=1 and r.height>=2:quads.append(fitz.Quad(r.tl, r.tr, r.bl, r.br))
            if not quads:raise ValueError('所选文字坐标无效，请尝试矩形标注或对本页OCR')
            annot = p.add_highlight_annot(quads) if kind=='text-highlight' else p.add_underline_annot(quads)
        elif kind=='selection':
            quote = (item.get('quote') or '').strip()[:1000]
            if len(quote)<2:raise ValueError('请先选取一段原文文字')
            quads = p.search_for(quote, quads=True)
            if not quads:
                head=quote.replace('\n',' ').split('  ')[0][:100]
                quads = p.search_for(head,quads=True)
            if not quads:raise ValueError('未能精确定位文字，可先选中文字或尝试OCR')
            annot=p.add_highlight_annot(quads[:40])
        elif kind=='ink':
            pts=item.get('points') or []
            if not 2<=len(pts)<=5000:raise ValueError('轨迹点无效')
            coords=[(float(x),float(y)) for x,y in pts]
            annot=p.add_ink_annot([coords])
        else:raise ValueError('不支持的批注类型')
        if annot:
            if kind == 'rect':
                annot.set_colors(stroke=color,fill=color)
                annot.set_border(width=.4)
            else:annot.set_colors(stroke=color)
            if kind=='ink':annot.set_border(width=width)
            annot.set_opacity(opacity)
            marker='paperisles-annot-'+uuid.uuid4().hex
            annot.set_info({'title':'纸屿 · Paper Isles','subject':marker})
            annot.update()
            xref=annot.xref
        with tempfile.NamedTemporaryFile(prefix='paperisles_',suffix='.pdf',dir=DATA_DIR/'papers',delete=False) as out:
            tmp=Path(out.name)
        try:pdf.save(tmp,garbage=3,deflate=True)
        except Exception:tmp.unlink(missing_ok=True);raise
        os.replace(tmp,path)  # atomic swap while holding LOCK
    # Garbage collection can renumber annotation xrefs. Resolve the saved xref for undo.
    with LOCK,fitz.open(path) as saved_pdf:
        for ann in saved_pdf[page_n-1].annots() or []:
            if (ann.info or {}).get('subject')==marker:
                xref=ann.xref
                break
    with db() as c:c.execute('UPDATE documents SET updated_at=? WHERE id=?',(now(),doc_id))
    return {'ok':True,'xref':xref,'marker':marker,'page':page_n}

def get_annotations(doc_id,page):
    out=[]
    with LOCK,fitz.open(work_path(doc_id)) as pdf:
        for a in pdf[page-1].annots() or []:
            info=a.info or {}
            out.append({'xref':a.xref,'type':a.type[1], 'text':info.get('content',''), 'rect':list(a.rect),'color':a.colors,'opacity':a.opacity})
    return out


def delete_annotation(doc_id,page,xref,marker=None):
    path=work_path(doc_id)
    with LOCK,fitz.open(path) as pdf:
        p=pdf[page-1]
        found=False
        for a in p.annots() or []:
            if (marker and (a.info or {}).get('subject')==marker) or (not marker and a.xref==int(xref)):
                p.delete_annot(a);found=True;break
        if not found:raise ValueError('该批注不存在')
        with tempfile.NamedTemporaryFile(prefix='paperisles_',suffix='.pdf',dir=DATA_DIR/'papers',delete=False) as out:tmp=Path(out.name)
        try:pdf.save(tmp,garbage=3,deflate=True)
        except Exception:tmp.unlink(missing_ok=True);raise
        os.replace(tmp,path)  # atomic swap while holding LOCK
    return {'ok':True}



# Native text editing is deliberately separate from note editing and annotations.
# It replaces a single unrotated, consistently-styled PDF text line only.
def pdf_text_edit_preview(doc_id, page, x, y):
    with LOCK:
        return pdf_text_edit.preview(work_path(doc_id), int(page), float(x), float(y))


def pdf_text_edit_apply(doc_id, item):
    page = int(item.get('page', 1))
    history = DATA_DIR / 'archives' / 'pdf-text-history'
    with LOCK:
        result = pdf_text_edit.replace(
            work_path(doc_id), history,
            page=page, x=item.get('x'), y=item.get('y'),
            original=item.get('original'), replacement=item.get('replacement'),
            expected_stamp=item.get('stamp'),
        )
        # Database and translations need to agree with the modified PDF.
        with db() as c:
            c.execute('''INSERT INTO pdf_text_edits
                (id,doc_id,page,old_text,new_text,before_sha,after_sha,backup_file,created_at)
                VALUES (?,?,?,?,?,?,?,?,?)''',
                (result['edit_id'],doc_id,page,item['original'],item['replacement'],
                 result['before_sha'],result['after_sha'],result['backup_file'],now()))
            c.execute('DELETE FROM ocr_pages WHERE doc_id=? AND page=?',(doc_id,page))
            c.execute('DELETE FROM translations WHERE doc_id=? AND page=?',(doc_id,page))
            c.execute('UPDATE documents SET updated_at=? WHERE id=?',(now(),doc_id))
            # History is bounded: at most eight complete snapshots per document.
            old = c.execute('''SELECT id,backup_file FROM pdf_text_edits WHERE doc_id=?
                                ORDER BY rowid DESC LIMIT -1 OFFSET 8''',(doc_id,)).fetchall()
            for stale in old:
                c.execute('DELETE FROM pdf_text_edits WHERE id=?',(stale['id'],))
        for stale in old:
            Path(stale['backup_file']).unlink(missing_ok=True)
    return {'ok': True, 'edit_id': result['edit_id'], 'page': page,
            'font_size': result['font_size']}


def pdf_text_edit_undo(doc_id, edit_id):
    with LOCK:
        with db() as c:
            latest = c.execute('''SELECT * FROM pdf_text_edits
                WHERE doc_id=? AND undone=0 ORDER BY rowid DESC LIMIT 1''',(doc_id,)).fetchone()
        if not latest or latest['id'] != edit_id:
            raise ValueError('只能撤销该文献最近一次尚未撤销的 PDF 文字修改')
        # Backup paths are never accepted from the HTTP client.
        backup = (DATA_DIR / 'archives' / 'pdf-text-history' / (edit_id + '.pdf'))
        if str(backup) != latest['backup_file']:
            raise ValueError('文字修改备份校验失败')
        pdf_text_edit.undo(work_path(doc_id), backup, latest['after_sha'])
        with db() as c:
            c.execute('UPDATE pdf_text_edits SET undone=1 WHERE id=?',(edit_id,))
            c.execute('DELETE FROM ocr_pages WHERE doc_id=? AND page=?',(doc_id,latest['page']))
            c.execute('DELETE FROM translations WHERE doc_id=? AND page=?',(doc_id,latest['page']))
            c.execute('UPDATE documents SET updated_at=? WHERE id=?',(now(),doc_id))
        return {'ok': True, 'page': latest['page']}


def translation_status():
    try:
        import argostranslate.translate as tr
        languages=tr.get_installed_languages()
        combos=[]
        for src,dst in [('en','zh'),('zh','en')]:
            a=next((x for x in languages if x.code==src),None)
            b=next((x for x in languages if x.code==dst),None)
            try:
                if a and b and a.get_translation(b):combos.append(f'{src}-{dst}')
            except Exception:pass
        return {'engine':True,'pairs':combos}
    except ImportError:
        return {'engine':False,'pairs':[]}
    except Exception:
        return {'engine':True,'pairs':[]}


def translate_text(text, direction):
    if direction not in ('en-zh','zh-en'):raise ValueError('只支持中英互译')
    import argostranslate.translate as tr
    from_lang,to_lang=direction.split('-')
    languages=tr.get_installed_languages()
    a=next((x for x in languages if x.code==from_lang),None)
    b=next((x for x in languages if x.code==to_lang),None)
    if not a or not b:raise RuntimeError('尚未安装该方向的离线翻译模型')
    translator=a.get_translation(b)
    text=text.strip()
    if not text:return ''
    # split roughly by paragraph and long continuous lines to avoid model context overflow
    parts=[]
    for para in text.split('\n\n'):
        if not para.strip():continue
        while len(para)>900:
            cut=para.rfind('. ',400,900)
            if cut<300:cut=para.rfind(' ',400,900)
            if cut<300:cut=850
            parts.append(para[:cut+1]);para=para[cut+1:]
        if para.strip():parts.append(para)
    return '\n\n'.join(translator.translate(x) for x in parts)


def page_translation(doc_id,page,direction='en-zh'):
    source=get_page_text(doc_id,page)['text']
    sh=hashlib.sha256(source.encode('utf-8')).hexdigest()
    with db() as c:
        hit=c.execute('SELECT * FROM translations WHERE doc_id=? AND page=? AND direction=?',(doc_id,page,direction)).fetchone()
        if hit and hit['source_hash']==sh:
            return {'text':hit['translation'],'cached':True,'original':source}
    result=translate_text(source,direction)
    with db() as c:
        c.execute('INSERT OR REPLACE INTO translations VALUES(?,?,?,?,?,?)',(doc_id,page,direction,sh,result,now()))
    return {'text':result,'cached':False,'original':source}


def start_full_translation(doc_id,direction):
    key=f'{doc_id}:{direction}'
    active=JOBS.get(key)
    if active and active.get('status')=='running':return active
    doc=safe_doc(doc_id)
    JOBS[key]={'status':'running','done':0,'total':doc['pages'],'errors':0,'cancel':False,'started_at':now()}
    def work():
        for pg in range(1,doc['pages']+1):
            if JOBS[key]['cancel']:
                JOBS[key]['status']='cancelled';return
            try:
                page_translation(doc_id,pg,direction)
            except Exception as e:
                JOBS[key]['errors']+=1
                JOBS[key]['error']=str(e)
                if JOBS[key]['errors']>3:
                    JOBS[key]['status']='failed';return
            JOBS[key]['done']=pg
        JOBS[key]['status']='done'
    threading.Thread(target=work,name='translation-'+doc_id,daemon=True).start()
    return JOBS[key]


def migrate_notes(payload):
    all_notes=payload.get('notes',payload)
    count=0
    for legacy,item in all_notes.items():
        if not isinstance(item,dict):continue
        with db() as c:
            doc=c.execute('SELECT id FROM documents WHERE legacy_id=?',(legacy,)).fetchone()
        if not doc:continue
        docid=doc['id']
        update_doc(docid,{'notebook':item.get('essay',''),'favorite':int(bool(item.get('favorite'))),
                          'reading_status':item.get('status','unread')})
        for q in item.get('quotes',[]) or []:
            with db() as c:
                c.execute('INSERT INTO extracts(id,doc_id,page,quote,thought,created_at) VALUES(?,?,?,?,?,?)',
                          (uuid.uuid4().hex[:16],docid,max(1,int(q.get('page',1))),q.get('quote',''),q.get('reflection',''),now()))
        count+=1
    return {'migrated':count,'note':'旧版影像画笔坐标不自动迁移为PDF原生批注，以免错位；文字笔记与摘录已导入。'}


def backup_library():
    stamp=dt.datetime.now().strftime('%Y%m%d_%H%M%S')
    outfile=DATA_DIR/'archives'/f'纸屿备份_{stamp}.zip'
    # sqlite online backup creates a transactionally consistent snapshot
    temp_db=DATA_DIR/'archives'/f'_snapshot_{uuid.uuid4().hex[:8]}.sqlite3'
    with db() as source,sqlite3.connect(temp_db) as dest:
        source.backup(dest)
    try:
        with zipfile.ZipFile(outfile,'w',zipfile.ZIP_DEFLATED,compresslevel=4,allowZip64=True) as z:
            z.write(temp_db,'paper_isles.sqlite3')
            for folder in ('originals','papers'):
                for path in (DATA_DIR/folder).glob('*.pdf'):
                    z.write(path,f'{folder}/{path.name}')
    finally:
        temp_db.unlink(missing_ok=True)
    return outfile


class Handler(BaseHTTPRequestHandler):
    server_version = 'PaperIsles/0.4.2'
    def log_message(self,*args):
        pass
    def send_bytes(self,raw,mime='application/json; charset=utf-8',status=200,filename=None):
        self.send_response(status)
        self.send_header('Content-Type',mime)
        self.send_header('Content-Length',str(len(raw)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('X-Frame-Options','SAMEORIGIN')
        if filename:self.send_header('Content-Disposition',"attachment; filename*=UTF-8''"+urlparse.quote(filename))
        self.end_headers()
        try:self.wfile.write(raw)
        except (ConnectionAbortedError, BrokenPipeError):pass
    def ok(self,data,status=200):
        self.send_bytes(json.dumps(data,ensure_ascii=False).encode('utf-8'),status=status)
    def bad(self,e,status=400):
        self.ok({'error':str(e),'type':e.__class__.__name__},status)
    def payload(self):
        length=int(self.headers.get('Content-Length','0'))
        if length>10*1024*1024:raise ValueError('JSON内容过大')
        return json.loads(self.rfile.read(length) or b'{}')
    def verify_mutation(self):
        host=self.headers.get('Origin')
        if host and not host.startswith('http://127.0.0.1:') and not host.startswith('http://localhost:'):
            raise PermissionError('仅允许本地程序访问')
        if self.headers.get('X-Paper-Isles')!='local-client':
            raise PermissionError('本地操作校验未通过')
    def parsed(self):
        p=urlparse.urlparse(self.path)
        return p.path,[x for x in p.path.split('/') if x],urlparse.parse_qs(p.query)
    def doc_part(self,parts):
        if len(parts)<3:return None
        if parts[0]!='api' or parts[1]!='doc':return None
        return parts[2]
    def do_GET(self):
        try:
            path,parts,q=self.parsed()
            if path=='/':
                return self.send_bytes((STATIC_DIR/'index.html').read_bytes(),'text/html; charset=utf-8')
            if path.startswith('/static/'):
                leaf=Path(path).name
                whitelist={'app.js':'application/javascript; charset=utf-8','style.css':'text/css; charset=utf-8','coast-painting.webp':'image/webp'}
                if leaf not in whitelist:raise FileNotFoundError()
                return self.send_bytes((STATIC_DIR/leaf).read_bytes(),whitelist[leaf])
            if path=='/api/bootstrap':return self.ok(bootstrap())
            if path=='/api/features':return self.ok(translation_status())
            if path=='/api/backup':
                f=backup_library()
                return self.file_download(f,'application/zip',f.name)
            if path=='/api/translate/status':
                key=f"{q.get('doc',[''])[0]}:{q.get('direction',['en-zh'])[0]}"
                return self.ok(JOBS.get(key,{'status':'idle','done':0,'total':0}))
            if len(parts)>=3 and parts[:2]==['api','doc']:
                doc_id=parts[2]
                d=safe_doc(doc_id)
                if len(parts)==3:return self.ok(normalize_document(d))
                action=parts[3]
                if action in ('pdf','export'):
                    f=work_path(doc_id)
                    if action=='export':return self.file_download(f,'application/pdf',clean_name(Path(d['filename']).stem+'_批注版.pdf'))
                    return self.pdf_stream(f)
                if action=='page' and len(parts)>=5:
                    n=int(parts[4]);scale=float(q.get('scale',[1.35])[0])
                    return self.send_bytes(render_page(doc_id,n,scale),'image/png')
                if action=='text':return self.ok(get_page_text(doc_id,int(q.get('page',[1])[0])))
                if action=='text-edit-preview':
                    return self.ok(pdf_text_edit_preview(doc_id,int(q.get('page',[1])[0]),
                        float(q.get('x',['nan'])[0]),float(q.get('y',['nan'])[0])))
                if action=='search':return self.ok({'hits':search_pdf(doc_id,q.get('q',[''])[0])})
                if action=='abstract':
                    import re
                    blocks=[]
                    for pg in range(1,min(4,d['pages']+1)):
                        blocks.append(get_page_text(doc_id,pg)['text'])
                    all_text='\n'.join(blocks)
                    match=re.search(r'(?:^|\n)\s*(?:abstract|摘\s*要)\s*[:：]?\s*(.{120,2200}?)(?:\n\s*(?:keywords|key words|关键词|introduction|1\s+introduction)\b|$)',all_text,re.I|re.S)
                    extracted=(match.group(1) if match else all_text[:900]).strip()
                    return self.ok({'draft':extracted[:2000],'extraction_only':True})
                if action=='annotations':return self.ok({'items':get_annotations(doc_id,int(q.get('page',[1])[0]))})
                if action=='extracts':
                    with db() as c:ls=rows(c,'SELECT * FROM extracts WHERE doc_id=? ORDER BY page ASC, created_at DESC',(doc_id,))
                    return self.ok({'items':ls})
                if action=='translation':
                    direction=q.get('direction',['en-zh'])[0]; page=int(q.get('page',[1])[0])
                    with db() as c:hit=c.execute('SELECT * FROM translations WHERE doc_id=? AND page=? AND direction=?',(doc_id,page,direction)).fetchone()
                    return self.ok({'translation':hit['translation'] if hit else None,'direction':direction})
            return self.bad('未找到对应资源',404)
        except Exception as e:return self.bad(e,404 if isinstance(e,FileNotFoundError) else 500)
    def pdf_stream(self,path):
        size=path.stat().st_size
        rng=self.headers.get('Range')
        if rng and rng.startswith('bytes='):
            try:
                s,e=rng.split('=')[1].split('-')
                start=int(s) if s else max(0,size-int(e));end=int(e) if e else size-1
                if start<0 or end<start or start>=size:raise ValueError()
                end=min(end,size-1)
                self.send_response(206)
                self.send_header('Content-Type','application/pdf')
                self.send_header('Content-Range',f'bytes {start}-{end}/{size}')
                self.send_header('Content-Length',str(end-start+1))
                self.send_header('Accept-Ranges','bytes')
                self.send_header('Cache-Control','no-store')
                self.end_headers()
                with path.open('rb') as f:
                    f.seek(start);self.wfile.write(f.read(end-start+1))
                return
            except (ValueError,IndexError):
                self.send_response(416);self.end_headers();return
        self.send_response(200)
        self.send_header('Content-Type','application/pdf')
        self.send_header('Accept-Ranges','bytes')
        self.send_header('Content-Length',str(size))
        self.send_header('Cache-Control','no-store')
        self.end_headers()
        try:
            with path.open('rb') as f:shutil.copyfileobj(f,self.wfile)
        except (ConnectionAbortedError,BrokenPipeError):pass
    def file_download(self,path,mime,name):
        self.send_response(200)
        self.send_header('Content-Type',mime)
        self.send_header('Content-Length',str(path.stat().st_size))
        self.send_header('Content-Disposition',"attachment; filename*=UTF-8''"+urlparse.quote(name))
        self.end_headers()
        try:
            with path.open('rb') as f:shutil.copyfileobj(f,self.wfile)
        except (ConnectionAbortedError,BrokenPipeError):pass
    def do_POST(self):
        try:
            self.verify_mutation()
            path,parts,q=self.parsed()
            if path=='/api/import':
                size=int(self.headers.get('Content-Length','0'))
                if size<=0 or size>250*1024*1024:raise ValueError('PDF大小超出范围（最大250MB）')
                filename=q.get('name',['文献.pdf'])[0]
                folder=q.get('folder',[''])[0] or None
                temp_dir=DATA_DIR/'archives'
                with tempfile.NamedTemporaryFile(dir=temp_dir,suffix='.pdf',delete=False) as tmp:
                    tmp_path=Path(tmp.name)
                    remaining=size
                    while remaining:
                        block=self.rfile.read(min(2**20,remaining))
                        if not block:raise IOError('PDF上传意外中断')
                        tmp.write(block);remaining-=len(block)
                try:res=copy_into_library(tmp_path,filename,folder)
                finally:tmp_path.unlink(missing_ok=True)
                return self.ok(res)
            if path=='/api/folder':
                data=self.payload();name=str(data.get('name','')).strip()[:100]
                if not name:raise ValueError('请输入文件夹名称')
                parent=data.get('parent_id') or None
                with db() as c:
                    if parent and not c.execute('SELECT id FROM folders WHERE id=? AND deleted_at IS NULL',(parent,)).fetchone():raise ValueError('上级文件夹不存在')
                    fid=uuid.uuid4().hex[:12]
                    c.execute('INSERT INTO folders(id,parent_id,name,created_at) VALUES (?,?,?,?)',(fid,parent,name,now()))
                return self.ok({'id':fid})
            if path=='/api/migrate':return self.ok(migrate_notes(self.payload()))
            if path=='/api/translate/selection':
                data=self.payload();return self.ok({'translation':translate_text(data.get('text',''),data.get('direction','en-zh'))})
            if path=='/api/translate/model':
                size=int(self.headers.get('Content-Length','0'))
                if not 0<size<150*1024*1024:raise ValueError('翻译模型文件过大')
                import argostranslate.package as package
                with tempfile.NamedTemporaryFile(dir=DATA_DIR/'archives',suffix='.argosmodel',delete=False) as f:
                    tmp=Path(f.name);f.write(self.rfile.read(size))
                try:package.install_from_path(str(tmp))
                finally:tmp.unlink(missing_ok=True)
                return self.ok({'features':translation_status()})
            if len(parts)>=3 and parts[:2]==['api','doc']:
                doc_id=parts[2]
                safe_doc(doc_id)
                if len(parts)==3:return self.ok(update_doc(doc_id,self.payload()))
                action=parts[3]
                if action=='annotate':return self.ok(annotate(doc_id,self.payload()))
                if action=='text-edit':return self.ok(pdf_text_edit_apply(doc_id,self.payload()))
                if action=='undo-text-edit':
                    return self.ok(pdf_text_edit_undo(doc_id,str(self.payload().get('edit_id',''))))
                if action=='annotation/delete':return self.bad('路径不合法',404)
                if action=='delete-annotation':
                    data=self.payload();return self.ok(delete_annotation(doc_id,int(data.get('page',1)),int(data.get('xref',0)),data.get('marker')))
                if action=='extract':
                    data=self.payload();eid=uuid.uuid4().hex[:16];kind=data.get('kind','quote')
                    if kind not in ('quote','thought','question','critique'):raise ValueError('笔记类型不合法')
                    link=data.get('linked_extract') or None
                    with db() as c:
                        if link and not c.execute('SELECT 1 FROM extracts WHERE id=? AND doc_id=?',(link,doc_id)).fetchone():raise ValueError('关联的摘录不存在')
                        c.execute('''INSERT INTO extracts(id,doc_id,page,quote,thought,created_at,kind,linked_extract,updated_at)
                                     VALUES (?,?,?,?,?,?,?,?,?)''',
                                  (eid,doc_id,max(1,int(data.get('page',1))),str(data.get('quote',''))[:8000],
                                   str(data.get('thought',''))[:10000],now(),kind,link,now()))
                    return self.ok({'id':eid})
                if action=='edit-extract':
                    data=self.payload();eid=data.get('id');kind=data.get('kind','quote')
                    if kind not in ('quote','thought','question','critique','mixed'):raise ValueError('笔记类型不合法')
                    with db() as c:
                        row=c.execute('SELECT * FROM extracts WHERE id=? AND doc_id=?',(eid,doc_id)).fetchone()
                        if not row:raise ValueError('笔记不存在')
                        c.execute('''UPDATE extracts SET page=?,quote=?,thought=?,kind=?,updated_at=? WHERE id=? AND doc_id=?''',
                                  (max(1,int(data.get('page',row['page']))),str(data.get('quote',''))[:8000],
                                   str(data.get('thought',''))[:10000],kind,now(),eid,doc_id))
                    return self.ok({'ok':True})
                if action=='ocr-page':
                    data=self.payload();return self.ok(ocr_page(doc_id,int(data.get('page',1))))
                if action=='delete-extract':
                    data=self.payload()
                    with db() as c:c.execute('DELETE FROM extracts WHERE id=? AND doc_id=?',(data['id'],doc_id))
                    return self.ok({'ok':True})
                if action=='translate-page':
                    data=self.payload();return self.ok(page_translation(doc_id,int(data.get('page',1)),data.get('direction','en-zh')))
                if action=='translate-all':
                    direction=self.payload().get('direction','en-zh')
                    if direction not in translation_status().get('pairs',[]):raise ValueError('请先安装所选中英翻译模型')
                    return self.ok(start_full_translation(doc_id,direction))
                if action=='cancel-translation':
                    direction=self.payload().get('direction','en-zh');key=f'{doc_id}:{direction}'
                    if key in JOBS:JOBS[key]['cancel']=True
                    return self.ok({'ok':True})
                if action in ('trash','restore','permanent-delete','force-delete'):
                    backup_files=[]
                    with db() as c:
                        if action in ('permanent-delete','force-delete'):
                            backup_files=[Path(x['backup_file']) for x in c.execute(
                                'SELECT backup_file FROM pdf_text_edits WHERE doc_id=?',(doc_id,))]
                        r=c.execute('SELECT deleted_at FROM documents WHERE id=?',(doc_id,)).fetchone()
                        if action=='trash':c.execute('UPDATE documents SET deleted_at=?,updated_at=? WHERE id=?',(now(),now(),doc_id))
                        elif action=='restore':c.execute('UPDATE documents SET deleted_at=NULL,updated_at=? WHERE id=?',(now(),doc_id))
                        else:
                            if action=='permanent-delete' and not r['deleted_at']:raise ValueError('请先移入回收站，再永久删除')
                            if action=='force-delete' and self.payload().get('confirm')!='永久删除':raise ValueError('必须输入「永久删除」才能执行不可撤回操作')
                            c.execute('DELETE FROM documents WHERE id=?',(doc_id,))
                    if action in ('permanent-delete','force-delete'):
                        original_path(doc_id).unlink(missing_ok=True)
                        (DATA_DIR/'papers'/f'{doc_id}.pdf').unlink(missing_ok=True)
                        for backup in backup_files:
                            if backup.parent == DATA_DIR/'archives'/'pdf-text-history':
                                backup.unlink(missing_ok=True)
                    return self.ok({'ok':True})
            if len(parts)==4 and parts[:2]==['api','folder']:
                fid=parts[2];action=parts[3]
                if action=='rename':
                    title=self.payload().get('name','').strip()
                    if not title:raise ValueError('新名称不能为空')
                    with db() as c:c.execute('UPDATE folders SET name=? WHERE id=?',(title[:100],fid))
                    return self.ok({'ok':True})
                if action=='remove':
                    with db() as c:
                        cnt=c.execute('SELECT COUNT(*) FROM documents WHERE folder_id=?',(fid,)).fetchone()[0]
                        children=c.execute('SELECT COUNT(*) FROM folders WHERE parent_id=?',(fid,)).fetchone()[0]
                        if cnt or children:raise ValueError('请先移动或删除文件夹中的文献与子文件夹')
                        c.execute('DELETE FROM folders WHERE id=?',(fid,))
                    return self.ok({'ok':True})
            return self.bad('未找到对应操作',404)
        except PermissionError as e:return self.bad(e,403)
        except (ValueError,KeyError,RuntimeError,ImportError,FileNotFoundError) as e:return self.bad(e)
        except Exception as e:
            traceback.print_exc();return self.bad(e,500)


def self_check():
    """Noninteractive package smoke test, suitable for macOS build agents."""
    import tempfile
    expected = [STATIC_DIR / 'index.html', STATIC_DIR / 'app.js', STATIC_DIR / 'style.css']
    for path in expected:
        if not path.is_file() or path.stat().st_size < 100:
            raise RuntimeError(f'应用内置界面文件不存在：{path.name}')
    import webview
    if sys.platform == 'darwin':
        import objc  # noqa: F401 - detects a missing bundled Cocoa bridge
        import WebKit  # noqa: F401
        import AppKit  # noqa: F401
        import UniformTypeIdentifiers  # noqa: F401
    with fitz.open() as pdf:
        page = pdf.new_page()
        page.insert_text((72, 100), 'Paper Isles PDF engine ready')
        if 'PDF engine ready' not in page.get_text():
            raise RuntimeError('PDF 读写组件无法工作')
    # Database check uses PAPER_ISLES_HOME supplied by caller, never touches
    # the user's real library when executed in the build workflow.
    init_db()
    if not DB_PATH.is_file():
        raise RuntimeError('数据库无法初始化')
    if sys.stdout is not None:
        print('PAPER_ISLES_SELF_CHECK_OK')


def setup_desktop_logging():
    """Persist Finder/Dock launch errors without displaying a Terminal window."""
    if sys.platform != 'darwin' or not getattr(sys, 'frozen', False):
        return
    try:
        log_dir = Path.home() / 'Library' / 'Logs' / 'Paper Isles'
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / 'latest.log'
        if log_path.exists() and log_path.stat().st_size > 2_000_000:
            log_path.replace(log_dir / 'previous.log')
        stream = log_path.open('a', encoding='utf-8', buffering=1)
        sys.stdout = stream
        sys.stderr = stream
        print(f'\n=== Paper Isles 0.4.2 started {dt.datetime.now().isoformat()} ===')
    except OSError:
        pass


def show_startup_alert():
    if sys.platform != 'darwin' or not getattr(sys, 'frozen', False):
        return
    try:
        import subprocess
        subprocess.run([
            '/usr/bin/osascript', '-e',
            'display alert "纸屿无法启动" message "请重新安装纸屿。如仍无法打开，请联系开发者并提供 ~/Library/Logs/Paper Isles/latest.log。" as warning',
        ], timeout=12, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--browser',action='store_true',help='开发调试：在浏览器打开本机页面')
    parser.add_argument('--server-only',action='store_true',help='持续运行本地服务器，不打开窗口（自动化测试）')
    parser.add_argument('--self-check',action='store_true',help='检查安装包内的运行时、PDF 引擎和界面文件')
    parser.add_argument('--version',action='version',version='Paper Isles 0.4.2')
    parser.add_argument('--port',type=int,default=8765)
    args=parser.parse_args()
    if args.self_check:
        return self_check()
    setup_desktop_logging()
    init_db()
    try:
        server=ThreadingHTTPServer(('127.0.0.1',args.port),Handler)
    except OSError:
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        print('默认端口被占用，已使用备用本地端口。')
    server.daemon_threads=True
    threading.Thread(target=server.serve_forever,daemon=True).start()
    address=f'http://127.0.0.1:{server.server_address[1]}'
    print(f'纸屿已启动：{address}  数据库：{DATA_DIR}')
    if not args.browser and not args.server_only:
        try:
            import webview
            webview.create_window('纸屿 · Paper Isles',address,width=1380,height=870,min_size=(850,590),background_color='#f8f7f2')
            webview.start(gui='cocoa' if sys.platform == 'darwin' else None, debug=False,private_mode=False)
            server.shutdown()
            return
        except Exception as ex:
            if sys.platform == 'darwin' and getattr(sys, 'frozen', False):
                server.shutdown()
                raise RuntimeError('macOS 桌面窗口无法启动，请检查系统兼容性') from ex
            print('桌面窗口启动未成功，将使用浏览器：',ex)
    if args.browser or not args.server_only:
        webbrowser.open(address)
    try:
        while True:time.sleep(1)
    except KeyboardInterrupt:
        server.shutdown()


if __name__=='__main__':
    try:
        main()
    except Exception:
        traceback.print_exc()
        show_startup_alert()
        raise SystemExit(1)
