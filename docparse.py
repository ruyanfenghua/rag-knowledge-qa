# -*- coding: utf-8 -*-
"""文档取文：不依赖第三方库，支持 Word、Excel、PDF、Markdown 与纯文本。

Word 和 Excel 本身就是 zip 包，解压后读里面 XML 即可；
PDF 用 zlib 解压内容流，再按 PDF 自带的 ToUnicode 映射表还原文字。
"""
import html
import re
import zipfile
import zlib
from io import BytesIO


def _utf16be(hexstr):
    try:
        return bytes.fromhex(hexstr).decode("utf-16-be")
    except Exception:
        return ""


def _parse_cmap(text):
    mapping = {}
    for block in re.findall(r"beginbfchar(.*?)endbfchar", text, re.S):
        for src, dst in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
            mapping[int(src, 16)] = _utf16be(dst)
    for block in re.findall(r"beginbfrange(.*?)endbfrange", text, re.S):
        for lo, hi, dst in re.findall(
                r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
            l, h, d = int(lo, 16), int(hi, 16), int(dst, 16)
            for i in range(min(h - l + 1, 4096)):
                try:
                    mapping[l + i] = chr(d + i)
                except Exception:
                    pass
        for lo, hi, arr in re.findall(
                r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*\[(.*?)\]", block, re.S):
            l = int(lo, 16)
            for i, it in enumerate(re.findall(r"<([0-9A-Fa-f]+)>", arr)):
                mapping[l + i] = _utf16be(it)
    return mapping


def _pdf(raw):
    streams = []
    for m in re.finditer(rb"stream\r?\n", raw):
        start = m.end()
        end = raw.find(b"endstream", start)
        if end < 0:
            continue
        try:
            streams.append(zlib.decompress(raw[start:end].rstrip(b"\r\n")))
        except Exception:
            pass

    cmap = {}
    for s in streams:
        if b"beginbfchar" in s or b"beginbfrange" in s:
            cmap.update(_parse_cmap(s.decode("latin-1", "ignore")))

    pieces = []
    for s in streams:
        if b"Tj" not in s and b"TJ" not in s:
            continue
        for m in re.finditer(rb"<([0-9A-Fa-f]+)>\s*Tj", s):
            pieces.append(m.group(1).decode())
        for m in re.finditer(rb"\[(.*?)\]\s*TJ", s, re.S):
            for h in re.findall(rb"<([0-9A-Fa-f]+)>", m.group(1)):
                pieces.append(h.decode())

    out = []
    for h in pieces:
        if len(h) % 4:
            h = h.zfill(len(h) + (4 - len(h) % 4))
        chunk = []
        for i in range(0, len(h), 4):
            chunk.append(cmap.get(int(h[i:i + 4], 16), ""))
        text = "".join(chunk).strip()
        if text:
            out.append(text)
    return "\n".join(out)


def _docx(raw):
    with zipfile.ZipFile(BytesIO(raw)) as z:
        names = z.namelist()
        xml = z.read("word/document.xml").decode("utf-8", "ignore") if "word/document.xml" in names else ""
    out = []
    for para in re.findall(r"<w:p[ >].*?</w:p>", xml, re.S):
        texts = re.findall(r"<w:t[^>]*>(.*?)</w:t>", para, re.S)
        line = html.unescape("".join(texts)).strip()
        if line:
            out.append(line)
    return "\n\n".join(out)


def _xlsx(raw):
    with zipfile.ZipFile(BytesIO(raw)) as z:
        names = z.namelist()
        shared = []
        if "xl/sharedStrings.xml" in names:
            sx = z.read("xl/sharedStrings.xml").decode("utf-8", "ignore")
            for si in re.findall(r"<si>(.*?)</si>", sx, re.S):
                shared.append(html.unescape("".join(re.findall(r"<t[^>]*>(.*?)</t>", si, re.S))))
        sheets = sorted(n for n in names if n.startswith("xl/worksheets/sheet"))
        blocks = []
        for name in sheets:
            sx = z.read(name).decode("utf-8", "ignore")
            rows = []
            for row in re.findall(r"<row[^>]*>(.*?)</row>", sx, re.S):
                cells = []
                for cm in re.finditer(r"<c\b([^>]*)>(.*?)</c>", row, re.S):
                    attrs, body = cm.group(1), cm.group(2)
                    kind = re.search(r't="([^"]+)"', attrs)
                    kind = kind.group(1) if kind else ""
                    v = re.search(r"<v>(.*?)</v>", body, re.S)
                    if not v:
                        inline = "".join(re.findall(r"<t[^>]*>(.*?)</t>", body, re.S))
                        if inline:
                            cells.append(html.unescape(inline).strip())
                        continue
                    val = v.group(1)
                    if kind == "s":
                        try:
                            val = shared[int(val)]
                        except Exception:
                            val = ""
                    cells.append(html.unescape(str(val)).strip())
                line = "  ".join(c for c in cells if c)
                if line.strip():
                    rows.append(line)
            if rows:
                blocks.append("\n".join(rows))
        return "\n\n".join(blocks)


def extract(name, raw):
    """按扩展名取出纯文本。不支持的格式返回空串。"""
    ext = (name.rsplit(".", 1)[-1] if "." in name else "").lower()
    if ext in ("md", "txt", "csv"):
        for enc in ("utf-8", "gb18030", "utf-16"):
            try:
                return raw.decode(enc)
            except Exception:
                continue
        return ""
    if ext == "docx":
        return _docx(raw)
    if ext == "xlsx":
        return _xlsx(raw)
    if ext == "pdf":
        return _pdf(raw)
    return ""
