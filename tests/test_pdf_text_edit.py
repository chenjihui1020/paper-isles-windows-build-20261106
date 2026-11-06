"""Paper Isles PDF native text editing integration tests (offline)."""
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from http.server import ThreadingHTTPServer

try:
    import pymupdf as fitz
except ImportError:
    import fitz

ROOT = Path(__file__).resolve().parents[1]
RES = ROOT / 'app'
sys.path.insert(0, str(RES))
TEST_HOME = tempfile.TemporaryDirectory(prefix='paper_isles_tests_')
os.environ['PAPER_ISLES_HOME'] = str(Path(TEST_HOME.name) / 'library')
import app  # noqa: E402


class TestPdfEditing(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.init_db()
        cls.samples = Path(TEST_HOME.name) / 'samples'
        cls.samples.mkdir(exist_ok=True)
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.server.server_address[1]}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)
        TEST_HOME.cleanup()

    def sample_pdf(self, name, *, chinese=False, scanned=False):
        path = self.samples / name
        doc = fitz.open()
        p = doc.new_page(width=595, height=842)
        p.draw_rect(fitz.Rect(45, 65, 520, 155), color=(0, 0.45, 0.4), fill=(0.94, 0.97, 0.95))
        if not scanned:
            if chinese:
                p.insert_text((72, 100), '这是原来的PDF文字', fontname='china-s', fontsize=13)
                p.insert_text((72, 130), '第二行保持不变', fontname='china-s', fontsize=13)
            else:
                p.insert_text((72, 100), 'This is the original PDF text', fontsize=12)
                p.insert_text((72, 130), 'Second line stays intact', fontsize=12)
                ann = p.add_highlight_annot(p.search_for('original')[0])
                ann.update()
        q = doc.new_page()
        q.insert_text((72, 100), 'Page two must not change', fontsize=12)
        doc.save(path)
        doc.close()
        return path

    def register(self, name, **kwargs):
        src = self.sample_pdf(name, **kwargs)
        result = app.copy_into_library(src, name)
        self.assertFalse(result.get('duplicate'))
        return src, result['id']

    def http(self, method, path, value=None):
        headers = {}
        body = None
        if method == 'POST':
            body = json.dumps(value).encode()
            headers = {'Content-Type':'application/json', 'X-Paper-Isles':'local-client'}
        req = Request(self.base + path, data=body, method=method, headers=headers)
        with urlopen(req, timeout=15) as res:
            return json.loads(res.read().decode())

    def test_01_replace_reindex_undo_preserve_original_and_annotations(self):
        src, docid = self.register('base.pdf')
        original_bytes = src.read_bytes()
        initial = app.pdf_text_edit_preview(docid, 1, 110, 97)
        self.assertEqual(initial['text'], 'This is the original PDF text')
        result = app.pdf_text_edit_apply(docid, dict(
            page=1, x=110, y=97, original=initial['text'],
            replacement='This is the updated PDF text', stamp=initial['stamp']))
        self.assertTrue(result['ok'])
        with fitz.open(app.work_path(docid)) as pdf:
            content = pdf[0].get_text('text')
            self.assertIn('updated PDF text', content)
            self.assertNotIn('original PDF text', content)
            self.assertIn('Second line stays intact', content)
            self.assertIn('Page two must not change', pdf[1].get_text('text'))
            self.assertEqual(len(list(pdf[0].annots())), 1)
        self.assertEqual(src.read_bytes(), original_bytes)
        self.assertEqual(app.get_page_text(docid,1)['text_source'], 'pdf')
        self.assertTrue(app.search_pdf(docid,'updated'))
        self.assertFalse(app.search_pdf(docid,'original'))
        undo = app.pdf_text_edit_undo(docid,result['edit_id'])
        self.assertTrue(undo['ok'])
        with fitz.open(app.work_path(docid)) as pdf:
            self.assertIn('original PDF text', pdf[0].get_text('text'))
            self.assertNotIn('updated PDF text', pdf[0].get_text('text'))
        self.assertEqual(src.read_bytes(), original_bytes)

    def test_02_invalid_edit_does_not_change_pdf(self):
        src, docid = self.register('bad.pdf')
        state = app.pdf_text_edit_preview(docid, 1, 100, 97)
        original_bytes = app.work_path(docid).read_bytes()
        with self.assertRaisesRegex(ValueError,'超过原行宽度'):
            app.pdf_text_edit_apply(docid, dict(
                page=1,x=100,y=97,original=state['text'],stamp=state['stamp'],
                replacement='X' * 200))
        self.assertEqual(original_bytes,app.work_path(docid).read_bytes())
        with self.assertRaisesRegex(ValueError,'已变化'):
            app.pdf_text_edit_apply(docid, dict(
                page=1,x=100,y=97,original='bogus',stamp=state['stamp'],
                replacement='Test'))
        with self.assertRaises(ValueError):
            app.pdf_text_edit_preview(docid,1,float('nan'),100)
        self.assertEqual(src.read_bytes(), app.original_path(docid).read_bytes())

    def test_03_chinese_pdf_replacement(self):
        src, docid=self.register('chinese.pdf',chinese=True)
        state=app.pdf_text_edit_preview(docid,1,90,98)
        self.assertIn('原来',state['text'])
        result=app.pdf_text_edit_apply(docid,dict(page=1,x=90,y=98,original=state['text'],
                                                replacement='这是修改后的PDF文字',stamp=state['stamp']))
        self.assertTrue(result['ok'])
        with fitz.open(app.work_path(docid)) as pdf:
            text=pdf[0].get_text()
            self.assertIn('修改后的PDF文字',text)
            self.assertNotIn('原来的PDF文字',text)
            self.assertIn('第二行保持不变',text)

    def test_04_scanned_pdf_rejected(self):
        src, docid=self.register('scanned.pdf',scanned=True)
        with self.assertRaisesRegex(ValueError,'没有识别到'):
            app.pdf_text_edit_preview(docid,1,90,95)

    def test_05_http_preview_replace_undo(self):
        src, docid=self.register('http.pdf')
        r=self.http('GET',f'/api/doc/{docid}/text-edit-preview?page=1&x=100&y=98')
        self.assertEqual(r['text'],'This is the original PDF text')
        result=self.http('POST',f'/api/doc/{docid}/text-edit',dict(page=1,x=100,y=98,
            original=r['text'],replacement='This is the updated PDF text',stamp=r['stamp']))
        self.assertTrue(result['ok'])
        content=self.http('GET',f'/api/doc/{docid}/text?page=1')['text']
        self.assertIn('updated PDF text',content)
        result=self.http('POST',f'/api/doc/{docid}/undo-text-edit',{'edit_id':result['edit_id']})
        self.assertTrue(result['ok'])
        self.assertIn('original PDF text',self.http('GET',f'/api/doc/{docid}/text?page=1')['text'])

    def test_06_undo_refuses_to_destroy_later_annotations(self):
        src, docid=self.register('undo_guard.pdf')
        preview=app.pdf_text_edit_preview(docid,1,100,98)
        result=app.pdf_text_edit_apply(docid,dict(page=1,x=100,y=98,
            original=preview['text'],replacement='This is the updated PDF text',stamp=preview['stamp']))
        app.annotate(docid,dict(page=2,type='rect',rect=[70,80,300,140]))
        with self.assertRaisesRegex(ValueError,'后 PDF 还有其它变化'):
            app.pdf_text_edit_undo(docid,result['edit_id'])
        with fitz.open(app.work_path(docid)) as pdf:
            self.assertIn('updated PDF text',pdf[0].get_text())
            self.assertEqual(len(list(pdf[1].annots())),1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
