"""
استخراج الصور من ملف .xlsx وربطها بالصف (والعمود) بتاعها **من إحداثيات الـ anchor** مش من ترتيب الصور.

نوعين صور في Excel:
  1) عايمة فوق الخلية (Insert > Pictures > Place Over Cells): محفوظة في xl/drawings/drawingN.xml بـ anchor (من/إلى خلية + إزاحة)
     أو (خلية + حجم) أو (موضع مطلق). بنحسب **مركز الصورة** من أبعاد الصفوف والأعمدة الفعلية ونحدد الصف/العمود اللي فيهم المركز.
  2) جوه الخلية (Excel 365 - Place in Cell): الخلية فيها vm="N" وبتشاور على rich value (xl/metadata.xml + xl/richData/*) ← ملف الصورة. الخلية معروفة بالظبط.

قواعد الأمان في الربط (بدل التخمين):
  - لو الصورة بتغطي أكتر من صف ومركزها مش جوه صف واضح (أقل من نص ارتفاعها في صف المركز) = مبهمة: مش بتتربط وبتتبلّغ.
  - لو الشيت فيه عمود «الصورة» بنقبل صور العمود ده بس (الصورة بتتحسب فيه لو مركزها فيه أو غطّت نصف عرضه على الأقل)، وأي صورة في عمود تاني بتتبلّغ (مش بتتربط).
  - صور فوق صف العناوين أو برا الجدول، أو linked مش embedded، أو ملفها أكبر من الحد: بتتبلّغ برسالة فيها الصف/الخلية.
  - مفيش أي حاجة بتتعمل بترتيب الصور؛ ولو أكتر من صورة وقعت في نفس الصف، الأولى حسب موضعها (يسار ثم فوق) هي الأساسية والباقي بيتبلّغ.

بنقرا الملف كـ zip + XML مباشرة (من غير openpyxl.load_workbook) فمفيش تحميل للصور كلها في الذاكرة: الصورة بتتقرا لما نحتاجها بس.
بيقرا **أول شيت** (نفس الشيت اللي pandas بيستورد منه).
"""
import io
import posixpath
import re
import zipfile
import xml.etree.ElementTree as ET
from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .images import MAX_IMAGE_BYTES

MAX_IMAGES = 5000
_MAX_XML_BYTES = 30 * 1024 * 1024
_EMU_PER_PX = 9525
_EMU_PER_PT = 12700
_DEFAULT_ROW_PT = 15.0
_DEFAULT_COL_PX = 64.0
_COL_PX_PER_CHAR = 7.0
_MIN_ROW_OVERLAP = 0.5       # نسبة ارتفاع الصورة اللي لازم تقع جوه صف المركز عشان نعتبرها «في الصف ده»

_NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_CELL_RE = re.compile(r"^([A-Za-z]+)(\d+)$")


@dataclass
class LocatedImage:
    member: str                    # مسار الملف جوه الـ zip
    row: int                       # صف Excel (1-based)
    col: int                       # عمود Excel (1-based)
    kind: str                      # "floating" | "in_cell"
    size: int = 0                  # حجم الملف (بايت)
    crc: int = 0                   # CRC بتاع الملف جوه الـ zip (عشان نعرف إن صورتين متطابقتين)


@dataclass
class ImageProblem:
    row: Optional[int]
    message: str


@dataclass
class ExtractedImages:
    by_row: Dict[int, List[LocatedImage]] = field(default_factory=dict)
    problems: List[ImageProblem] = field(default_factory=list)
    total: int = 0                 # كل الصور اللي لقيناها (المربوطة وغير المربوطة)
    _zf: Optional[zipfile.ZipFile] = None  # _Zip

    def read(self, img: LocatedImage) -> bytes:
        assert self._zf is not None
        return self._zf.read(img.member)


class _Zip(zipfile.ZipFile):
    """بعض الكُتّاب (زي XlsxWriter) بيكتبوا أسماء الأجزاء بـ / في الأول (/xl/...) وExcel بيتسامح: بندوّر بالشكلين."""

    def getinfo(self, name):
        try:
            return super().getinfo(name)
        except KeyError:
            return super().getinfo(name.lstrip("/") if name.startswith("/") else "/" + name)


# ---------------------------------------------------------------- أدوات XML
def _ln(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _attr(el, name: str) -> Optional[str]:
    """attribute بغض النظر عن الـ namespace."""
    v = el.get(name)
    if v is not None:
        return v
    for k, val in el.attrib.items():
        if _ln(k) == name:
            return val
    return None


def _read_xml(zf: _Zip, name: str) -> Optional[ET.Element]:
    try:
        info = zf.getinfo(name)
    except KeyError:
        return None
    if info.file_size > _MAX_XML_BYTES:
        raise ValueError(f"{name} أكبر من اللازم")
    return ET.fromstring(zf.read(name))


def _col_to_num(letters: str) -> int:
    n = 0
    for ch in letters.upper():
        n = n * 26 + (ord(ch) - 64)
    return n


def _num_to_col(n: int) -> str:
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s or "A"


def _resolve(base_dir: str, target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(posixpath.join(base_dir, target))


def _rels(zf: _Zip, part: str) -> Dict[str, Tuple[str, str]]:
    """Id -> (Type, resolved Target) لملف علاقات جزء معين."""
    d, f = posixpath.split(part)
    root = _read_xml(zf, posixpath.join(d, "_rels", f + ".rels"))
    out: Dict[str, Tuple[str, str]] = {}
    if root is None:
        return out
    for rel in root:
        if _ln(rel.tag) != "Relationship":
            continue
        if (rel.get("TargetMode") or "").lower() == "external":
            out[rel.get("Id")] = (rel.get("Type") or "", "EXTERNAL:" + (rel.get("Target") or ""))
        else:
            out[rel.get("Id")] = (rel.get("Type") or "", _resolve(d, rel.get("Target") or ""))
    return out


# ---------------------------------------------------------------- أبعاد الشيت
class _Grid:
    """حدود الصفوف/الأعمدة بالـ EMU (مجاميع تراكمية) عشان نحوّل موضع الصورة لصف/عمود."""

    def __init__(self, row_pt: Dict[int, float], default_row_pt: float, col_px: Dict[int, float], default_col_px: float, max_row: int, max_col: int):
        self.row_edges = [0.0]
        for r in range(1, max_row + 1):
            self.row_edges.append(self.row_edges[-1] + row_pt.get(r, default_row_pt) * _EMU_PER_PT)
        self.col_edges = [0.0]
        for c in range(1, max_col + 1):
            self.col_edges.append(self.col_edges[-1] + col_px.get(c, default_col_px) * _EMU_PER_PX)

    def row_top(self, row0: int) -> float:        # row0 = 0-based
        return self.row_edges[min(max(row0, 0), len(self.row_edges) - 1)]

    def col_left(self, col0: int) -> float:
        return self.col_edges[min(max(col0, 0), len(self.col_edges) - 1)]

    def row_at(self, y: float) -> int:            # 1-based
        return max(1, min(bisect_right(self.row_edges, y), len(self.row_edges) - 1))

    def col_at(self, x: float) -> int:
        return max(1, min(bisect_right(self.col_edges, x), len(self.col_edges) - 1))


@dataclass
class _SheetInfo:
    row_pt: Dict[int, float] = field(default_factory=dict)
    default_row_pt: float = _DEFAULT_ROW_PT
    col_px: Dict[int, float] = field(default_factory=dict)
    default_col_px: float = _DEFAULT_COL_PX
    vm_cells: List[Tuple[int, int, int]] = field(default_factory=list)    # (row, col, vm)
    drawing_rid: Optional[str] = None
    max_row: int = 1
    max_col: int = 1


def _scan_sheet(zf: _Zip, path: str) -> _SheetInfo:
    info = _SheetInfo()
    if zf.getinfo(path).file_size > _MAX_XML_BYTES * 4:
        raise ValueError("الشيت كبير جدًا")
    with zf.open(path) as f:
        for _ev, el in ET.iterparse(f, events=("end",)):
            t = _ln(el.tag)
            if t == "c":
                m = _CELL_RE.match(el.get("r") or "")
                if m:
                    r, c = int(m.group(2)), _col_to_num(m.group(1))
                    info.max_row, info.max_col = max(info.max_row, r), max(info.max_col, c)
                    if el.get("vm"):
                        info.vm_cells.append((r, c, int(el.get("vm"))))
            elif t == "row":
                try:
                    r = int(el.get("r"))
                except (TypeError, ValueError):
                    el.clear()
                    continue
                info.max_row = max(info.max_row, r)
                if el.get("hidden") in ("1", "true"):
                    info.row_pt[r] = 0.0
                elif el.get("ht"):
                    try:
                        info.row_pt[r] = float(el.get("ht"))
                    except ValueError:
                        pass
                el.clear()
            elif t == "col":
                try:
                    lo, hi = int(el.get("min")), int(el.get("max"))
                    w = 0.0 if el.get("hidden") in ("1", "true") else float(el.get("width"))
                except (TypeError, ValueError):
                    continue
                hi = min(hi, lo + 2000)
                for c in range(lo, hi + 1):
                    info.col_px[c] = w * _COL_PX_PER_CHAR
                info.max_col = max(info.max_col, hi)
            elif t == "sheetFormatPr":
                try:
                    if el.get("defaultRowHeight"):
                        info.default_row_pt = float(el.get("defaultRowHeight"))
                    if el.get("defaultColWidth"):
                        info.default_col_px = float(el.get("defaultColWidth")) * _COL_PX_PER_CHAR
                except ValueError:
                    pass
            elif t == "drawing":
                info.drawing_rid = _attr(el, "id")
    return info


# ---------------------------------------------------------------- الصور العايمة
def _anchor_box(anchor, grid: _Grid) -> Optional[Tuple[float, float, float, float]]:
    """(left, top, right, bottom) بالـ EMU، أو None لو الـ anchor ناقص."""
    kind = _ln(anchor.tag)

    def pt(el):
        d = {_ln(c.tag): c.text for c in el}
        return int(d.get("col", 0)), int(d.get("colOff", 0)), int(d.get("row", 0)), int(d.get("rowOff", 0))

    frm = next((c for c in anchor if _ln(c.tag) == "from"), None)
    to = next((c for c in anchor if _ln(c.tag) == "to"), None)
    ext = next((c for c in anchor if _ln(c.tag) == "ext"), None)
    pos = next((c for c in anchor if _ln(c.tag) == "pos"), None)
    if kind == "twoCellAnchor" and frm is not None and to is not None:
        c0, co0, r0, ro0 = pt(frm)
        c1, co1, r1, ro1 = pt(to)
        return grid.col_left(c0) + co0, grid.row_top(r0) + ro0, grid.col_left(c1) + co1, grid.row_top(r1) + ro1
    if kind == "oneCellAnchor" and frm is not None and ext is not None:
        c0, co0, r0, ro0 = pt(frm)
        left, top = grid.col_left(c0) + co0, grid.row_top(r0) + ro0
        return left, top, left + int(ext.get("cx", 0)), top + int(ext.get("cy", 0))
    if kind == "absoluteAnchor" and pos is not None and ext is not None:
        left, top = float(pos.get("x", 0)), float(pos.get("y", 0))
        return left, top, left + int(ext.get("cx", 0)), top + int(ext.get("cy", 0))
    return None


def _locate_box(box, grid: _Grid) -> Tuple[Optional[int], Optional[int], bool]:
    """(row, col, ambiguous). ambiguous لو الصورة بتغطي أكتر من صف ومركزها مش جوه صف واضح."""
    left, top, right, bottom = box
    if bottom < top:
        top, bottom = bottom, top
    if right < left:
        left, right = right, left
    cy, cx = (top + bottom) / 2, (left + right) / 2
    row, col = grid.row_at(cy), grid.col_at(cx)
    height = bottom - top
    if height > 0:
        overlap = max(0.0, min(bottom, grid.row_edges[row]) - max(top, grid.row_edges[row - 1]))  # الجزء من الصورة اللي جوه صف المركز
        if overlap / height < _MIN_ROW_OVERLAP:
            return row, col, True
    return row, col, False


def _h_overlap_ok(box, grid: _Grid, col: int) -> bool:
    """هل الصورة تغطي عمود col (1-based) بنسبة كافية؟ (نصف عرض الأصغر بين الصورة والعمود). بيتعامل مع صورة أعرض من عمودها (مركزها بيقع في عمود تاني)."""
    left, _top, right, _bottom = box
    if right < left:
        left, right = right, left
    if col < 1 or col >= len(grid.col_edges):
        return False
    c_left, c_right = grid.col_edges[col - 1], grid.col_edges[col]
    overlap = max(0.0, min(right, c_right) - max(left, c_left))
    smaller = min(right - left, c_right - c_left)
    return smaller > 0 and overlap >= _MIN_ROW_OVERLAP * smaller


def _floating(zf, sheet_path: str, info: _SheetInfo, out: ExtractedImages, found: List[LocatedImage], image_col: Optional[int] = None) -> None:
    if not info.drawing_rid:
        return
    rel = _rels(zf, sheet_path).get(info.drawing_rid)
    if not rel or rel[1].startswith("EXTERNAL:"):
        return
    drawing_path = rel[1]
    root = _read_xml(zf, drawing_path)
    if root is None:
        return
    drels = _rels(zf, drawing_path)
    # أقصى صف/عمود محتاجينه لحساب الحدود (الصور ممكن تتخطى آخر خلية فيها بيانات)
    max_row, max_col = info.max_row, info.max_col
    for anchor in root:
        if _ln(anchor.tag) not in ("twoCellAnchor", "oneCellAnchor", "absoluteAnchor"):
            continue
        for el in anchor.iter():
            if _ln(el.tag) in ("row", "col") and (el.text or "").isdigit():
                v = int(el.text) + 2
                if _ln(el.tag) == "row":
                    max_row = max(max_row, v)
                else:
                    max_col = max(max_col, v)
    grid = _Grid(info.row_pt, info.default_row_pt, info.col_px, info.default_col_px, max_row + 50, max_col + 5)

    for anchor in root:
        if _ln(anchor.tag) not in ("twoCellAnchor", "oneCellAnchor", "absoluteAnchor"):
            continue
        box = _anchor_box(anchor, grid)
        for pic in (e for e in anchor.iter() if _ln(e.tag) == "pic"):
            out.total += 1
            blip = next((e for e in pic.iter() if _ln(e.tag) == "blip"), None)
            embed = _attr(blip, "embed") if blip is not None else None
            if box is None:
                out.problems.append(ImageProblem(None, "صورة مش متحدد مكانها في الشيت (مش ممكن تتربط بمنتج)"))
                continue
            row, col, ambiguous = _locate_box(box, grid)
            if image_col is not None and col != image_col and _h_overlap_ok(box, grid, image_col):
                col = image_col   # صورة أعرض من عمودها: بتتحسب في عمود «الصورة» لأنها بتغطيه بوضوح
            if embed is None or embed not in drels or drels[embed][1].startswith("EXTERNAL:"):
                out.problems.append(ImageProblem(row, "الصورة مربوطة من برا الملف (linked) مش مضمّنة فيه؛ انسخها داخل الملف"))
                continue
            if ambiguous:
                top_row, bottom_row = grid.row_at(min(box[1], box[3])), grid.row_at(max(box[1], box[3]) - 1)
                out.problems.append(ImageProblem(row, f"الصورة أكبر من الصف وبتغطي الصفوف {top_row}–{bottom_row}؛ صغّرها لتناسب خلية المنتج"))
                continue
            found.append(LocatedImage(member=drels[embed][1], row=row, col=col, kind="floating"))


# ---------------------------------------------------------------- صور جوه الخلية (rich value)
def _in_cell(zf, info: _SheetInfo, out: ExtractedImages, found: List[LocatedImage]) -> None:
    if not info.vm_cells:
        return
    try:
        meta = _read_xml(zf, "xl/metadata.xml")
        rv_root = _read_xml(zf, "xl/richData/rdrichvalue.xml")
        st_root = _read_xml(zf, "xl/richData/rdrichvaluestructure.xml")
        rel_root = _read_xml(zf, "xl/richData/richValueRel.xml")
        if meta is None or rv_root is None or st_root is None or rel_root is None:
            raise ValueError("richData ناقصة")
        rel_map = _rels(zf, "xl/richData/richValueRel.xml")
        rel_ids = [_attr(r, "id") for r in rel_root if _ln(r.tag) == "rel"]
        structures = [(s.get("t", ""), [k.get("n", "") for k in s if _ln(k.tag) == "k"]) for s in st_root if _ln(s.tag) == "s"]
        rvs = [(int(rv.get("s", 0)), [(v.text or "") for v in rv if _ln(v.tag) == "v"]) for rv in rv_root if _ln(rv.tag) == "rv"]

        type_names = [mt.get("name", "") for mt in meta.iter() if _ln(mt.tag) == "metadataType"]
        rich_type_idx = type_names.index("XLRICHVALUE") + 1 if "XLRICHVALUE" in type_names else None
        future_rvb: List[int] = []
        for fm in meta:
            if _ln(fm.tag) == "futureMetadata" and fm.get("name") == "XLRICHVALUE":
                for bk in fm:
                    rvb = next((e for e in bk.iter() if _ln(e.tag) == "rvb"), None)
                    future_rvb.append(int(rvb.get("i")) if rvb is not None else -1)
        value_md: List[Optional[Tuple[int, int]]] = []
        for vmd in meta:
            if _ln(vmd.tag) == "valueMetadata":
                for bk in vmd:
                    rc = next((e for e in bk.iter() if _ln(e.tag) == "rc"), None)
                    value_md.append((int(rc.get("t")), int(rc.get("v"))) if rc is not None else None)
    except Exception:
        for r, c, _vm in info.vm_cells:
            out.total += 1
            out.problems.append(ImageProblem(r, f"تعذّرت قراءة الصورة اللي جوه الخلية {_num_to_col(c)}{r}"))
        return

    for r, c, vm in info.vm_cells:
        md = value_md[vm - 1] if 0 < vm <= len(value_md) else None
        if md is None or rich_type_idx is None or md[0] != rich_type_idx or not (0 <= md[1] < len(future_rvb)):
            continue                                   # قيمة معدّلة/نوع بيانات تاني مش صورة
        rv_i = future_rvb[md[1]]
        if not (0 <= rv_i < len(rvs)):
            continue
        s_idx, values = rvs[rv_i]
        if not (0 <= s_idx < len(structures)):
            continue
        s_type, keys = structures[s_idx]
        if "_rvRel:LocalImageIdentifier" not in keys:
            continue                                   # rich value مش صورة محلية (مثلًا نوع بيانات)
        out.total += 1
        try:
            rel_index = int(values[keys.index("_rvRel:LocalImageIdentifier")])
            target = rel_map[rel_ids[rel_index]][1]
            if target.startswith("EXTERNAL:"):
                raise KeyError(target)
            zf.getinfo(target)
        except Exception:
            out.problems.append(ImageProblem(r, f"تعذّرت قراءة الصورة اللي جوه الخلية {_num_to_col(c)}{r}"))
            continue
        found.append(LocatedImage(member=target, row=r, col=c, kind="in_cell"))


# ---------------------------------------------------------------- الواجهة
def _first_sheet_path(zf: _Zip) -> str:
    wb = _read_xml(zf, "xl/workbook.xml")
    if wb is not None:
        for sh in wb.iter():
            if _ln(sh.tag) == "sheet":
                rid = _attr(sh, "id")
                rel = _rels(zf, "xl/workbook.xml").get(rid)
                if rel and not rel[1].startswith("EXTERNAL:"):
                    return rel[1]
                break
    return "xl/worksheets/sheet1.xml"


def has_candidate_images(content: bytes) -> bool:
    """فحص سريع رخيص: هل الملف فيه ميديا أصلًا؟ (لو لأ، متضيعش وقت في التحليل)."""
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            return any(n.lstrip("/").startswith("xl/media/") for n in zf.namelist())
    except zipfile.BadZipFile:
        return False


def extract_images(content: bytes, image_col: Optional[int] = None, first_data_row: int = 2) -> ExtractedImages:
    """
    بيرجّع الصور مجمّعة بالصف (by_row) + المشاكل (problems). مبيرميش استثناء على ملف غريب: بيرجّع problem عامة.
    image_col: رقم عمود «الصورة» (1-based) لو الشيت فيه عمود بالاسم ده؛ None = أي عمود.
    first_data_row: أول صف بيانات (الافتراضي 2 = بعد العناوين).
    """
    out = ExtractedImages()
    try:
        zf = _Zip(io.BytesIO(content))
    except zipfile.BadZipFile:
        return out
    out._zf = zf
    found: List[LocatedImage] = []
    try:
        sheet_path = _first_sheet_path(zf)
        info = _scan_sheet(zf, sheet_path)
        _floating(zf, sheet_path, info, out, found, image_col)
        _in_cell(zf, info, out, found)
    except Exception:
        out.problems.append(ImageProblem(None, "تعذّرت قراءة الصور من الملف (الملف ممكن يكون تالف أو بصيغة غير معتادة)"))
        return out

    if out.total > MAX_IMAGES:
        out.problems.append(ImageProblem(None, f"عدد الصور في الملف كبير جدًا (الحد الأقصى {MAX_IMAGES})"))
        return out

    # فلترة حسب الصف/العمود/الحجم (ترتيب ثابت: بالصف ثم العمود)
    for img in sorted(found, key=lambda i: (i.row, i.col)):
        try:
            zi = zf.getinfo(img.member)
            img.size, img.crc = zi.file_size, zi.CRC
        except KeyError:
            out.problems.append(ImageProblem(img.row, f"ملف الصورة في الصف {img.row} مش موجود جوه الملف"))
            continue
        cell = f"{_num_to_col(img.col)}{img.row}"
        if img.row < first_data_row:
            out.problems.append(ImageProblem(img.row, f"صورة في الخلية {cell} فوق بيانات المنتجات (صف العناوين)؛ مش هتتربط"))
            continue
        if image_col is not None and img.col != image_col:
            out.problems.append(ImageProblem(img.row, f"صورة في الخلية {cell} مش في عمود «الصورة» ({_num_to_col(image_col)}) فمتربطتش"))
            continue
        if img.size > MAX_IMAGE_BYTES:
            out.problems.append(ImageProblem(img.row, f"الصورة في الصف {img.row} كبيرة جدًا (الحد الأقصى {MAX_IMAGE_BYTES / 1024 / 1024:g} ميجا)"))
            continue
        out.by_row.setdefault(img.row, []).append(img)
    return out
