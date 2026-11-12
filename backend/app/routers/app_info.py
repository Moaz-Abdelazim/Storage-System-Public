"""بيانات تعريف المنتج (الاسم) من ملف الإعدادات المركزي `project.config.json`. عام: الواجهة محتاجاها قبل تسجيل الدخول."""
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ..project_config import get_project_config

router = APIRouter(prefix="/api", tags=["App"])


@router.get("/app-info")
def app_info():
    return JSONResponse(get_project_config(), headers={"Cache-Control": "public, max-age=300"})
