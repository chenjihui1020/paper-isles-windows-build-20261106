# -*- coding: utf-8 -*-
"""Conservative, native-PDF single-line text editing for Paper Isles.

This is intentionally NOT an OCR/image overpaint feature. We only modify text
when a precise, horizontal, consistently styled PDF text line can be found.
All operations are performed on the working copy, with a separate snapshot for
safe undo. The caller must serialize calls with the library's PDF lock.
"""
from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path
import shutil
import tempfile
import uuid

try:
    import pymupdf as fitz
except ImportError:
    import fitz


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def stamp(path: Path) -> str:
    state = Path(path).stat()
    return f'{state.st_mtime_ns}:{state.st_size}'


def _validated_page(pdf, number):
    if pdf.needs_pass:
        raise ValueError('加密或需要密码的 PDF 暂不支持原文修改')
    if not isinstance(number, int) or not 1 <= number <= len(pdf):
        raise ValueError('PDF 页码不正确')
    return pdf[number - 1]


def _pick_line(page, x, y):
    try:
        x, y = float(x), float(y)
    except (TypeError, ValueError) as ex:
        raise ValueError('请选择 PDF 页面上的一行文字') from ex
    if not math.isfinite(x) or not math.isfinite(y) or not page.rect.contains(fitz.Point(x, y)):
        raise ValueError('文字坐标超出页面范围')

    contents = page.get_text('dict', sort=False)
    all_lines = []
    for block in contents.get('blocks', []):
        if block.get('type') != 0:
            continue
        all_lines.extend(block.get('lines', []))

    choices = []
    p = fitz.Point(x, y)
    for line in all_lines:
        box = fitz.Rect(line['bbox'])
        if box.contains(p):
            # The smallest matching text line wins in overlapping text blocks.
            choices.append((box.get_area(), line))
    if not choices:
        raise ValueError('没有识别到可编辑的原生 PDF 文字；扫描件请先进行 OCR')

    line = min(choices, key=lambda entry: entry[0])[1]
    direction = line.get('dir', (1, 0))
    if abs(float(direction[0]) - 1) > 0.02 or abs(float(direction[1])) > 0.02:
        raise ValueError('暂不支持竖排或旋转文字的直接替换')

    spans = [s for s in line.get('spans', []) if s.get('text', '').strip()]
    if not spans:
        raise ValueError('该位置没有可编辑的文字内容')
    first = spans[0]
    for other in spans[1:]:
        if (other['font'] != first['font']
                or abs(float(other['size']) - float(first['size'])) > 0.12
                or other['color'] != first['color']
                or abs(other['origin'][1] - first['origin'][1]) > 1.0):
            raise ValueError('该行存在混合字体、公式或上下标，暂不允许覆盖，以免破坏排版')
    original = ''.join(s.get('text', '') for s in line.get('spans', []))
    if not original.strip() or '\ufffd' in original or '\x00' in original:
        raise ValueError('PDF 字体编码无法准确解析，暂不支持安全替换')
    if len(original) > 500:
        raise ValueError('当前文本行过长，暂不支持整体替换')
    box = fitz.Rect(spans[0]['bbox'])
    for s in spans[1:]:
        box |= fitz.Rect(s['bbox'])
    box &= page.rect
    if box.is_empty or box.width < 2 or box.height < 2:
        raise ValueError('该行文字的边界无法可靠识别')

    # Redaction removes ANY touching character. If another text line's bounding
    # box overlaps this one, refuse rather than silently destroying that line.
    for other in all_lines:
        if other is line:
            continue
        for s in other.get('spans', []):
            b = fitz.Rect(s['bbox'])
            if not (box & b).is_empty and (box & b).get_area() > 0.2:
                raise ValueError('附近文字与所选行重叠，直接修改可能误删内容，操作已取消')

    return {
        'text': original,
        'box': box,
        'origin': fitz.Point(first['origin']),
        'font': first['font'],
        'fontsize': float(first['size']),
        'color': int(first['color']),
    }


def _preview(path, page_number, x, y):
    with fitz.open(path) as pdf:
        page = _validated_page(pdf, page_number)
        line = _pick_line(page, x, y)
    return {
        'page': page_number,
        'x': round(float(x), 3),
        'y': round(float(y), 3),
        'text': line['text'],
        'font': line['font'],
        'font_size': round(line['fontsize'], 1),
        'bbox': [round(float(v), 2) for v in line['box']],
        'stamp': stamp(path),
    }


def preview(path: Path, page_number: int, x: float, y: float):
    return _preview(Path(path), page_number, x, y)


def _replacement_font(original_font: str, replacement: str) -> str:
    if any(ord(c) > 255 for c in replacement):
        # Bundled PDF CJK font: handled by MuPDF; no OS font path dependency.
        return 'china-s'
    font = original_font.casefold()
    bold = 'bold' in font or 'black' in font or 'demi' in font
    italic = 'italic' in font or 'oblique' in font
    serif = any(name in font for name in ('times', 'serif', 'roman', 'cambria', 'garamond'))
    if serif:
        if bold and italic:
            return 'tibi'
        if bold:
            return 'tibo'
        if italic:
            return 'tiit'
        return 'tiro'
    if bold and italic:
        return 'hebi'
    if bold:
        return 'hebo'
    if italic:
        return 'heit'
    return 'helv'


def _edit_page(page, line, replacement: str):
    text = replacement
    if len(text) > 500 or '\n' in text or '\r' in text or '\x00' in text:
        raise ValueError('每次只支持编辑一行，替换文字最多 500 字且不能包含换行')
    if any(ord(c) < 32 for c in text):
        raise ValueError('替换文字包含不支持的控制字符')
    if text == line['text']:
        raise ValueError('替换内容没有变化')
    box = line['box']
    size = line['fontsize']
    fontname = _replacement_font(line['font'], text)
    target_size = size
    if text:
        font = fitz.Font(fontname=fontname)
        measured = font.text_length(text, fontsize=size)
        allowed_width = box.width * 1.075  # small visual tolerance for font metrics
        if measured > allowed_width:
            target_size = size * allowed_width / measured
        if target_size < max(5.5, size * 0.76):
            raise ValueError('替换文字明显超过原行宽度。请缩短内容，以免挤压其它排版')
        final_width = font.text_length(text, fontsize=target_size)
        if box.x0 + final_width > page.rect.x1 - 2:
            raise ValueError('新文字会超出页面右侧，请缩短内容')
        if final_width > box.width + 0.1:
            # Some typeface substitutions make the new line slightly wider.
            # Refuse if that extra area reaches text in a neighboring column.
            future = fitz.Rect(box.x0, box.y0, box.x0 + final_width, box.y1)
            for block in page.get_text('dict', sort=False).get('blocks', []):
                if block.get('type') != 0:
                    continue
                for other_line in block.get('lines', []):
                    for sp in other_line.get('spans', []):
                        other = fitz.Rect(sp['bbox'])
                        if (box & other).get_area() > 0.2:
                            continue  # original text to be replaced
                        if (future & other).get_area() > 0.2:
                            raise ValueError('新文字会与邻近文字重叠，请缩短替换内容')

    # Do not overwrite images / vector drawings underneath a text line. No
    # opaque white rectangle is drawn: colored backgrounds are preserved.
    page.add_redact_annot(box, fill=False, cross_out=False)
    page.apply_redactions(
        images=fitz.PDF_REDACT_IMAGE_NONE,
        graphics=fitz.PDF_REDACT_LINE_ART_NONE,
        text=fitz.PDF_REDACT_TEXT_REMOVE,
    )
    if text:
        color = line['color']
        rgb = ((color >> 16 & 255) / 255, (color >> 8 & 255) / 255, (color & 255) / 255)
        page.insert_text(
            (box.x0, line['origin'].y), text,
            fontname=fontname, fontsize=target_size,
            color=rgb, overlay=True,
        )
        # Stop if the replacement failed at extraction (font mapping, etc.).
        if text not in page.get_text('text'):
            raise ValueError('新文字未能正确写入可检索的 PDF 文本层，已取消保存')
    if line['text'] in page.get_text('text'):
        # The same phrase could legitimately occur elsewhere, so only check
        # spatially close to the line as a best-effort diagnostic, not global.
        for word in page.get_text('words'):
            if str(word[4]) in line['text'] and fitz.Rect(word[:4]).intersects(box):
                if text and str(word[4]) in text:
                    continue
                raise ValueError('旧文字残留于编辑区域，已取消保存')
    return round(target_size, 2)


def replace(path: Path, history_dir: Path, *, page: int, x: float, y: float,
            original: str, replacement: str, expected_stamp: str):
    path = Path(path)
    history_dir = Path(history_dir)
    if expected_stamp != stamp(path):
        raise ValueError('PDF 已被其他操作更新，请重新点击文字以获取最新内容')
    if not isinstance(original, str) or not isinstance(replacement, str):
        raise ValueError('文字参数无效')
    with fitz.open(path) as pdf:
        target = _validated_page(pdf, page)
        line = _pick_line(target, x, y)
        if original != line['text']:
            raise ValueError('文字内容已变化，请重新选择该行')
        final_size = _edit_page(target, line, replacement)
        history_dir.mkdir(parents=True, exist_ok=True)
        edit_id = uuid.uuid4().hex
        backup = history_dir / f'{edit_id}.pdf'
        with tempfile.NamedTemporaryFile(prefix='paperisles_edit_', suffix='.pdf',
                                         dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
        try:
            # Write the replacement before touching the current working file.
            pdf.save(temporary, garbage=4, deflate=True, encryption=fitz.PDF_ENCRYPT_KEEP)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    # At this point the source PDF object is closed, safe for an atomic swap.
    try:
        before_sha = file_sha256(path)
        after_sha = file_sha256(temporary)
        shutil.copy2(path, backup)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        backup.unlink(missing_ok=True)
        raise
    return {
        'ok': True,
        'edit_id': edit_id,
        'backup_file': str(backup),
        'before_sha': before_sha,
        'after_sha': after_sha,
        'page': page,
        'font_size': final_size,
    }


def undo(path: Path, backup: Path, expected_current_sha: str):
    path, backup = Path(path), Path(backup)
    if not backup.is_file():
        raise ValueError('该次修改的回退文件不存在，无法安全撤销')
    if file_sha256(path) != expected_current_sha:
        raise ValueError('文字修改后 PDF 还有其它变化。为保护后续批注，无法直接恢复旧快照')
    if not backup.parent.name == 'pdf-text-history':
        raise ValueError('备份路径不合法')
    with tempfile.NamedTemporaryFile(prefix='paperisles_undo_', suffix='.pdf',
                                     dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
    try:
        shutil.copy2(backup, temporary)
        with fitz.open(temporary) as pdf:
            if pdf.needs_pass or len(pdf) == 0:
                raise ValueError('备份 PDF 内容不完整')
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    backup.unlink(missing_ok=True)
    return {'ok': True}
