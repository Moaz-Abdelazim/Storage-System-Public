def format_user_display(user, snapshot_username=None):
    """
    بيرجع اسم المستخدم جاهز للعرض في الفواتير والسجلات:
    - لو الحساب لسه موجود وشغال: يرجع اسمه عادي.
    - لو الحساب موجود بس موقوف (is_active=False): يرجع "الاسم (الحساب موقوف)".
    - لو الحساب اتمسح خالص (مفيش user، بس عندنا نسخة محفوظة من اسمه وقت العملية): يرجع "الاسم (الحساب اتمسح)".
    """
    if user is not None:
        if not user.is_active:
            return f"{user.username} (الحساب موقوف)"
        return user.username
    if snapshot_username:
        return f"{snapshot_username} (الحساب اتمسح)"
    return None
