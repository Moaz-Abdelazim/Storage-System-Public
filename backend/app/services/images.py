"""
معالجة صور المنتجات (بدون أي تخزين): تحقق من المحتوى الفعلي، تصحيح الاتجاه، تصغير، تحويل WebP، Thumbnail.

- بنفتح الصورة بـ Pillow ونتأكد إنها JPEG / PNG / WebP حسب **محتوى الملف** (مش الامتداد ولا الـ Content-Type).
- حدود: حجم الملف، وعدد البكسلات (حماية من decompression bomb)، وأبعاد الصورة بعد المعالجة.
- الصورة الأساسية: أقصى بُعد MAIN_MAX بكسل، والـ Thumbnail: أقصى بُعد THUMB_MAX، نسبة الأبعاد محفوظة ومفيش تكبير أبدًا.
- الـ Metadata (EXIF / ICC / GPS) بتتشال لأننا بنحفظ بيانات البكسل بس.
- كل الأخطاء بترمي ImageError برسالة عربي صالحة للعرض للمستخدم.
"""
import io
import os
from dataclasses import dataclass

from PIL import Image, ImageOps

try:
    _MB = float(os.getenv("MAX_PRODUCT_IMAGE_MB", "8"))
except ValueError:
    _MB = 8.0
MAX_IMAGE_BYTES = int(max(0.5, _MB) * 1024 * 1024)
MAX_PIXELS = 40_000_000     # ~ 40 ميجابكسل (موبايل 48MP بيعدّي بعد تصغير الجودة؛ فوق كده بنرفض)
MAIN_MAX = 1024
THUMB_MAX = 256
MAIN_QUALITY = 82
THUMB_QUALITY = 75
ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP"}
MAIN_NAME = "main.webp"
THUMB_NAME = "thumb.webp"

Image.MAX_IMAGE_PIXELS = MAX_PIXELS


class ImageError(ValueError):
    """صورة مرفوضة (تالفة / صيغة مش مدعومة / كبيرة أوي). الرسالة عربي للمستخدم."""


@dataclass
class ProcessedImage:
    main: bytes
    thumb: bytes
    width: int
    height: int


def _to_webp(img: Image.Image, max_side: int, quality: int) -> Image.Image:
    out = img.copy()
    out.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)  # بيصغّر بس، ومبيكبّرش، ومحافظ على النسبة
    return out


def _encode(img: Image.Image, quality: int) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=quality, method=4)  # من غير exif/icc => metadata متشالة
    return buf.getvalue()


def process_image(data: bytes) -> ProcessedImage:
    if not data:
        raise ImageError("الملف فاضي")
    if len(data) > MAX_IMAGE_BYTES:
        raise ImageError(f"الصورة كبيرة جدًا (الحد الأقصى {MAX_IMAGE_BYTES / 1024 / 1024:g} ميجا)")
    try:
        img = Image.open(io.BytesIO(data))  # فتح كسول: بيقرا الهيدر بس
        if img.format not in ALLOWED_FORMATS:
            raise ImageError("صيغة الصورة مش مدعومة (المسموح JPEG أو PNG أو WebP)")
        width, height = img.size
        if width < 1 or height < 1 or width * height > MAX_PIXELS:  # قبل أي فك تشفير للبكسلات
            raise ImageError("أبعاد الصورة كبيرة جدًا")
        img.load()  # بيكشف الملف التالف/المقطوع هنا (LOAD_TRUNCATED_IMAGES متقفلة)
    except ImageError:
        raise
    except Image.DecompressionBombError:
        raise ImageError("أبعاد الصورة كبيرة جدًا")
    except Exception:  # OSError / SyntaxError / ValueError / Image.UnidentifiedImageError ...
        raise ImageError("الصورة تالفة أو مش صورة صالحة")

    try:
        img.seek(0)  # صور متحركة (WebP/APNG): أول إطار بس
    except EOFError:
        pass
    try:
        img = ImageOps.exif_transpose(img)  # اتجاه الصورة من EXIF (صور الموبايل)
        has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
        img = img.convert("RGBA" if has_alpha else "RGB")
        main_img = _to_webp(img, MAIN_MAX, MAIN_QUALITY)
        thumb_img = _to_webp(main_img, THUMB_MAX, THUMB_QUALITY)
        return ProcessedImage(
            main=_encode(main_img, MAIN_QUALITY), thumb=_encode(thumb_img, THUMB_QUALITY),
            width=main_img.width, height=main_img.height,
        )
    except ImageError:
        raise
    except Exception:
        raise ImageError("تعذّرت معالجة الصورة")
