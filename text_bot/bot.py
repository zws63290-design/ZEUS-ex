import os
import sys
from pathlib import Path
from dotenv import load_dotenv
import re
import io
import time
import uuid
import json
import base64
import hmac
import hashlib
import asyncio
import threading
import tempfile
import zipfile
import shutil
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Tuple

import requests
from user_agent import generate_user_agent

import discord
from discord import app_commands, ui
from discord.ext import commands

# ============================================================
# الإعدادات العامة
# ============================================================
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN          = os.getenv("DISCORD_TOKEN", "").strip()
OWNER_ID           = int(os.getenv("OWNER_ID", "656783724662226963"))
TEMP_API_URL       = os.getenv("TEMP_API_URL", "").rstrip("/")
BASE_QWEN_URL      = os.getenv("BASE_QWEN_URL", "https://chat.qwen.ai/api/v2").rstrip("/")
BOT_VERSION        = os.getenv("BOT_VERSION", "2.2.0")
GDRIVE_API_KEY     = os.getenv("GDRIVE_API_KEY", "").strip()
SUPPORT_SERVER_URL = os.getenv("SUPPORT_SERVER_URL", "").strip()

MAX_IMAGES_PER_REQUEST = int(os.getenv("MAX_IMAGES_PER_REQUEST", "5"))
MAX_IMAGE_SIZE_MB      = int(os.getenv("MAX_IMAGE_SIZE_MB", "8"))

from utils.storage import (
    admin_adjust_user,
    close_mongo,
    consume_point,
    get_user_profile,
    list_user_profiles,
    load_accounts_data,
    save_accounts_data,
    update_user_settings,
)
from utils.emojis import emoji_manager, emojize, themed_embed, markdown_block

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
bot.remove_command("help")

# ============================================================
# ألوان موحدة
# ============================================================
COLOR_GOLD   = 0xF1C40F
COLOR_GREEN  = 0x57F287
COLOR_RED    = 0xED4245
COLOR_BLUE   = 0x5865F2
COLOR_PURPLE = 0x9B59B6

# ============================================================
# طبقة مركزية لاستبدال placeholders في رسائل discord.py
# ============================================================
_original_response_send_message = discord.InteractionResponse.send_message
async def _patched_response_send_message(self, *args, **kwargs):
    args   = tuple(emojize(a) for a in args)
    kwargs = {k: emojize(v) for k, v in kwargs.items()}
    return await _original_response_send_message(self, *args, **kwargs)
discord.InteractionResponse.send_message = _patched_response_send_message

_original_followup_send = discord.Webhook.send
async def _patched_followup_send(self, *args, **kwargs):
    args   = tuple(emojize(a) for a in args)
    kwargs = {k: emojize(v) for k, v in kwargs.items()}
    return await _original_followup_send(self, *args, **kwargs)
discord.Webhook.send = _patched_followup_send

_original_messageable_send = discord.abc.Messageable.send
async def _patched_messageable_send(self, *args, **kwargs):
    args   = tuple(emojize(a) for a in args)
    kwargs = {k: emojize(v) for k, v in kwargs.items()}
    return await _original_messageable_send(self, *args, **kwargs)
discord.abc.Messageable.send = _patched_messageable_send

# ============================================================
# مساعدات Components V2
# ============================================================
CV2_FLAG = discord.MessageFlags.components_v2

def _sep(visible: bool = True) -> ui.Separator:
    return ui.Separator(visible=visible)

def _txt(content: str) -> ui.TextDisplay:
    # نمرر النص عبر emojize حتى تتحول placeholders إلى إيموجيات حقيقية
    return ui.TextDisplay(content=emojize(content))

def e(name: str) -> str:
    """اختصار لجلب إيموجي واحد من الـ manager."""
    return emoji_manager.placeholder(name)

# ============================================================
# دوال بناء رسائل الحالة — Components V2
# ============================================================

def _container_view(title: str, body: str = "", *, color: int = COLOR_GOLD,
                    footer: str = "") -> ui.LayoutView:
    """
    Container موحد:  عنوان  ─  فاصل مرئي  ─  نص  [─  فاصل  ─  footer]
    كل النصوص تمر عبر emojize تلقائياً داخل _txt().
    """
    lv      = ui.LayoutView()
    items   = [_txt(title), _sep(visible=True)]
    if body:
        items.append(_txt(body))
    if footer:
        items.append(_sep(visible=True))
        items.append(_txt(footer))
    lv.add_item(ui.Container(*items, accent_color=color))
    return lv


async def send_status(channel, title: str, body: str = "", *,
                      error: bool = False) -> discord.Message:
    color = COLOR_RED if error else COLOR_GOLD
    return await channel.send(
        view=_container_view(title, body, color=color),
        flags=CV2_FLAG,
    )


async def edit_status(message: discord.Message, title: str, body: str = "", *,
                      error: bool = False):
    color = COLOR_RED if error else COLOR_GOLD
    await message.edit(
        view=_container_view(title, body, color=color),
        content=None,
        embeds=[],
    )

# ============================================================
# دوال مساعدة عامة
# ============================================================

def fmt_bool(value: bool) -> str:
    icon = e("circlecheck") if value else e("circlex")
    return f"{icon} {'مفعّل' if value else 'معطّل'}"


def get_remaining_time(limit_timestamp: int) -> str:
    now_ts    = int(time.time())
    remaining = limit_timestamp - now_ts
    if remaining <= 0:
        return f"{e('circlecheck')} **نشط**"
    days  = remaining // 86400
    hours = (remaining % 86400) // 3600
    mins  = (remaining % 3600) // 60
    secs  = remaining % 60
    parts = []
    if days:  parts.append(f"{days} يوم")
    if hours: parts.append(f"{hours} ساعة")
    if mins:  parts.append(f"{mins} دقيقة")
    if secs:  parts.append(f"{secs} ثانية")
    return f"{e('circlex')} **{' و '.join(parts)}**"


def get_remaining_time_short(limit_timestamp: int) -> str:
    now_ts    = int(time.time())
    remaining = limit_timestamp - now_ts
    if remaining <= 0:
        return "0s"
    days  = remaining // 86400
    hours = (remaining % 86400) // 3600
    mins  = (remaining % 3600) // 60
    secs  = remaining % 60
    if days:  return f"{days}d {hours}h {mins}m"
    if hours: return f"{hours}h {mins}m {secs}s"
    if mins:  return f"{mins}m {secs}s"
    return f"{secs}s"


def build_extraction_prompt(settings: dict) -> str:
    spacing = (
        "اترك سطرًا فارغًا بين كل فقاعة كلام."
        if settings.get("bubble_spacing", True)
        else "لا تترك أسطرًا فارغة بين الفقاعات."
    )
    sfx = (
        "ضمّن المؤثرات الصوتية والنصوص الجانبية."
        if settings.get("include_sfx", True)
        else "تجاهل المؤثرات الصوتية والنصوص الزخرفية."
    )
    return (
        "استخرج جميع النصوص من هذه الصورة (مانجا/مانهوا) بدقة عالية. "
        "رتبها حسب ترتيب القراءة الصحيح (من اليمين إلى اليسار ومن الأعلى إلى الأسفل). "
        f"{spacing} {sfx} "
        "أعد النص فقط بدون أي تعليقات أو ترجمة."
    )


def make_docx_bytes(text: str) -> io.BytesIO:
    def esc(v: str) -> str:
        return (v or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    paragraphs   = "".join(
        f"<w:p><w:r><w:t xml:space='preserve'>{esc(p)}</w:t></w:r></w:p>"
        for p in text.split("\n")
    )
    document     = (
        "<?xml version='1.0' encoding='UTF-8' standalone='yes'?>"
        "<w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'>"
        f"<w:body>{paragraphs}<w:sectPr/></w:body></w:document>"
    )
    content_types = (
        "<?xml version='1.0' encoding='UTF-8'?>"
        "<Types xmlns='http://schemas.openxmlformats.org/package/2006/content-types'>"
        "<Default Extension='rels' ContentType='application/vnd.openxmlformats-package.relationships+xml'/>"
        "<Default Extension='xml' ContentType='application/xml'/>"
        "<Override PartName='/word/document.xml' "
        "ContentType='application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml'/>"
        "</Types>"
    )
    rels = (
        "<?xml version='1.0' encoding='UTF-8'?>"
        "<Relationships xmlns='http://schemas.openxmlformats.org/package/2006/relationships'>"
        "<Relationship Id='rId1' "
        "Type='http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument' "
        "Target='word/document.xml'/>"
        "</Relationships>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("word/document.xml", document)
    buf.seek(0)
    return buf

# ============================================================
# إدارة الحسابات — Qwen
# ============================================================

def get_base_headers(content_type: str = "application/json", is_app: bool = True) -> dict:
    headers = {}
    if is_app:
        headers["User-Agent"] = (
            "Dalvik/2.1.0 (Linux; U; Android 16; CPH2631 Build/BP2A.250605.015) "
            "AliApp(QWENCHAT/2.7.2) AppType/Release AplusBridgeLite"
        )
    else:
        headers["User-Agent"] = generate_user_agent()
    if content_type:
        headers["Content-Type"] = content_type
    return headers


def is_rate_limited_response(response_obj) -> bool:
    if isinstance(response_obj, dict):
        if response_obj.get("code") == "RateLimited":
            return True
        if response_obj.get("data", {}).get("code") == "RateLimited":
            return True
        if "RateLimited" in json.dumps(response_obj):
            return True
    elif isinstance(response_obj, str):
        if "RateLimited" in response_obj:
            return True
    return False


def create_temp_email() -> Optional[str]:
    try:
        r = requests.post(
            f"{TEMP_API_URL}/email/new",
            headers=get_base_headers(is_app=False),
            json={"min_name_length": 10, "max_name_length": 10},
            timeout=30,
        )
        if r.ok:
            return r.json().get("email")
    except Exception:
        pass
    return None


def signup_qwen(email: str, name: str, password: str) -> bool:
    url     = f"{BASE_QWEN_URL}/auths/signup"
    payload = {
        "name": name, "email": email, "password": password,
        "profile_image_url": "", "oauth_sub": "", "oauth_token": "",
    }
    try:
        r = requests.post(url, json=payload, headers=get_base_headers(), timeout=30)
        return r.status_code in [200, 201]
    except Exception:
        return False


def get_activation_link(email: str, max_attempts: int = 8, delay: int = 3) -> Optional[str]:
    ua = generate_user_agent()
    for _ in range(max_attempts):
        try:
            r = requests.get(
                f"{TEMP_API_URL}/email/{email}/messages",
                headers={"User-Agent": ua}, timeout=30,
            )
            if r.ok:
                for m in r.json():
                    body  = m.get("body_text") or m.get("body_html") or m.get("body") or ""
                    match = re.search(
                        r"https://chat\.qwen\.ai/api/v1/auths/activate\?[^\s\)\"\']+", body
                    )
                    if match:
                        return match.group(0)
        except Exception:
            pass
        time.sleep(delay)
    return None


def activate_account(activation_url: str) -> bool:
    try:
        r = requests.get(
            activation_url,
            headers=get_base_headers(content_type=None, is_app=False),
            timeout=30,
        )
        return r.status_code in [200, 201]
    except Exception:
        return False


def signin_qwen(email: str, password: str) -> Optional[str]:
    url = f"{BASE_QWEN_URL}/auths/signin"
    try:
        r = requests.post(url, json={"email": email, "password": password},
                          headers=get_base_headers(), timeout=30)
        if r.ok:
            data = r.json()
            if data.get("success") and "data" in data:
                return data["data"].get("token")
    except Exception:
        pass
    return None


def create_and_save_new_account(user_id: int) -> str:
    password = "899409576f885e962bb8aecc95ed24efc9b46a0872fdd8e79ed1d6fd72aeb358"
    name     = "User_" + uuid.uuid4().hex[:6]
    for _ in range(5):
        email = create_temp_email()
        if not email:
            continue
        if not signup_qwen(email, name, password):
            continue
        act_link = get_activation_link(email)
        if not act_link:
            continue
        if activate_account(act_link):
            token = signin_qwen(email, password)
            if token:
                data    = load_accounts_data(user_id)
                new_acc = {
                    "email": email, "password": password, "token": token,
                    "image_limit_until": 0, "video_limit_until": 0,
                    "image_edit_limit_until": 0, "ocr_limit_until": 0,
                    "created_at": int(time.time()),
                    "image_count": 0, "video_count": 0,
                    "image_edit_count": 0, "ocr_count": 0,
                }
                data["accounts"].append(new_acc)
                for key in ("active_image_index", "active_video_index",
                            "active_image_edit_index", "active_ocr_index"):
                    if data.get(key, -1) == -1:
                        data[key] = len(data["accounts"]) - 1
                save_accounts_data(user_id, data)
                return token
    raise Exception("فشل إنشاء حساب جديد بعد عدة محاولات.")


SERVICE_KEYS = {
    "image":      ("image_limit_until",     "active_image_index",      "image_count"),
    "video":      ("video_limit_until",      "active_video_index",      "video_count"),
    "image_edit": ("image_edit_limit_until", "active_image_edit_index", "image_edit_count"),
    "ocr":        ("ocr_limit_until",        "active_ocr_index",        "ocr_count"),
    "chat":       ("image_limit_until",      "active_image_index",      "image_count"),
}


def get_valid_qwen_token(user_id: int, service_type: str = "chat") -> str:
    data      = load_accounts_data(user_id)
    now_ts    = int(time.time())
    limit_key, active_key, _ = SERVICE_KEYS.get(service_type, SERVICE_KEYS["chat"])
    active_idx = data.get(active_key, -1)
    if 0 <= active_idx < len(data["accounts"]):
        if data["accounts"][active_idx].get(limit_key, 0) <= now_ts:
            return data["accounts"][active_idx]["token"]
    for idx, acc in enumerate(data["accounts"]):
        if acc.get(limit_key, 0) <= now_ts:
            data[active_key] = idx
            save_accounts_data(user_id, data)
            return acc["token"]
    return create_and_save_new_account(user_id)


def mark_account_rate_limited(user_id: int, service_type: str = "chat"):
    data       = load_accounts_data(user_id)
    unban_time = int((datetime.now(timezone.utc) + timedelta(hours=24)).timestamp())
    limit_key, active_key, _ = SERVICE_KEYS.get(service_type, SERVICE_KEYS["chat"])
    active_idx = data.get(active_key, -1)
    if 0 <= active_idx < len(data["accounts"]):
        data["accounts"][active_idx][limit_key] = unban_time
        data[active_key] = -1
        save_accounts_data(user_id, data)


def increment_account_usage(user_id: int, service_type: str = "chat"):
    data      = load_accounts_data(user_id)
    _, active_key, count_key = SERVICE_KEYS.get(service_type, SERVICE_KEYS["chat"])
    active_idx = data.get(active_key, -1)
    if 0 <= active_idx < len(data["accounts"]):
        data["accounts"][active_idx][count_key] = (
            data["accounts"][active_idx].get(count_key, 0) + 1
        )
        save_accounts_data(user_id, data)


def get_qwen_headers(user_id: int, service_type: str = "chat") -> dict:
    token   = get_valid_qwen_token(user_id, service_type)
    headers = get_base_headers(content_type="application/json; charset=UTF-8")
    headers.update({
        "Accept":          "*/*,text/event-stream",
        "Authorization":   f"Bearer {token}",
        "x-device-id":    "0",
        "source":          "app",
        "Accept-Language": "en-US",
        "Cookie":          f"x-ap=eu-central-1; token={token}",
    })
    return headers

# ============================================================
# رفع الصور إلى Qwen OSS
# ============================================================

def generate_oss_signature(secret_key, method, content_md5, content_type,
                            date, canonical_headers, canonical_resource) -> str:
    string_to_sign = (
        f"{method}\n{content_md5}\n{content_type}\n{date}\n"
        f"{canonical_headers}{canonical_resource}"
    )
    h = hmac.new(secret_key.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha1)
    return base64.b64encode(h.digest()).decode("utf-8")


def upload_image_to_qwen_oss(user_id: int, photo_bytes: bytes,
                              service_type: str = "chat") -> dict:
    file_size = str(len(photo_bytes))
    filename  = f"{uuid.uuid4()}_IMG.jpg"
    sts_url   = "https://chat.qwen.ai/api/v2/files/getstsToken"
    payload   = {"filename": filename, "filetype": "image", "filesize": file_size}
    headers   = get_qwen_headers(user_id, service_type)
    headers["x-request-id"] = str(uuid.uuid4())

    session = requests.Session()
    session.headers.update(headers)
    res = session.post(sts_url, json=payload, timeout=60).json()

    if is_rate_limited_response(res):
        mark_account_rate_limited(user_id, service_type)
        return upload_image_to_qwen_oss(user_id, photo_bytes, service_type)
    if "data" not in res:
        raise Exception(f"فشل تصريح الرفع:\n{json.dumps(res, ensure_ascii=False)}")

    sts               = res["data"]
    access_key_id     = sts["access_key_id"]
    access_key_secret = sts["access_key_secret"]
    security_token    = sts["security_token"]
    file_path         = sts["file_path"]
    file_id           = sts["file_id"]
    bucket            = sts["bucketname"]
    host              = f"{bucket}.{sts['endpoint']}"
    canon_headers     = f"x-oss-security-token:{security_token}\n"

    # Init multipart
    init_url  = f"https://{host}/{file_path}?uploads"
    gmt_date  = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    canon_res = f"/{bucket}/{file_path}?uploads"
    sig       = generate_oss_signature(access_key_secret, "POST", "", "image/jpeg",
                                       gmt_date, canon_headers, canon_res)
    init_hdrs = {
        "Authorization":        f"OSS {access_key_id}:{sig}",
        "User-Agent":           "aliyun-sdk-android/2.9.21",
        "Host":                 host,
        "x-oss-security-token": security_token,
        "Date":                 gmt_date,
        "Content-Type":         "image/jpeg",
        "Content-Length":       "0",
    }
    init_res  = requests.post(init_url, headers=init_hdrs, timeout=60)
    root      = ET.fromstring(init_res.text)
    upload_id = root.find("{*}UploadId").text

    # Upload part
    part_url    = f"https://{host}/{file_path}?uploadId={upload_id}&partNumber=1"
    gmt_date    = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    content_md5 = base64.b64encode(hashlib.md5(photo_bytes).digest()).decode("utf-8")
    canon_res   = f"/{bucket}/{file_path}?partNumber=1&uploadId={upload_id}"
    sig         = generate_oss_signature(access_key_secret, "PUT", content_md5, "image/jpeg",
                                         gmt_date, canon_headers, canon_res)
    part_hdrs   = {
        "Authorization":        f"OSS {access_key_id}:{sig}",
        "User-Agent":           "aliyun-sdk-android/2.9.21",
        "Host":                 host,
        "x-oss-security-token": security_token,
        "Date":                 gmt_date,
        "Content-MD5":          content_md5,
        "Content-Type":         "image/jpeg",
        "Content-Length":       file_size,
    }
    part_res = requests.put(part_url, data=photo_bytes, headers=part_hdrs, timeout=120)
    etag     = part_res.headers.get("ETag", "").replace('"', "")

    # Complete multipart
    complete_url  = f"https://{host}/{file_path}?uploadId={upload_id}"
    gmt_date      = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    complete_body = (
        f"<CompleteMultipartUpload>"
        f"<Part><PartNumber>1</PartNumber><ETag>{etag}</ETag></Part>"
        f"</CompleteMultipartUpload>"
    )
    canon_res     = f"/{bucket}/{file_path}?uploadId={upload_id}"
    sig           = generate_oss_signature(access_key_secret, "POST", "", "image/jpeg",
                                           gmt_date, canon_headers, canon_res)
    complete_hdrs = {
        "Authorization":        f"OSS {access_key_id}:{sig}",
        "User-Agent":           "aliyun-sdk-android/2.9.21",
        "Host":                 host,
        "x-oss-security-token": security_token,
        "Date":                 gmt_date,
        "Content-Type":         "image/jpeg",
        "Content-Length":       str(len(complete_body)),
    }
    requests.post(complete_url, data=complete_body, headers=complete_hdrs, timeout=60)

    signed_url = sts.get("file_url", f"https://{host}/{file_path}")
    return {
        "type":     "image",
        "file":     {"data": {}, "filename": filename, "id": file_id,
                     "meta": {"name": filename}},
        "id":       file_id,
        "filename": filename,
        "name":     filename,
        "url":      signed_url,
    }


def create_new_chat(user_id: int, service_type: str = "chat") -> str:
    url     = "https://chat.qwen.ai/api/v2/chats/new"
    payload = {"chat_mode": "normal", "project_id": ""}
    headers = get_qwen_headers(user_id, service_type)
    headers["x-request-id"] = str(uuid.uuid4())
    res = requests.post(url, json=payload, headers=headers, timeout=60).json()
    if is_rate_limited_response(res):
        mark_account_rate_limited(user_id, service_type)
        return create_new_chat(user_id, service_type)
    if "data" not in res:
        raise Exception(f"فشل إنشاء المحادثة:\n{json.dumps(res, ensure_ascii=False)}")
    return res["data"]["id"]


def delete_chat(user_id: int, chat_id: str, service_type: str = "chat") -> bool:
    if not chat_id:
        return False
    url     = f"https://chat.qwen.ai/api/v2/chats/{chat_id}"
    headers = get_qwen_headers(user_id, service_type)
    headers["x-request-id"] = str(uuid.uuid4())
    headers["Content-Type"] = "application/x-www-form-urlencoded"
    try:
        r = requests.delete(url, headers=headers, timeout=30)
        return r.status_code in [200, 204]
    except Exception:
        return False

# ============================================================
# استخراج النص من صورة
# ============================================================

def extract_text_from_single_image(
    user_id: int,
    image_bytes: bytes,
    image_name: str,
    thinking_enabled: bool,
    settings: dict = None,
    chat_id: str   = None,
) -> str:
    uploaded = upload_image_to_qwen_oss(user_id, image_bytes, "ocr")
    if not chat_id:
        chat_id = create_new_chat(user_id, "ocr")

    url     = f"https://chat.qwen.ai/api/v2/chat/completions?chat_id={chat_id}"
    headers = get_qwen_headers(user_id, "ocr")
    headers["x-request-id"] = str(uuid.uuid4())
    headers["Accept"]        = "*/*,text/event-stream"

    file_payload = {
        "type": "image",
        "file": {"data": {}, "filename": uploaded["filename"],
                 "id": uploaded["id"], "meta": {"name": uploaded["filename"]}},
        "id":           uploaded["id"],
        "url":          uploaded["url"],
        "name":         uploaded["filename"],
        "image_width":  1024,
        "image_height": 1024,
    }

    prompt       = build_extraction_prompt(settings or {})
    message_data = {
        "chat_type": "t2t",
        "content":   prompt,
        "role":      "user",
        "feature_config": {
            "output_schema":    "phase",
            "thinking_enabled": thinking_enabled,
            "thinking_format":  "summary",
            "auto_thinking":    thinking_enabled,
            "auto_search":      False,
        },
        "timestamp":     int(time.time()),
        "sub_chat_type": "t2t",
        "models":        ["qwen3.8-max"],
        "user_action":   "chat",
        "extra":         {"meta": {"subChatType": "t2t"}},
        "files":         [file_payload],
    }

    payload = {
        "stream":             True,
        "incremental_output": True,
        "chat_id":            chat_id,
        "chat_mode":          "normal",
        "model":              "qwen3.8-max",
        "messages":           [message_data],
        "timestamp":          int(time.time()),
        "share_id":           "",
        "version":            "2.1",
        "origin_branch_message_id": "",
    }

    response      = requests.post(url, json=payload, headers=headers,
                                  stream=True, timeout=300)
    full_response = ""

    for line in response.iter_lines():
        if not line:
            continue
        line_str = line.decode("utf-8")
        if is_rate_limited_response(line_str):
            mark_account_rate_limited(user_id, "ocr")
            new_chat_id = create_new_chat(user_id, "ocr")
            return extract_text_from_single_image(
                user_id, image_bytes, image_name, thinking_enabled, settings, new_chat_id
            )
        if line_str.startswith("data: "):
            data_content = line_str[6:].strip()
            if data_content == "[DONE]":
                break
            try:
                data_json = json.loads(data_content)
                if is_rate_limited_response(data_json):
                    mark_account_rate_limited(user_id, "ocr")
                    new_chat_id = create_new_chat(user_id, "ocr")
                    return extract_text_from_single_image(
                        user_id, image_bytes, image_name, thinking_enabled, settings, new_chat_id
                    )
                if data_json.get("choices"):
                    delta = data_json["choices"][0].get("delta", {})
                    if delta.get("phase") == "answer" and delta.get("content"):
                        full_response += delta["content"]
            except Exception:
                continue

    return full_response.strip()

# ============================================================
# معالجة الصور
# ============================================================

def natural_sort_key(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def download_image_from_url(url: str) -> bytes:
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    r = requests.get(url, headers=headers, stream=True, timeout=60)
    r.raise_for_status()
    return r.content


def extract_gdrive_folder_id(url: str) -> Optional[str]:
    for pat in [r"/folders/([a-zA-Z0-9_-]+)",
                r"[?&]id=([a-zA-Z0-9_-]+)",
                r"/drive/([a-zA-Z0-9_-]+)"]:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None


def extract_gdrive_file_id(url: str) -> Optional[str]:
    for pat in [r"/file/d/([a-zA-Z0-9_-]+)",
                r"id=([a-zA-Z0-9_-]+)",
                r"/open\?id=([a-zA-Z0-9_-]+)"]:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None


def list_drive_images(folder_id: str) -> list:
    url    = "https://www.googleapis.com/drive/v3/files"
    params = {
        "q":       f"'{folder_id}' in parents and mimeType contains 'image/'",
        "key":     GDRIVE_API_KEY,
        "fields":  "files(id,name,mimeType)",
        "orderBy": "name",
        "pageSize": 1000,
    }
    r = requests.get(url, params=params, timeout=60)
    if r.status_code != 200:
        raise Exception(f"فشل جلب قائمة الصور: {r.status_code}")
    files = r.json().get("files", [])
    files.sort(key=lambda x: natural_sort_key(x["name"]))
    return files


def download_drive_image(file_id: str) -> bytes:
    url     = f"https://drive.google.com/uc?export=download&id={file_id}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    resp    = requests.get(url, headers=headers, allow_redirects=True,
                           stream=True, timeout=120)
    if resp.status_code != 200 or "text/html" in resp.headers.get("Content-Type", ""):
        url  = f"https://drive.google.com/uc?export=download&confirm=t&id={file_id}"
        resp = requests.get(url, headers=headers, stream=True, timeout=120)
        if resp.status_code != 200:
            raise Exception(f"فشل تحميل الصورة: {file_id}")
    return resp.content


def process_drive_link(link: str) -> List[Tuple[bytes, str]]:
    folder_id = extract_gdrive_folder_id(link)
    file_id   = extract_gdrive_file_id(link)
    if folder_id:
        files = list_drive_images(folder_id)
        return [(download_drive_image(f["id"]), f["name"]) for f in files]
    elif file_id:
        data = download_drive_image(file_id)
        if zipfile.is_zipfile(io.BytesIO(data)):
            return extract_images_from_zip_bytes(data)
        return [(data, f"image_{file_id}.jpg")]
    raise Exception("رابط Drive غير صالح.")


def extract_images_from_zip_bytes(zip_bytes: bytes) -> List[Tuple[bytes, str]]:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = [
            n for n in zf.namelist()
            if n.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))
        ]
        names.sort(key=lambda x: natural_sort_key(os.path.basename(x)))
        return [(zf.read(n), os.path.basename(n)) for n in names]

# ============================================================
# واجهة اختيار الوضع
# ============================================================

class ModeSelectView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=120)
        self.user_id          = user_id
        self.thinking_enabled = None
        self.confirmed        = False

    @discord.ui.button(label="✅ دقة عالية", style=discord.ButtonStyle.success)
    async def high_accuracy(self, interaction: discord.Interaction,
                            button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("هذا الزر ليس لك.", ephemeral=True)
            return
        self.thinking_enabled = True
        self.confirmed        = True
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="⚡ سرعة عالية", style=discord.ButtonStyle.primary)
    async def high_speed(self, interaction: discord.Interaction,
                         button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("هذا الزر ليس لك.", ephemeral=True)
            return
        self.thinking_enabled = False
        self.confirmed        = True
        await interaction.response.defer()
        self.stop()

# ============================================================
# واجهة الإعدادات
# ============================================================

class SettingsView(discord.ui.View):
    def __init__(self, user):
        super().__init__(timeout=240)
        self.user    = user
        self.profile = get_user_profile(user)
        self._rebuild()

    def _rebuild(self):
        self.clear_items()
        s   = self.profile["settings"]
        fmt = s.get("output_format", "txt")

        btn_fmt          = discord.ui.Button(
            label   = f"{e('folder')} الصيغة: {fmt.upper()}",
            style   = discord.ButtonStyle.primary if fmt == "txt" else discord.ButtonStyle.success,
            row     = 0,
        )
        btn_fmt.callback = self.toggle_format
        self.add_item(btn_fmt)

        spacing          = s.get("bubble_spacing", True)
        btn_spacing      = discord.ui.Button(
            label   = f"{e('list')} مسافات الفقاعات: {fmt_bool(spacing)}",
            style   = discord.ButtonStyle.success if spacing else discord.ButtonStyle.secondary,
            row     = 1,
        )
        btn_spacing.callback = self.toggle_spacing
        self.add_item(btn_spacing)

        sfx          = s.get("include_sfx", True)
        btn_sfx      = discord.ui.Button(
            label   = f"{e('music_play')} المؤثرات الصوتية: {fmt_bool(sfx)}",
            style   = discord.ButtonStyle.success if sfx else discord.ButtonStyle.secondary,
            row     = 1,
        )
        btn_sfx.callback = self.toggle_sfx
        self.add_item(btn_sfx)

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("هذه اللوحة ليست لك.", ephemeral=True)
            return False
        return True

    def layout_view(self) -> ui.LayoutView:
        s   = self.profile["settings"]
        fmt = s.get("output_format", "txt")
        lv  = ui.LayoutView()
        lv.add_item(ui.Container(
            _txt(f"## {e('settings')} لوحة الإعدادات"),
            _sep(visible=True),
            _txt(
                f"**{e('folder')} صيغة الملف:** `{fmt.upper()}`\n"
                f"**{e('list')} مسافات الفقاعات:** {fmt_bool(s.get('bubble_spacing', True))}\n"
                f"**{e('music_play')} المؤثرات الصوتية:** {fmt_bool(s.get('include_sfx', True))}"
            ),
            _sep(visible=True),
            _txt("› اضغط الأزرار لتغيير الإعدادات."),
            accent_color=COLOR_BLUE,
        ))
        return lv

    async def toggle_format(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        current      = self.profile["settings"].get("output_format", "txt")
        self.profile = update_user_settings(
            self.user, output_format="docx" if current == "txt" else "txt"
        )
        self._rebuild()
        await interaction.response.edit_message(view=self.layout_view(), attachments=[])

    async def toggle_spacing(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        self.profile = update_user_settings(
            self.user,
            bubble_spacing=not self.profile["settings"].get("bubble_spacing", True),
        )
        self._rebuild()
        await interaction.response.edit_message(view=self.layout_view(), attachments=[])

    async def toggle_sfx(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        self.profile = update_user_settings(
            self.user,
            include_sfx=not self.profile["settings"].get("include_sfx", True),
        )
        self._rebuild()
        await interaction.response.edit_message(view=self.layout_view(), attachments=[])

# ============================================================
# البروفايل
# ============================================================

def build_profile_view(user, profile: dict) -> ui.LayoutView:
    s       = profile["settings"]
    points  = profile.get("points", 0)
    total   = profile.get("total_extractions", 0)
    blocked = profile.get("is_blocked", False)
    status  = f"{e('circlex')} محظور" if blocked else f"{e('circlecheck')} نشط"
    support = f"\n{e('ticket')} [تجديد النقاط]({SUPPORT_SERVER_URL})" if SUPPORT_SERVER_URL else ""

    lv = ui.LayoutView()
    lv.add_item(ui.Container(
        _txt(f"## {e('user')} {user.display_name}"),
        _sep(visible=True),
        _txt(
            f"**{e('star')} النقاط المتاحة:** `{points}`{support}\n\n"
            f"**{e('chartpie')} إجمالي الاستخراجات:** `{total}`\n\n"
            f"**{e('shield')} الحالة:** {status}"
        ),
        _sep(visible=True),
        _txt(
            f"**{e('settings')} الإعدادات الحالية**\n"
            f"الصيغة `{s.get('output_format','txt').upper()}` · "
            f"مسافات {fmt_bool(s.get('bubble_spacing', True))} · "
            f"مؤثرات {fmt_bool(s.get('include_sfx', True))}"
        ),
        accent_color=COLOR_PURPLE,
    ))
    return lv

# ============================================================
# إدارة الحسابات
# ============================================================

class AccountsView(discord.ui.View):
    def __init__(self, user_id: int, page: int = 0):
        super().__init__(timeout=180)
        self.user_id = user_id
        self.page    = page
        self.update_buttons()

    def get_data(self) -> dict:
        return load_accounts_data(self.user_id)

    def update_buttons(self):
        data     = self.get_data()
        accounts = data["accounts"]
        per_page = 5
        start    = self.page * per_page
        end      = start + per_page
        self.clear_items()

        for idx, acc in enumerate(accounts[start:end]):
            real_idx = start + idx
            label    = f"حساب {real_idx + 1}"
            if data.get("active_ocr_index") == real_idx:
                label += f" {e('circlecheck')}"
            btn          = discord.ui.Button(
                label=label, style=discord.ButtonStyle.secondary,
                custom_id=f"acc_{real_idx}", row=0,
            )
            btn.callback = self.make_callback(real_idx)
            self.add_item(btn)

        if self.page > 0:
            prev          = discord.ui.Button(label="⬅️ السابق",
                                              style=discord.ButtonStyle.primary,
                                              custom_id="prev", row=1)
            prev.callback = self.prev_page
            self.add_item(prev)
        if end < len(accounts):
            nxt          = discord.ui.Button(label="التالي ➡️",
                                             style=discord.ButtonStyle.primary,
                                             custom_id="next", row=1)
            nxt.callback = self.next_page
            self.add_item(nxt)

        create          = discord.ui.Button(
            label=f"➕ إنشاء حساب", style=discord.ButtonStyle.success,
            custom_id="create", row=1,
        )
        create.callback = self.create_account
        self.add_item(create)

        status_btn          = discord.ui.Button(
            label=f"{e('chartpie')} الحالة", style=discord.ButtonStyle.secondary,
            custom_id="status_btn", row=1,
        )
        status_btn.callback = self.show_status
        self.add_item(status_btn)

    def make_callback(self, idx: int):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.user_id:
                await interaction.response.send_message("هذه القائمة ليست لك.", ephemeral=True)
                return
            await self.show_account_detail(interaction, idx)
        return callback

    async def prev_page(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("هذه القائمة ليست لك.", ephemeral=True)
            return
        self.page -= 1
        self.update_buttons()
        await interaction.response.edit_message(view=self)

    async def next_page(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("هذه القائمة ليست لك.", ephemeral=True)
            return
        self.page += 1
        self.update_buttons()
        await interaction.response.edit_message(view=self)

    async def create_account(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("هذه القائمة ليست لك.", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            await asyncio.to_thread(create_and_save_new_account, self.user_id)
            self.update_buttons()
            await interaction.followup.send(
                f"{e('circlecheck')} تم إنشاء حساب جديد بنجاح.", ephemeral=True
            )
        except Exception as ex:
            await interaction.followup.send(f"{e('circlex')} فشل: {ex}", ephemeral=True)

    async def show_status(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("هذه القائمة ليست لك.", ephemeral=True)
            return
        data   = self.get_data()
        now_ts = int(time.time())
        active = sum(1 for a in data["accounts"] if a.get("ocr_limit_until", 0) <= now_ts)
        lv     = ui.LayoutView()
        lv.add_item(ui.Container(
            _txt(f"## {e('chartpie')} حالة الحسابات"),
            _sep(visible=True),
            _txt(
                f"**الإجمالي:** `{len(data['accounts'])}`\n"
                f"**نشطة للمعالجة:** `{active}`"
            ),
            accent_color=COLOR_BLUE,
        ))
        await interaction.response.send_message(view=lv, flags=CV2_FLAG, ephemeral=True)

    async def show_account_detail(self, interaction: discord.Interaction, idx: int):
        data = self.get_data()
        if idx >= len(data["accounts"]):
            await interaction.response.send_message("الحساب غير موجود.", ephemeral=True)
            return
        acc       = data["accounts"][idx]
        ocr_limit = acc.get("ocr_limit_until", 0)
        lv        = ui.LayoutView()
        lv.add_item(ui.Container(
            _txt(f"## {e('user')} تفاصيل الحساب {idx + 1}"),
            _sep(visible=True),
            _txt(
                f"**📧 البريد:** `{acc['email'][:40]}`\n"
                f"**🔐 التوكن:** `{acc['token'][:30]}…`\n\n"
                f"**حالة الخدمة:** {get_remaining_time(ocr_limit)}\n"
                f"**عدد الاستخدامات:** `{acc.get('ocr_count', 0)}`"
            ),
            accent_color=COLOR_PURPLE,
        ))
        detail_view = AccountDetailView(self.user_id, idx)
        await interaction.response.send_message(view=lv, flags=CV2_FLAG, ephemeral=True)
        await interaction.followup.send(view=detail_view, ephemeral=True)


class AccountDetailView(discord.ui.View):
    def __init__(self, user_id: int, acc_idx: int):
        super().__init__(timeout=180)
        self.user_id  = user_id
        self.acc_idx  = acc_idx

    @discord.ui.button(label="📖 تعيين كخدمة نشطة", style=discord.ButtonStyle.primary)
    async def set_active(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("هذه القائمة ليست لك.", ephemeral=True)
            return
        data = load_accounts_data(self.user_id)
        if self.acc_idx < len(data["accounts"]):
            data["active_ocr_index"]                            = self.acc_idx
            data["accounts"][self.acc_idx]["ocr_limit_until"]   = 0
            save_accounts_data(self.user_id, data)
            await interaction.response.send_message(
                f"{e('circlecheck')} تم تعيين الحساب كخدمة نشطة.", ephemeral=True
            )
        else:
            await interaction.response.send_message("الحساب غير موجود.", ephemeral=True)

    @discord.ui.button(label="🔄 تجديد التوكن", style=discord.ButtonStyle.secondary)
    async def refresh_token(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("هذه القائمة ليست لك.", ephemeral=True)
            return
        data = load_accounts_data(self.user_id)
        if self.acc_idx < len(data["accounts"]):
            acc       = data["accounts"][self.acc_idx]
            new_token = await asyncio.to_thread(signin_qwen, acc["email"], acc["password"])
            if new_token:
                data["accounts"][self.acc_idx]["token"] = new_token
                save_accounts_data(self.user_id, data)
                await interaction.response.send_message(
                    f"{e('circlecheck')} تم تجديد التوكن.", ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    f"{e('circlex')} فشل تجديد التوكن.", ephemeral=True
                )
        else:
            await interaction.response.send_message("الحساب غير موجود.", ephemeral=True)

    @discord.ui.button(label="🔓 فك الحظر", style=discord.ButtonStyle.success)
    async def unban(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("هذه القائمة ليست لك.", ephemeral=True)
            return
        data = load_accounts_data(self.user_id)
        if self.acc_idx < len(data["accounts"]):
            data["accounts"][self.acc_idx]["ocr_limit_until"] = 0
            save_accounts_data(self.user_id, data)
            await interaction.response.send_message(f"{e('circlecheck')} تم فك الحظر.", ephemeral=True)
        else:
            await interaction.response.send_message("الحساب غير موجود.", ephemeral=True)

    @discord.ui.button(label="🗑️ حذف الحساب", style=discord.ButtonStyle.danger)
    async def delete_account(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("هذه القائمة ليست لك.", ephemeral=True)
            return
        data = load_accounts_data(self.user_id)
        if self.acc_idx < len(data["accounts"]):
            del data["accounts"][self.acc_idx]
            if data.get("active_ocr_index", -1) >= len(data["accounts"]):
                data["active_ocr_index"] = -1
            save_accounts_data(self.user_id, data)
            await interaction.response.send_message("🗑️ تم حذف الحساب.", ephemeral=True)
        else:
            await interaction.response.send_message("الحساب غير موجود.", ephemeral=True)

# ============================================================
# لوحة الإدارة
# ============================================================

def build_admin_panel_view() -> tuple:
    lv = ui.LayoutView()
    lv.add_item(ui.Container(
        _txt(f"## {e('adjustments')} لوحة التحكم — ZEUS Text"),
        _sep(visible=True),
        _txt(
            f"**{e('user')} المستخدمون** — عرض آخر 25 مستخدم\n"
            f"**{e('star')} إضافة نقاط** — أضف رصيداً لمستخدم\n"
            f"**{e('adjustments')} ضبط النقاط** — اضبط الرصيد بدقة\n"
            f"**{e('lock')} منع / فك المنع** — إدارة الوصول\n"
            f"**{e('chartpie')} حسابات OCR** — إدارة حسابات Qwen"
        ),
        _sep(visible=True),
        _txt(f"{e('infocircle')} جميع العمليات مخفية ولا يراها إلا المالك."),
        accent_color=COLOR_RED,
    ))
    return lv, AdminPanelView()


class AdminActionModal(discord.ui.Modal):
    def __init__(self, action: str):
        super().__init__(title="لوحة التحكم")
        self.action  = action
        self.user_id = discord.ui.TextInput(
            label="ID المستخدم", placeholder="123456789 أو منشن", required=True
        )
        self.amount  = discord.ui.TextInput(
            label="النقاط", placeholder="اتركها فارغة للحظر/فك الحظر", required=False
        )
        self.add_item(self.user_id)
        if action in {"add", "reset"}:
            self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message("غير مصرح.", ephemeral=True)
            return
        target = parse_user_id(str(self.user_id.value))
        try:
            amount = int(str(self.amount.value or "0"))
        except ValueError:
            await interaction.response.send_message(
                f"{e('circlex')} قيمة النقاط غير صحيحة.", ephemeral=True
            )
            return

        if self.action == "add":
            p   = admin_adjust_user(target, points_delta=amount)
            msg = f"تمت إضافة `{amount}` نقطة. الرصيد: `{p.get('points', 0)}`"
        elif self.action == "reset":
            p   = admin_adjust_user(target, set_points=amount)
            msg = f"تم ضبط الرصيد إلى `{p.get('points', 0)}`"
        elif self.action == "block":
            admin_adjust_user(target, blocked=True)
            msg = "تم منع المستخدم."
        else:
            admin_adjust_user(target, blocked=False)
            msg = "تم فك المنع."

        lv = _container_view(f"{e('circlecheck')} {msg}", color=COLOR_GREEN)
        await interaction.response.send_message(view=lv, flags=CV2_FLAG, ephemeral=True)


class AdminPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=240)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message("غير مصرح.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="👥 المستخدمون", style=discord.ButtonStyle.primary)
    async def users(self, interaction: discord.Interaction, button: discord.ui.Button):
        users = list_user_profiles(25)
        lines = []
        for u in users:
            st = e("circlex") if u.get("is_blocked") else e("circlecheck")
            lines.append(
                f"{st} `{u.get('user_id')}` — "
                f"**{u.get('display_name', u.get('username', 'Unknown'))}** — "
                f"`{u.get('points', 0)}` نقطة"
            )
        body = "\n".join(lines) or "لا يوجد مستخدمون بعد."
        lv   = ui.LayoutView()
        lv.add_item(ui.Container(
            _txt(f"## {e('chartpie')} آخر المستخدمين"),
            _sep(visible=True),
            _txt(body),
            accent_color=COLOR_BLUE,
        ))
        await interaction.response.send_message(view=lv, flags=CV2_FLAG, ephemeral=True)

    @discord.ui.button(label="➕ إضافة نقاط", style=discord.ButtonStyle.success)
    async def add(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AdminActionModal("add"))

    @discord.ui.button(label="🔧 ضبط النقاط", style=discord.ButtonStyle.secondary)
    async def reset(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AdminActionModal("reset"))

    @discord.ui.button(label="🚫 منع", style=discord.ButtonStyle.danger)
    async def block(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AdminActionModal("block"))

    @discord.ui.button(label="✅ فك المنع", style=discord.ButtonStyle.secondary)
    async def unblock(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AdminActionModal("unblock"))

    @discord.ui.button(label="📋 حسابات OCR", style=discord.ButtonStyle.primary, row=1)
    async def ocr_accounts(self, interaction: discord.Interaction, button: discord.ui.Button):
        data = load_accounts_data(OWNER_ID)
        if not data["accounts"]:
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                await asyncio.to_thread(create_and_save_new_account, OWNER_ID)
            except Exception as ex:
                lv = _container_view(
                    f"{e('circlex')} فشل إنشاء حساب OCR", f"`{ex}`", color=COLOR_RED
                )
                await interaction.followup.send(view=lv, flags=CV2_FLAG, ephemeral=True)
                return
        lv = ui.LayoutView()
        lv.add_item(ui.Container(
            _txt(f"## {e('user')} إدارة حسابات OCR"),
            _sep(visible=True),
            _txt("اختر حسابًا لإدارته."),
            accent_color=COLOR_PURPLE,
        ))
        await interaction.response.send_message(view=lv, flags=CV2_FLAG, ephemeral=True)
        await interaction.followup.send(view=AccountsView(OWNER_ID), ephemeral=True)

    @discord.ui.button(label="📊 حالة OCR", style=discord.ButtonStyle.secondary, row=1)
    async def ocr_status(self, interaction: discord.Interaction, button: discord.ui.Button):
        data       = load_accounts_data(OWNER_ID)
        now_ts     = int(time.time())
        active     = sum(1 for a in data["accounts"] if a.get("ocr_limit_until", 0) <= now_ts)
        total_uses = sum(a.get("ocr_count", 0) for a in data["accounts"])
        lv         = ui.LayoutView()
        lv.add_item(ui.Container(
            _txt(f"## {e('chartpie')} حالة OCR"),
            _sep(visible=True),
            _txt(
                f"**الحسابات الإجمالية:** `{len(data['accounts'])}`\n"
                f"**نشطة:** `{active}`\n"
                f"**إجمالي الاستخدامات:** `{total_uses}`"
            ),
            accent_color=COLOR_BLUE,
        ))
        await interaction.response.send_message(view=lv, flags=CV2_FLAG, ephemeral=True)


def parse_user_id(value: str) -> str:
    m = re.search(r"(\d{15,22})", str(value))
    if not m:
        raise ValueError("missing user id")
    return m.group(1)

# ============================================================
# الأوامر — Slash
# ============================================================

@bot.event
async def on_ready():
    print(f"✅ ZEUS Text Bot جاهز  (v{BOT_VERSION})")
    try:
        synced = await bot.tree.sync()
        print(f"تمت مزامنة {len(synced)} أمر.")
    except Exception as ex:
        print(f"فشل مزامنة الأوامر: {ex}")
    if os.getenv("SYNC_APPLICATION_EMOJIS", "false").lower() == "true":
        try:
            result = await asyncio.to_thread(
                emoji_manager.sync_application_emojis, bot.user.id, BOT_TOKEN
            )
            print(f"[EmojiSetup] {result}")
        except Exception as ex:
            print(f"[EmojiSetup] failed: {ex}")
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching, name="ZEUS Text | /help"
        )
    )


@bot.tree.command(name="extract", description="استخراج النصوص من صور المانجا")
async def extract(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    user_id = interaction.user.id
    profile = get_user_profile(interaction.user)

    if profile.get("is_blocked"):
        lv = _container_view(
            f"{e('lock')} تم منعك من استخدام البوت.", color=COLOR_RED
        )
        await interaction.followup.send(view=lv, flags=CV2_FLAG, ephemeral=True)
        return

    if int(profile.get("points", 0)) <= 0:
        support = f"\n{e('ticket')} [تجديد النقاط]({SUPPORT_SERVER_URL})" if SUPPORT_SERVER_URL else ""
        lv = _container_view(
            f"{e('circlex')} لا تملك نقاطاً كافية.",
            f"كل فصل يستهلك **نقطة واحدة**.{support}",
            color=COLOR_RED,
        )
        await interaction.followup.send(view=lv, flags=CV2_FLAG, ephemeral=True)
        return

    settings = profile["settings"]

    # اختيار الوضع
    mode_lv = ui.LayoutView()
    mode_lv.add_item(ui.Container(
        _txt(f"## {e('settings')} اختر وضع المعالجة"),
        _sep(visible=True),
        _txt(
            "**✅ دقة عالية** — تفكير أعمق، نتائج أدق، وقت أطول قليلاً.\n"
            "**⚡ سرعة عالية** — أسرع، مناسب للفصول الواضحة."
        ),
        accent_color=COLOR_GOLD,
    ))
    mode_btns = ModeSelectView(user_id)
    await interaction.followup.send(view=mode_lv, flags=CV2_FLAG)
    await interaction.followup.send(view=mode_btns)
    await mode_btns.wait()

    if not mode_btns.confirmed:
        lv = _container_view(f"{e('circlex')} تم إلغاء العملية.", color=COLOR_RED)
        await interaction.followup.send(view=lv, flags=CV2_FLAG, ephemeral=True)
        return

    thinking_enabled = mode_btns.thinking_enabled

    # طلب الملفات
    lv = ui.LayoutView()
    lv.add_item(ui.Container(
        _txt(f"## {e('folderopen')} أرسل ملفات الفصل الآن"),
        _sep(visible=True),
        _txt(
            f"**{e('photo')} صور مباشرة** — حتى {MAX_IMAGES_PER_REQUEST} صور (PNG/JPG/WEBP)\n"
            f"**📦 ملف ZIP** — ضغط كل صور الفصل\n"
            f"**🔗 رابط Google Drive** — مجلد أو ملف مباشر"
        ),
        _sep(visible=True),
        _txt(f"{e('clock')} سأحدّث حالة المعالجة أولاً بأول."),
        accent_color=COLOR_GOLD,
    ))
    await interaction.followup.send(view=lv, flags=CV2_FLAG)

    try:
        msg = await bot.wait_for(
            "message",
            check=lambda m: m.author == interaction.user and m.channel == interaction.channel,
            timeout=300,
        )
    except asyncio.TimeoutError:
        lv = _container_view(
            f"{e('clock')} انتهى الوقت.", "أعد الأمر مرة أخرى.", color=COLOR_RED
        )
        await interaction.followup.send(view=lv, flags=CV2_FLAG, ephemeral=True)
        return

    status_msg = await send_status(
        interaction.channel,
        f"{e('clock')} جاري قراءة المصدر",
        "لا تقلق، سأحدّث هذه الرسالة أثناء العمل.",
    )
    images: List[Tuple[bytes, str]] = []

    if msg.attachments:
        if len(msg.attachments) > MAX_IMAGES_PER_REQUEST:
            await edit_status(
                status_msg, f"{e('circlex')} خطأ",
                f"الحد الأقصى {MAX_IMAGES_PER_REQUEST} صور.", error=True,
            )
            return
        for att in msg.attachments:
            fn = att.filename.lower()
            if fn.endswith((".png", ".jpg", ".jpeg", ".webp")):
                images.append((await att.read(), att.filename))
            elif fn.endswith(".zip"):
                images.extend(extract_images_from_zip_bytes(await att.read()))
            else:
                await edit_status(
                    status_msg, f"{e('circlex')} ملف غير مدعوم",
                    f"`{att.filename}`", error=True,
                )
                return

    elif msg.content.startswith("http"):
        link = msg.content.strip()
        await edit_status(
            status_msg, f"{e('clock')} جاري تحميل الرابط",
            "قد يستغرق Drive وقتاً حسب حجم الفصل.",
        )
        try:
            if "drive.google.com" in link:
                images = await asyncio.to_thread(process_drive_link, link)
            else:
                data = await asyncio.to_thread(download_image_from_url, link)
                images = (
                    extract_images_from_zip_bytes(data)
                    if zipfile.is_zipfile(io.BytesIO(data))
                    else [(data, "downloaded_image.jpg")]
                )
        except Exception as ex:
            await edit_status(
                status_msg, f"{e('circlex')} فشل الرابط", f"`{ex}`", error=True
            )
            return
    else:
        await edit_status(
            status_msg, f"{e('circlex')} مصدر غير صالح",
            "أرسل صوراً أو ZIP أو رابطاً صالحاً.", error=True,
        )
        return

    if not images:
        await edit_status(
            status_msg, f"{e('circlex')} لا توجد صور",
            "لم يُعثر على صور صالحة.", error=True,
        )
        return

    images.sort(key=lambda x: natural_sort_key(x[1]))
    await edit_status(
        status_msg, f"{e('clock')} بدأ الاستخراج",
        f"تم العثور على `{len(images)}` صورة.",
    )

    combined_text = ""
    total_images  = len(images)

    for idx, (img_bytes, img_name) in enumerate(images, start=1):
        try:
            text = await asyncio.to_thread(
                extract_text_from_single_image,
                user_id, img_bytes, img_name, thinking_enabled, settings,
            )
            combined_text += f"\n\n{'─'*30}\n## صورة {idx}\n{'─'*30}\n\n{text}"
            await edit_status(
                status_msg, f"{e('clock')} المعالجة مستمرة",
                f"تمت معالجة `{idx}` من `{total_images}` صورة.",
            )
        except Exception as ex:
            await edit_status(
                status_msg, f"{e('circlex')} خطأ أثناء المعالجة",
                f"الصورة `{idx}`: `{ex}`", error=True,
            )
            return

    ok, profile_after = consume_point(user_id)
    if not ok:
        await edit_status(
            status_msg, f"{e('circlex')} لا توجد نقاط",
            "لا يمكن إتمام العملية.", error=True,
        )
        return

    output_dir    = BASE_DIR / "data"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_format = settings.get("output_format", "txt")
    filename      = output_dir / f"extracted_{user_id}_{int(time.time())}.{output_format}"

    if output_format == "docx":
        filename.write_bytes(make_docx_bytes(combined_text).getvalue())
    else:
        filename.write_text(combined_text, encoding="utf-8")

    remaining = profile_after.get("points", 0)
    lv = ui.LayoutView()
    lv.add_item(ui.Container(
        _txt(f"## {e('circlecheck')} اكتمل الاستخراج"),
        _sep(visible=True),
        _txt(
            f"**الصور المعالجة:** `{total_images}` صورة\n"
            f"**الصيغة:** `{output_format.upper()}`\n\n"
            f"**{e('star')} النقاط المتبقية:** `{remaining}`"
        ),
        accent_color=COLOR_GREEN,
    ))

    await status_msg.delete()
    await interaction.channel.send(view=lv, file=discord.File(str(filename)), flags=CV2_FLAG)
    os.remove(filename)
    increment_account_usage(user_id, "ocr")


@bot.tree.command(name="help", description="تعليمات استخدام البوت")
async def help_command(interaction: discord.Interaction):
    support = f"\n{e('ticket')} [سيرفر الدعم]({SUPPORT_SERVER_URL})" if SUPPORT_SERVER_URL else ""
    lv = ui.LayoutView()
    lv.add_item(ui.Container(
        _txt(f"## {e('photo')} ZEUS Text Bot"),
        _sep(visible=True),
        _txt("بوت احترافي لاستخراج نصوص المانجا والمانهوا\nمع نظام نقاط وإعدادات إخراج شخصية."),
        _sep(visible=True),
        _txt(
            f"**{e('photo')} /extract**\n"
            f"استخراج فصل كامل — كل عملية تخصم نقطة واحدة.\n\n"
            f"**{e('settings')} /setting**\n"
            f"تغيير صيغة الإخراج TXT/DOCX، مسافات الفقاعات، والمؤثرات الصوتية.\n\n"
            f"**{e('user')} /profile**\n"
            f"عرض نقاطك وحالتك وإجمالي الاستخراجات.\n\n"
            f"**{e('star')} النقاط**\n"
            f"كل مستخدم يبدأ بـ 5 نقاط مجانية.{support}"
        ),
        _sep(visible=True),
        _txt(f"{e('infocircle')} الإصدار `v{BOT_VERSION}` › ZEUS Text"),
        accent_color=COLOR_GOLD,
    ))
    await interaction.response.send_message(view=lv, flags=CV2_FLAG)


@bot.tree.command(name="setting", description="لوحة إعدادات استخراج النصوص")
async def setting(interaction: discord.Interaction):
    view = SettingsView(interaction.user)
    await interaction.response.send_message(
        view=view.layout_view(), flags=CV2_FLAG, ephemeral=True
    )
    await interaction.followup.send(view=view, ephemeral=True)


@bot.tree.command(name="profile", description="عرض ملفك ونقاطك")
async def profile_cmd(interaction: discord.Interaction):
    profile_data = get_user_profile(interaction.user)
    lv           = build_profile_view(interaction.user, profile_data)
    await interaction.response.send_message(view=lv, flags=CV2_FLAG, ephemeral=True)


@bot.tree.command(name="zx", description="...")
async def zx(interaction: discord.Interaction):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("غير مصرح.", ephemeral=True)
        return
    lv, btn_view = build_admin_panel_view()
    await interaction.response.send_message(view=lv, flags=CV2_FLAG, ephemeral=True)
    await interaction.followup.send(view=btn_view, ephemeral=True)

# ============================================================
# أوامر Prefix !
# ============================================================

@bot.command(name="help", aliases=["مساعدة", "اوامر"])
async def prefix_help(ctx):
    support = f"\n{e('ticket')} [سيرفر الدعم]({SUPPORT_SERVER_URL})" if SUPPORT_SERVER_URL else ""
    lv = ui.LayoutView()
    lv.add_item(ui.Container(
        _txt(f"## {e('photo')} ZEUS Text Bot"),
        _sep(visible=True),
        _txt("بوت احترافي لاستخراج نصوص المانجا والمانهوا."),
        _sep(visible=True),
        _txt(
            f"**{e('photo')} /extract** أو `!extract` — استخراج فصل\n"
            f"**{e('settings')} /setting** أو `!اعدادات` — الإعدادات\n"
            f"**{e('user')} /profile** أو `!بروفايل` — الملف الشخصي{support}"
        ),
        accent_color=COLOR_GOLD,
    ))
    await ctx.send(view=lv, flags=CV2_FLAG)


@bot.command(name="profile", aliases=["بروفايل"])
async def prefix_profile(ctx):
    profile_data = get_user_profile(ctx.author)
    lv           = build_profile_view(ctx.author, profile_data)
    await ctx.send(view=lv, flags=CV2_FLAG)


@bot.command(name="setting", aliases=["settings", "اعدادات"])
async def prefix_setting(ctx):
    view = SettingsView(ctx.author)
    await ctx.send(view=view.layout_view(), flags=CV2_FLAG)
    await ctx.send(view=view)


@bot.command(name="extract", aliases=["استخراج"])
async def prefix_extract(ctx):
    user_id = ctx.author.id
    profile = get_user_profile(ctx.author)

    if profile.get("is_blocked"):
        lv = _container_view(f"{e('lock')} أنت ممنوع من استخدام البوت.", color=COLOR_RED)
        await ctx.reply(view=lv, flags=CV2_FLAG, mention_author=False)
        return

    if int(profile.get("points", 0)) <= 0:
        support = f"\n{e('ticket')} [تجديد النقاط]({SUPPORT_SERVER_URL})" if SUPPORT_SERVER_URL else ""
        lv = _container_view(
            f"{e('circlex')} لا تملك نقاطاً كافية.",
            f"كل فصل يستهلك نقطة واحدة.{support}",
            color=COLOR_RED,
        )
        await ctx.reply(view=lv, flags=CV2_FLAG, mention_author=False)
        return

    settings  = profile["settings"]
    mode_lv   = ui.LayoutView()
    mode_lv.add_item(ui.Container(
        _txt(f"## {e('settings')} اختر وضع المعالجة"),
        _sep(visible=True),
        _txt(
            "**✅ دقة عالية** — تفكير أعمق، نتائج أدق.\n"
            "**⚡ سرعة عالية** — أسرع للفصول الواضحة."
        ),
        accent_color=COLOR_GOLD,
    ))
    mode_btns = ModeSelectView(user_id)
    await ctx.send(view=mode_lv, flags=CV2_FLAG)
    await ctx.send(view=mode_btns)
    await mode_btns.wait()

    if not mode_btns.confirmed:
        lv = _container_view(f"{e('circlex')} تم إلغاء العملية.", color=COLOR_RED)
        await ctx.send(view=lv, flags=CV2_FLAG)
        return

    lv = ui.LayoutView()
    lv.add_item(ui.Container(
        _txt(f"## {e('folderopen')} أرسل ملفات الفصل الآن"),
        _sep(visible=True),
        _txt(
            f"صور مباشرة (حتى {MAX_IMAGES_PER_REQUEST}) أو ZIP أو رابط Google Drive."
        ),
        accent_color=COLOR_GOLD,
    ))
    await ctx.send(view=lv, flags=CV2_FLAG)

    try:
        msg = await bot.wait_for(
            "message",
            check=lambda m: m.author == ctx.author and m.channel == ctx.channel,
            timeout=300,
        )
    except asyncio.TimeoutError:
        lv = _container_view(f"{e('clock')} انتهى الوقت.", "أعد الأمر مرة أخرى.", color=COLOR_RED)
        await ctx.send(view=lv, flags=CV2_FLAG)
        return

    status_msg = await send_status(
        ctx.channel,
        f"{e('clock')} جاري قراءة المصدر",
        "سأحدّث هذه الرسالة أثناء العمل.",
    )
    images: List[Tuple[bytes, str]] = []

    try:
        if msg.attachments:
            if len(msg.attachments) > MAX_IMAGES_PER_REQUEST:
                await edit_status(
                    status_msg, f"{e('circlex')} خطأ",
                    f"الحد الأقصى {MAX_IMAGES_PER_REQUEST} صور.", error=True,
                )
                return
            for att in msg.attachments:
                fn = att.filename.lower()
                if fn.endswith((".png", ".jpg", ".jpeg", ".webp")):
                    images.append((await att.read(), att.filename))
                elif fn.endswith(".zip"):
                    images.extend(extract_images_from_zip_bytes(await att.read()))
                else:
                    await edit_status(
                        status_msg, f"{e('circlex')} ملف غير مدعوم",
                        f"`{att.filename}`", error=True,
                    )
                    return
        elif msg.content.startswith("http"):
            link = msg.content.strip()
            await edit_status(
                status_msg, f"{e('clock')} جاري تحميل الرابط",
                "Drive قد يستغرق وقتاً.",
            )
            if "drive.google.com" in link:
                images = await asyncio.to_thread(process_drive_link, link)
            else:
                data = await asyncio.to_thread(download_image_from_url, link)
                images = (
                    extract_images_from_zip_bytes(data)
                    if zipfile.is_zipfile(io.BytesIO(data))
                    else [(data, "downloaded_image.jpg")]
                )
        else:
            await edit_status(
                status_msg, f"{e('circlex')} مصدر غير صالح",
                "أرسل صوراً أو ZIP أو رابطاً.", error=True,
            )
            return
    except Exception as ex:
        await edit_status(
            status_msg, f"{e('circlex')} فشل قراءة المصدر",
            f"`{ex}`", error=True,
        )
        return

    if not images:
        await edit_status(
            status_msg, f"{e('circlex')} لا توجد صور",
            "لم يُعثر على صور صالحة.", error=True,
        )
        return

    images.sort(key=lambda x: natural_sort_key(x[1]))
    await edit_status(
        status_msg, f"{e('clock')} بدأ الاستخراج",
        f"تم العثور على `{len(images)}` صورة.",
    )

    combined_text = ""
    for idx, (img_bytes, img_name) in enumerate(images, start=1):
        try:
            text = await asyncio.to_thread(
                extract_text_from_single_image,
                user_id, img_bytes, img_name, mode_btns.thinking_enabled, settings,
            )
            combined_text += f"\n\n{'─'*30}\n## صورة {idx}\n{'─'*30}\n\n{text}"
            await edit_status(
                status_msg, f"{e('clock')} المعالجة مستمرة",
                f"تمت معالجة `{idx}` من `{len(images)}` صورة.",
            )
        except Exception as ex:
            await edit_status(
                status_msg, f"{e('circlex')} خطأ",
                f"الصورة `{idx}`: `{ex}`", error=True,
            )
            return

    ok, profile_after = consume_point(user_id)
    if not ok:
        await edit_status(
            status_msg, f"{e('circlex')} لا توجد نقاط",
            "لا يمكن إتمام العملية.", error=True,
        )
        return

    output_dir    = BASE_DIR / "data"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_format = settings.get("output_format", "txt")
    filename      = output_dir / f"extracted_{user_id}_{int(time.time())}.{output_format}"

    if output_format == "docx":
        filename.write_bytes(make_docx_bytes(combined_text).getvalue())
    else:
        filename.write_text(combined_text, encoding="utf-8")

    remaining = profile_after.get("points", 0)
    lv = ui.LayoutView()
    lv.add_item(ui.Container(
        _txt(f"## {e('circlecheck')} اكتمل الاستخراج"),
        _sep(visible=True),
        _txt(
            f"**الصور:** `{len(images)}`  ›  **النقاط المتبقية:** `{remaining}`"
        ),
        accent_color=COLOR_GREEN,
    ))

    await status_msg.delete()
    await ctx.send(view=lv, file=discord.File(str(filename)), flags=CV2_FLAG)
    os.remove(filename)
    increment_account_usage(user_id, "ocr")


@bot.command(name="لوحة", aliases=["admin", "ادارة"])
async def prefix_admin_panel(ctx):
    if ctx.author.id != OWNER_ID:
        await ctx.reply("غير مصرح.", mention_author=False)
        return
    lv, btn_view = build_admin_panel_view()
    await ctx.send(view=lv, flags=CV2_FLAG)
    await ctx.send(view=btn_view)


@bot.command(name="عطه", aliases=["addpoints"])
async def prefix_add_points(ctx, target: str, amount: int):
    if ctx.author.id != OWNER_ID:
        await ctx.reply("غير مصرح.", mention_author=False)
        return
    target_id    = parse_user_id(target)
    profile_data = admin_adjust_user(target_id, points_delta=amount)
    lv = _container_view(
        f"{e('circlecheck')} تمت إضافة `{amount}` نقطة.",
        f"الرصيد الجديد: `{profile_data.get('points', 0)}`",
        color=COLOR_GREEN,
    )
    await ctx.send(view=lv, flags=CV2_FLAG)


@bot.command(name="صفر", aliases=["setpoints"])
async def prefix_set_points(ctx, target: str, amount: int = 0):
    if ctx.author.id != OWNER_ID:
        await ctx.reply("غير مصرح.", mention_author=False)
        return
    target_id    = parse_user_id(target)
    profile_data = admin_adjust_user(target_id, set_points=amount)
    lv = _container_view(
        f"{e('circlecheck')} تم ضبط الرصيد إلى `{profile_data.get('points', 0)}`",
        color=COLOR_GREEN,
    )
    await ctx.send(view=lv, flags=CV2_FLAG)


@bot.command(name="منع", aliases=["blockuser"])
async def prefix_block(ctx, target: str):
    if ctx.author.id != OWNER_ID:
        await ctx.reply("غير مصرح.", mention_author=False)
        return
    admin_adjust_user(parse_user_id(target), blocked=True)
    lv = _container_view(f"{e('lock')} تم منع المستخدم.", color=COLOR_RED)
    await ctx.send(view=lv, flags=CV2_FLAG)


@bot.command(name="فك", aliases=["unblockuser"])
async def prefix_unblock(ctx, target: str):
    if ctx.author.id != OWNER_ID:
        await ctx.reply("غير مصرح.", mention_author=False)
        return
    admin_adjust_user(parse_user_id(target), blocked=False)
    lv = _container_view(f"{e('circlecheck')} تم فك المنع.", color=COLOR_GREEN)
    await ctx.send(view=lv, flags=CV2_FLAG)

# ============================================================
# معالجة الأخطاء
# ============================================================

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction,
                                error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CommandOnCooldown):
        lv = _container_view(
            f"{e('clock')} الأمر قيد التهدئة.",
            f"حاول بعد `{error.retry_after:.1f}` ثانية.",
        )
        await interaction.response.send_message(view=lv, flags=CV2_FLAG, ephemeral=True)
    else:
        lv = _container_view(
            f"{e('alerttriangle')} خطأ غير متوقع", f"`{error}`", color=COLOR_RED
        )
        if interaction.response.is_done():
            await interaction.followup.send(view=lv, flags=CV2_FLAG, ephemeral=True)
        else:
            await interaction.response.send_message(view=lv, flags=CV2_FLAG, ephemeral=True)

# ============================================================
# تشغيل البوت
# ============================================================

if __name__ == "__main__":
    if not BOT_TOKEN:
        raise RuntimeError("DISCORD_TOKEN مفقود من .env")
    try:
        emoji_manager.load()
        bot.run(BOT_TOKEN)
    finally:
        close_mongo()
