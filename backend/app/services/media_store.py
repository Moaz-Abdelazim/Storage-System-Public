"""
تخزين ملفات الوسائط (صور المنتجات). طبقة صغيرة: put / read / delete / list_keys.

التنفيذ الحالي على disk المحلي (MEDIA_DIR، الافتراضي backend/media). لو اتغيّر مكان التخزين بعدين (مثلًا blob لنسخة الـ demo)
بيتكتب implementation تاني بنفس الدوال ويتغيّر get_store() بس، ومحدش بيلمس باقي الكود.

- الملف بيتكتب في ملف مؤقت ثم os.replace (ذرّي): مفيش صورة نص مكتوبة تتقدّم.
- المفتاح (key) hex عشوائي 32 حرف، واسم الملف من قايمة ثابتة: مفيش path traversal.
- كل صورة منتج ليها key جديد عند كل تحديث (فالـ URL بيتغيّر والكاش ما يبقاش قديم)، والقديم بيتمسح بعد نجاح الجديد.
"""
import json
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple

_log = logging.getLogger("app.media")

_KEY_RE = re.compile(r"^[0-9a-f]{32}$")
_NAMES = {"main.webp", "thumb.webp"}
_DEFAULT_ROOT = Path(__file__).resolve().parent.parent.parent / "media"


class LocalMediaStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    # ---------- مسارات ----------
    def _dir(self, key: str) -> Path:
        if not _KEY_RE.match(key or ""):
            raise ValueError("invalid media key")
        return self.root / "products" / key[:2] / key

    def _file(self, key: str, name: str) -> Path:
        if name not in _NAMES:
            raise ValueError("invalid media name")
        return self._dir(key) / name

    # ---------- العمليات ----------
    def put(self, key: str, name: str, data: bytes) -> None:
        target = self._file(key, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, target)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def read(self, key: str, name: str) -> Optional[bytes]:
        try:
            return self._file(key, name).read_bytes()
        except FileNotFoundError:
            return None

    def exists(self, key: str, name: str) -> bool:
        return self._file(key, name).is_file()

    def delete(self, key: str) -> None:
        """بيمسح ملفات الـ key (best effort: الفشل بيتسجّل ومبيوقفش العملية)."""
        try:
            d = self._dir(key)
        except ValueError:
            return
        try:
            shutil.rmtree(d)
        except FileNotFoundError:
            return
        except OSError:
            _log.warning("media: could not delete %s", key, exc_info=True)
            return
        try:
            d.parent.rmdir()  # فولدر البادئة لو فضي
        except OSError:
            pass

    def list_keys(self) -> List[str]:
        base = self.root / "products"
        if not base.is_dir():
            return []
        out = []
        for prefix in base.iterdir():
            if prefix.is_dir():
                out.extend(p.name for p in prefix.iterdir() if p.is_dir() and _KEY_RE.match(p.name))
        return sorted(out)

    # ---------- دفعات رفع الصور بالجملة (staging): meta.json لكل دفعة، والصور نفسها بتتخزن بالـ key العادي ----------
    def _staging(self, token: str) -> Path:
        if not _KEY_RE.match(token or ""):
            raise ValueError("invalid batch token")
        return self.root / "staging" / token

    def staging_write(self, token: str, meta: dict) -> None:
        d = self._staging(token)
        d.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(d), prefix=".tmp-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False)
            os.replace(tmp, d / "meta.json")
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def staging_read(self, token: str) -> Optional[dict]:
        try:
            return json.loads((self._staging(token) / "meta.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, json.JSONDecodeError):
            return None

    def staging_delete(self, token: str) -> None:
        try:
            shutil.rmtree(self._staging(token))
        except (FileNotFoundError, ValueError):
            pass
        except OSError:
            _log.warning("media: could not delete staging %s", token, exc_info=True)

    def staging_list(self) -> List[Tuple[str, float]]:
        """(token, آخر تعديل) لكل دفعة موجودة."""
        base = self.root / "staging"
        if not base.is_dir():
            return []
        out = []
        for d in base.iterdir():
            if d.is_dir() and _KEY_RE.match(d.name):
                try:
                    out.append((d.name, (d / "meta.json").stat().st_mtime if (d / "meta.json").exists() else d.stat().st_mtime))
                except OSError:
                    pass
        return out

    def purge_tmp(self) -> int:
        """ملفات .tmp-* المتبقية من كتابة اتقطعت (بتتمسح في check-images --fix)."""
        n = 0
        for base in (self.root / "products", self.root / "staging"):
            if not base.is_dir():
                continue
            for f in base.rglob(".tmp-*"):
                try:
                    f.unlink()
                    n += 1
                except OSError:
                    pass
        return n


def get_store() -> LocalMediaStore:
    return LocalMediaStore(Path(os.getenv("MEDIA_DIR") or _DEFAULT_ROOT))


def valid_key(key: Optional[str]) -> bool:
    return bool(key and _KEY_RE.match(key))
