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
from discord import app_commands
from discord.ext import commands

# ============================================================
# الإعدادات العامة — معزولة داخل مجلد text_bot
# ============================================================
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "656783724662226963"))
TEMP_API_URL = os.getenv("TEMP_API_URL", "").rstrip("/")
BASE_QWEN_URL = os.getenv("BASE_QWEN_URL", "https://chat.qwen.ai/api/v2").rstrip("/")
BOT_VERSION = os.getenv("BOT_VERSION", "2.1.0")
GDRIVE_API_KEY = os.getenv("GDRIVE_API_KEY", "").strip()

MAX_IMAGES_PER_REQUEST = int(os.getenv("MAX_IMAGES_PER_REQUEST", "5"))
MAX_IMAGE_SIZE_MB = int(os.getenv("MAX_IMAGE_SIZE_MB", "8"))

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
from utils.emojis import THEMES, emoji_manager, emojize, themed_embed, markdown_block

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
bot.remove_command("help")

# ============================================================
# طبقة مركزية لاستبدال placeholders داخل رسائل discord.py
# ============================================================
def _format_send_kwargs(args, kwargs):
    args = tuple(emojize(arg) for arg in args)
    kwargs = {key: emojize(value) for key, value in kwargs.items()}
    return args, kwargs

_original_response_send_message = discord.InteractionResponse.send_message
async def _patched_response_send_message(self, *args, **kwargs):
    args, kwargs = _format_send_kwargs(args, kwargs)
    return await _original_response_send_message(self, *args, **kwargs)
discord.InteractionResponse.send_message = _patched_response_send_message

_original_followup_send = discord.Webhook.send
async def _patched_followup_send(self, *args, **kwargs):
    args, kwargs = _format_send_kwargs(args, kwargs)
    return await _original_followup_send(self, *args, **kwargs)
discord.Webhook.send = _patched_followup_send

_original_messageable_send = discord.abc.Messageable.send
async def _patched_messageable_send(self, *args, **kwargs):
    args, kwargs = _format_send_kwargs(args, kwargs)
    return await _original_messageable_send(self, *args, **kwargs)
discord.abc.Messageable.send = _patched_messageable_send



# ============================================================
# عرض موحد للرسائل — Emojis + Markdown منسق
# ============================================================
RULE = "\n\n---\n\n"
V2_SEPARATOR_SUPPORTED = all(hasattr(discord.ui, name) for name in ("LayoutView", "Container", "TextDisplay", "Separator"))
SUPPORT_SERVER_URL = os.getenv("SUPPORT_SERVER_URL", "").strip()

def line():
    return RULE

def fmt_bool(value):
    return "مفعّل" if value else "معطّل"


def status_embed(title, description, *, error=False):
    return themed_embed(title=title, description=description, color_name="red" if error else "gold")

def _split_visual_rules(text):
    return [part.strip() for part in emojize(text or "").split(RULE) if part.strip()]

def components_v2_panel(title=None, description=None, *, sections=None, error=False):
    """Build a Components V2 panel only for messages that really need visual sections."""
    if not V2_SEPARATOR_SUPPORTED:
        return None
    container = discord.ui.Container(accent_colour=discord.Colour.red() if error else discord.Colour(THEMES["gold"]["color"]))
    if title:
        container.add_item(discord.ui.TextDisplay(emojize(title)))
    parts = [emojize(part).strip() for part in sections] if sections is not None else _split_visual_rules(description)
    for idx, part in enumerate([p for p in parts if p]):
        if idx > 0:
            container.add_item(discord.ui.Separator(visible=True))
        container.add_item(discord.ui.TextDisplay(part))
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(container)
    return view

async def send_panel(destination, title=None, description=None, *, sections=None, content=None, file=None, view=None, ephemeral=False, error=False):
    panel_view = components_v2_panel(title, description, sections=sections, error=error)
    kwargs = {"content": emojize(content) if content else None}
    if file is not None:
        kwargs["file"] = file
    if ephemeral:
        kwargs["ephemeral"] = True
    if panel_view and view is None:
        kwargs["view"] = panel_view
        return await destination.send(**kwargs)
    embed = themed_embed(title=title, description=description or ("\n\n".join(sections or [])), color_name="red" if error else "gold")
    kwargs.update({"embed": embed, "view": view})
    return await destination.send(**kwargs)

def status_body(stage, detail=None, *, current=None, total=None):
    progress = f"`{current}/{total}`" if current is not None and total else "`...`"
    lines = [f"{emoji_manager.placeholder('clock')} **الحالة:** {stage}", f"{emoji_manager.placeholder('chartpie')} **التقدم:** {progress}"]
    if detail:
        lines.append(f"{emoji_manager.placeholder('infocircle')} {detail}")
    return "\n".join(lines)

async def send_status(channel, title, description=None, *, error=False, current=None, total=None):
    body = status_body(emojize(title), description, current=current, total=total)
    return await channel.send(embed=status_embed("{emoji:clock} مؤشر الاستخراج", body, error=error))

async def edit_status(message, title, description=None, *, error=False, current=None, total=None):
    body = status_body(emojize(title), description, current=current, total=total)
    await message.edit(embed=status_embed("{emoji:clock} مؤشر الاستخراج", body, error=error), content=None, view=None)

def build_extraction_prompt(settings):
    spacing = "اترك سطرًا فارغًا بين كل فقاعة كلام." if settings.get("bubble_spacing", True) else "لا تترك أسطرًا فارغة بين الفقاعات؛ اجعل النص متتابعًا ومنظمًا."
    sfx = "ضمّن المؤثرات الصوتية والنصوص الجانبية كما تظهر." if settings.get("include_sfx", True) else "تجاهل المؤثرات الصوتية والنصوص الزخرفية غير الحوارية قدر الإمكان."
    return (
        "استخرج جميع النصوص من هذه الصورة (مانجا/مانهوا) بدقة عالية. "
        "رتبها حسب ترتيب القراءة الصحيح (من اليمين إلى اليسار ومن الأعلى إلى الأسفل). "
        f"{spacing} {sfx} "
        "أعد النص فقط بدون أي تعليقات إضافية أو ترجمة."
    )

def make_docx_bytes(text):
    def esc(v):
        return (v or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    paragraphs = "".join(f"<w:p><w:r><w:t xml:space='preserve'>{esc(part)}</w:t></w:r></w:p>" for part in text.split("\n"))
    document = f"""<?xml version='1.0' encoding='UTF-8' standalone='yes'?>
<w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'><w:body>{paragraphs}<w:sectPr/></w:body></w:document>"""
    content_types = """<?xml version='1.0' encoding='UTF-8'?><Types xmlns='http://schemas.openxmlformats.org/package/2006/content-types'><Default Extension='rels' ContentType='application/vnd.openxmlformats-package.relationships+xml'/><Default Extension='xml' ContentType='application/xml'/><Override PartName='/word/document.xml' ContentType='application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml'/></Types>"""
    rels = """<?xml version='1.0' encoding='UTF-8'?><Relationships xmlns='http://schemas.openxmlformats.org/package/2006/relationships'><Relationship Id='rId1' Type='http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument' Target='word/document.xml'/></Relationships>"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("word/document.xml", document)
    buffer.seek(0)
    return buffer

# ============================================================
# دوال إدارة الحسابات — التخزين عبر MongoDB في utils/storage.py
# ============================================================
def get_remaining_time(limit_timestamp):
    now_ts = int(time.time())
    remaining = limit_timestamp - now_ts
    if remaining <= 0:
        return emojize("{emoji:circlecheck} **نشط**")
    days = remaining // 86400
    hours = (remaining % 86400) // 3600
    minutes = (remaining % 3600) // 60
    seconds = remaining % 60
    parts = []
    if days > 0:
        parts.append(f"{days} يوم")
    if hours > 0:
        parts.append(f"{hours} ساعة")
    if minutes > 0:
        parts.append(f"{minutes} دقيقة")
    if seconds > 0:
        parts.append(f"{seconds} ثانية")
    return emojize("{emoji:circlex} **" + " و ".join(parts) + "**")

def get_remaining_time_short(limit_timestamp):
    now_ts = int(time.time())
    remaining = limit_timestamp - now_ts
    if remaining <= 0:
        return "0s"
    days = remaining // 86400
    hours = (remaining % 86400) // 3600
    minutes = (remaining % 3600) // 60
    seconds = remaining % 60
    if days > 0:
        return f"{days}d {hours}h {minutes}m"
    elif hours > 0:
        return f"{hours}h {minutes}m {seconds}s"
    elif minutes > 0:
        return f"{minutes}m {seconds}s"
    else:
        return f"{seconds}s"

def get_base_headers(content_type="application/json", is_app=True):
    headers = {}
    if is_app:
        headers['User-Agent'] = "Dalvik/2.1.0 (Linux; U; Android 16; CPH2631 Build/BP2A.250605.015) AliApp(QWENCHAT/2.7.2) AppType/Release AplusBridgeLite"
    else:
        headers['User-Agent'] = generate_user_agent()
    if content_type:
        headers['Content-Type'] = content_type
    return headers

def is_rate_limited_response(response_obj):
    if isinstance(response_obj, dict):
        if response_obj.get("code") == "RateLimited" or response_obj.get("data", {}).get("code") == "RateLimited":
            return True
        if "RateLimited" in json.dumps(response_obj):
            return True
    elif isinstance(response_obj, str):
        if "RateLimited" in response_obj:
            return True
    return False

def create_temp_email():
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

def signup_qwen(email, name, password):
    url = f"{BASE_QWEN_URL}/auths/signup"
    headers = get_base_headers()
    payload = {
        "name": name,
        "email": email,
        "password": password,
        "profile_image_url": "",
        "oauth_sub": "",
        "oauth_token": "",
    }
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=30)
        return r.status_code in [200, 201]
    except Exception:
        return False

def get_activation_link(email, max_attempts=8, delay=3):
    ua = generate_user_agent()
    for _ in range(max_attempts):
        try:
            r = requests.get(
                f"{TEMP_API_URL}/email/{email}/messages",
                headers={"User-Agent": ua},
                timeout=30,
            )
            if r.ok:
                for m in r.json():
                    body = m.get("body_text") or m.get("body_html") or m.get("body") or ""
                    match = re.search(
                        r"https://chat\.qwen\.ai/api/v1/auths/activate\?[^\s\)\"\']+", body
                    )
                    if match:
                        return match.group(0)
        except Exception:
            pass
        time.sleep(delay)
    return None

def activate_account(activation_url):
    try:
        r = requests.get(activation_url, headers=get_base_headers(content_type=None, is_app=False), timeout=30)
        return r.status_code in [200, 201]
    except Exception:
        return False

def signin_qwen(email, password):
    url = f"{BASE_QWEN_URL}/auths/signin"
    headers = get_base_headers()
    payload = {"email": email, "password": password}
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=30)
        if r.ok:
            data = r.json()
            if data.get("success") and "data" in data:
                return data["data"].get("token")
    except Exception:
        pass
    return None

def create_and_save_new_account(user_id):
    password = "899409576f885e962bb8aecc95ed24efc9b46a0872fdd8e79ed1d6fd72aeb358"
    name = "User_" + uuid.uuid4().hex[:6]
    for attempt in range(5):
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
                data = load_accounts_data(user_id)
                new_acc = {
                    "email": email,
                    "password": password,
                    "token": token,
                    "image_limit_until": 0,
                    "video_limit_until": 0,
                    "image_edit_limit_until": 0,
                    "ocr_limit_until": 0,
                    "created_at": int(time.time()),
                    "image_count": 0,
                    "video_count": 0,
                    "image_edit_count": 0,
                    "ocr_count": 0,
                }
                data["accounts"].append(new_acc)
                if data["active_image_index"] == -1:
                    data["active_image_index"] = len(data["accounts"]) - 1
                if data["active_video_index"] == -1:
                    data["active_video_index"] = len(data["accounts"]) - 1
                if data["active_image_edit_index"] == -1:
                    data["active_image_edit_index"] = len(data["accounts"]) - 1
                if data["active_ocr_index"] == -1:
                    data["active_ocr_index"] = len(data["accounts"]) - 1
                save_accounts_data(user_id, data)
                return token
    raise Exception("فشل إنشاء حساب جديد بعد عدة محاولات.")

SERVICE_KEYS = {
    "image": ("image_limit_until", "active_image_index", "image_count"),
    "video": ("video_limit_until", "active_video_index", "video_count"),
    "image_edit": ("image_edit_limit_until", "active_image_edit_index", "image_edit_count"),
    "ocr": ("ocr_limit_until", "active_ocr_index", "ocr_count"),
    "chat": ("image_limit_until", "active_image_index", "image_count"),
}

def get_valid_qwen_token(user_id, service_type="chat"):
    data = load_accounts_data(user_id)
    now_ts = int(time.time())
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

def mark_account_rate_limited(user_id, service_type="chat"):
    data = load_accounts_data(user_id)
    unban_time = int((datetime.now(timezone.utc) + timedelta(hours=24)).timestamp())
    limit_key, active_key, _ = SERVICE_KEYS.get(service_type, SERVICE_KEYS["chat"])
    active_idx = data.get(active_key, -1)
    if 0 <= active_idx < len(data["accounts"]):
        data["accounts"][active_idx][limit_key] = unban_time
        data[active_key] = -1
        save_accounts_data(user_id, data)

def increment_account_usage(user_id, service_type="chat"):
    data = load_accounts_data(user_id)
    _, active_key, count_key = SERVICE_KEYS.get(service_type, SERVICE_KEYS["chat"])
    active_idx = data.get(active_key, -1)
    if 0 <= active_idx < len(data["accounts"]):
        data["accounts"][active_idx][count_key] = data["accounts"][active_idx].get(count_key, 0) + 1
        save_accounts_data(user_id, data)

def get_qwen_headers(user_id, service_type="chat"):
    token = get_valid_qwen_token(user_id, service_type)
    headers = get_base_headers(content_type="application/json; charset=UTF-8")
    headers.update({
        'Accept': "*/*,text/event-stream" if service_type in ["chat", "image", "image_edit", "ocr"] else "application/json",
        'Authorization': f"Bearer {token}",
        'x-device-id': "0",
        'source': "app",
        'Accept-Language': "en-US",
        'Cookie': f"x-ap=eu-central-1; token={token}",
    })
    return headers

# ============================================================
# دوال رفع الصور إلى Qwen OSS
# ============================================================
def generate_oss_signature(secret_key, method, content_md5, content_type, date, canonical_headers, canonical_resource):
    string_to_sign = f"{method}\n{content_md5}\n{content_type}\n{date}\n{canonical_headers}{canonical_resource}"
    h = hmac.new(secret_key.encode('utf-8'), string_to_sign.encode('utf-8'), hashlib.sha1)
    return base64.b64encode(h.digest()).decode('utf-8')

def upload_image_to_qwen_oss(user_id, photo_bytes, service_type="chat"):
    file_size = str(len(photo_bytes))
    filename = f"{uuid.uuid4()}_IMG.jpg"
    sts_url = "https://chat.qwen.ai/api/v2/files/getstsToken"
    payload = {"filename": filename, "filetype": "image", "filesize": file_size}
    headers = get_qwen_headers(user_id, service_type)
    headers['x-request-id'] = str(uuid.uuid4())
    session = requests.Session()
    session.headers.update(headers)
    res = session.post(sts_url, json=payload, timeout=60).json()
    if is_rate_limited_response(res):
        mark_account_rate_limited(user_id, service_type)
        return upload_image_to_qwen_oss(user_id, photo_bytes, service_type)
    if "data" not in res:
        raise Exception(f"فشل تصريح الرفع:\n{json.dumps(res, ensure_ascii=False)}")
    sts_res = res["data"]
    access_key_id = sts_res["access_key_id"]
    access_key_secret = sts_res["access_key_secret"]
    security_token = sts_res["security_token"]
    file_path = sts_res["file_path"]
    file_id = sts_res["file_id"]
    bucket = sts_res["bucketname"]
    host = f"{bucket}.{sts_res['endpoint']}"

    init_url = f"https://{host}/{file_path}?uploads"
    gmt_date = datetime.now(timezone.utc).strftime('%a, %d %b %Y %H:%M:%S GMT')
    canon_headers = f"x-oss-security-token:{security_token}\n"
    canon_resource = f"/{bucket}/{file_path}?uploads"
    sig = generate_oss_signature(access_key_secret, "POST", "", "image/jpeg", gmt_date, canon_headers, canon_resource)
    init_headers = {
        'Authorization': f'OSS {access_key_id}:{sig}',
        'User-Agent': 'aliyun-sdk-android/2.9.21',
        'Host': host,
        'x-oss-security-token': security_token,
        'Date': gmt_date,
        'Content-Type': 'image/jpeg',
        'Content-Length': '0',
    }
    init_res = requests.post(init_url, headers=init_headers, timeout=60)
    root = ET.fromstring(init_res.text)
    upload_id = root.find('{*}UploadId').text

    part_url = f"https://{host}/{file_path}?uploadId={upload_id}&partNumber=1"
    gmt_date = datetime.now(timezone.utc).strftime('%a, %d %b %Y %H:%M:%S GMT')
    content_md5 = base64.b64encode(hashlib.md5(photo_bytes).digest()).decode('utf-8')
    canon_resource = f"/{bucket}/{file_path}?partNumber=1&uploadId={upload_id}"
    sig = generate_oss_signature(access_key_secret, "PUT", content_md5, "image/jpeg", gmt_date, canon_headers, canon_resource)
    part_headers = {
        'Authorization': f'OSS {access_key_id}:{sig}',
        'User-Agent': 'aliyun-sdk-android/2.9.21',
        'Host': host,
        'x-oss-security-token': security_token,
        'Date': gmt_date,
        'Content-MD5': content_md5,
        'Content-Type': 'image/jpeg',
        'Content-Length': file_size,
    }
    part_res = requests.put(part_url, data=photo_bytes, headers=part_headers, timeout=120)
    etag = part_res.headers.get("ETag", "").replace('"', '')

    complete_url = f"https://{host}/{file_path}?uploadId={upload_id}"
    gmt_date = datetime.now(timezone.utc).strftime('%a, %d %b %Y %H:%M:%S GMT')
    complete_body = f"<CompleteMultipartUpload><Part><PartNumber>1</PartNumber><ETag>{etag}</ETag></Part></CompleteMultipartUpload>"
    canon_resource = f"/{bucket}/{file_path}?uploadId={upload_id}"
    sig = generate_oss_signature(access_key_secret, "POST", "", "image/jpeg", gmt_date, canon_headers, canon_resource)
    complete_headers = {
        'Authorization': f'OSS {access_key_id}:{sig}',
        'User-Agent': 'aliyun-sdk-android/2.9.21',
        'Host': host,
        'x-oss-security-token': security_token,
        'Date': gmt_date,
        'Content-Type': 'image/jpeg',
        'Content-Length': str(len(complete_body)),
    }
    requests.post(complete_url, data=complete_body, headers=complete_headers, timeout=60)
    signed_url = sts_res.get("file_url", f"https://{host}/{file_path}")
    return {
        "type": "image",
        "file": {"data": {}, "filename": filename, "id": file_id, "meta": {"name": filename}},
        "id": file_id,
        "filename": filename,
        "name": filename,
        "url": signed_url,
    }

def create_new_chat(user_id, service_type="chat"):
    url = "https://chat.qwen.ai/api/v2/chats/new"
    payload = {"chat_mode": "normal", "project_id": ""}
    headers = get_qwen_headers(user_id, service_type)
    headers['x-request-id'] = str(uuid.uuid4())
    res = requests.post(url, json=payload, headers=headers, timeout=60).json()
    if is_rate_limited_response(res):
        mark_account_rate_limited(user_id, service_type)
        return create_new_chat(user_id, service_type)
    if "data" not in res:
        raise Exception(f"فشل إنشاء المحادثة:\n{json.dumps(res, ensure_ascii=False)}")
    return res["data"]["id"]

def delete_chat(user_id, chat_id, service_type="chat"):
    if not chat_id:
        return False
    url = f"https://chat.qwen.ai/api/v2/chats/{chat_id}"
    headers = get_qwen_headers(user_id, service_type)
    headers['x-request-id'] = str(uuid.uuid4())
    headers['Content-Type'] = "application/x-www-form-urlencoded"
    try:
        r = requests.delete(url, headers=headers, timeout=30)
        return r.status_code in [200, 204]
    except Exception:
        return False

# ============================================================
# استخراج النص من صورة واحدة
# ============================================================
def extract_text_from_single_image(user_id, image_bytes, image_name, thinking_enabled, settings=None, chat_id=None):
    uploaded = upload_image_to_qwen_oss(user_id, image_bytes, "ocr")
    if not chat_id:
        chat_id = create_new_chat(user_id, "ocr")

    url = f"https://chat.qwen.ai/api/v2/chat/completions?chat_id={chat_id}"
    headers = get_qwen_headers(user_id, "ocr")
    headers['x-request-id'] = str(uuid.uuid4())
    headers['Accept'] = "*/*,text/event-stream"

    file_payload = {
        "type": "image",
        "file": {"data": {}, "filename": uploaded["filename"], "id": uploaded["id"], "meta": {"name": uploaded["filename"]}},
        "id": uploaded["id"],
        "url": uploaded["url"],
        "name": uploaded["filename"],
        "image_width": 1024,
        "image_height": 1024,
    }

    prompt = build_extraction_prompt(settings or {})

    message_data = {
        "chat_type": "t2t",
        "content": prompt,
        "role": "user",
        "feature_config": {
            "output_schema": "phase",
            "thinking_enabled": thinking_enabled,
            "thinking_format": "summary",
            "auto_thinking": thinking_enabled,
            "auto_search": False,
        },
        "timestamp": int(time.time()),
        "sub_chat_type": "t2t",
        "models": ["qwen3.8-max"],
        "user_action": "chat",
        "extra": {"meta": {"subChatType": "t2t"}},
        "files": [file_payload]
    }

    payload = {
        "stream": True,
        "incremental_output": True,
        "chat_id": chat_id,
        "chat_mode": "normal",
        "model": "qwen3.8-max",
        "messages": [message_data],
        "timestamp": int(time.time()),
        "share_id": "",
        "version": "2.1",
        "origin_branch_message_id": "",
    }

    response = requests.post(url, json=payload, headers=headers, stream=True, timeout=300)
    full_response = ""
    for line in response.iter_lines():
        if not line:
            continue
        line_str = line.decode('utf-8')
        if is_rate_limited_response(line_str):
            mark_account_rate_limited(user_id, "ocr")
            new_chat_id = create_new_chat(user_id, "ocr")
            return extract_text_from_single_image(user_id, image_bytes, image_name, thinking_enabled, settings, new_chat_id)
        if line_str.startswith("data: "):
            data_content = line_str[6:].strip()
            if data_content == "[DONE]":
                break
            try:
                data_json = json.loads(data_content)
                if is_rate_limited_response(data_json):
                    mark_account_rate_limited(user_id, "ocr")
                    new_chat_id = create_new_chat(user_id, "ocr")
                    return extract_text_from_single_image(user_id, image_bytes, image_name, thinking_enabled, settings, new_chat_id)
                if "choices" in data_json and len(data_json["choices"]) > 0:
                    delta = data_json["choices"][0].get("delta", {})
                    if delta.get("phase") == "answer" and delta.get("content"):
                        full_response += delta["content"]
            except Exception:
                continue
    return full_response.strip()

# ============================================================
# دوال معالجة الصور من مصادر مختلفة
# ============================================================
def natural_sort_key(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', s)]

def download_image_from_url(url):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    r = requests.get(url, headers=headers, stream=True, timeout=60)
    r.raise_for_status()
    return r.content

def sanitize_output_stem(value, default="zeus"):
    value = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", value or "").strip()
    value = re.sub(r"\s+", "_", value).strip("._")
    return (value or default)[:80]

def unique_output_path(directory, stem, suffix):
    stem = sanitize_output_stem(stem)
    candidate = directory / f"{stem}.{suffix}"
    counter = 2
    while candidate.exists():
        candidate = directory / f"{stem}_{counter}.{suffix}"
        counter += 1
    return candidate


def extract_gdrive_folder_id(url):
    patterns = [
        r'/folders/([a-zA-Z0-9_-]+)',
        r'[?&]id=([a-zA-Z0-9_-]+)',
        r'/drive/([a-zA-Z0-9_-]+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None

def extract_gdrive_file_id(url):
    patterns = [
        r'/file/d/([a-zA-Z0-9_-]+)',
        r'id=([a-zA-Z0-9_-]+)',
        r'/open\?id=([a-zA-Z0-9_-]+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None

def get_drive_item_name(item_id):
    if not GDRIVE_API_KEY:
        return "zeus"
    url = f"https://www.googleapis.com/drive/v3/files/{item_id}"
    r = requests.get(url, params={"key": GDRIVE_API_KEY, "fields": "name"}, timeout=30)
    if r.status_code == 200:
        return r.json().get("name") or "zeus"
    return "zeus"

def list_drive_images(folder_id):
    url = "https://www.googleapis.com/drive/v3/files"
    query = f"'{folder_id}' in parents and mimeType contains 'image/'"
    params = {
        "q": query,
        "key": GDRIVE_API_KEY,
        "fields": "files(id,name,mimeType)",
        "orderBy": "name",
        "pageSize": 1000,
    }
    r = requests.get(url, params=params, timeout=60)
    if r.status_code != 200:
        raise Exception(f"فشل جلب قائمة الصور: {r.status_code} - {r.text[:200]}")
    files = r.json().get("files", [])
    files.sort(key=lambda x: natural_sort_key(x["name"]))
    return files

def download_drive_image(file_id):
    url = f"https://drive.google.com/uc?export=download&id={file_id}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    response = requests.get(url, headers=headers, allow_redirects=True, stream=True, timeout=120)
    if response.status_code != 200 or "text/html" in response.headers.get("Content-Type", ""):
        url = f"https://drive.google.com/uc?export=download&confirm=t&id={file_id}"
        response = requests.get(url, headers=headers, stream=True, timeout=120)
        if response.status_code != 200:
            raise Exception(f"فشل تحميل الصورة من Drive: {file_id}")
    return response.content

def process_drive_link(link, *, include_name=False):
    folder_id = extract_gdrive_folder_id(link)
    file_id = extract_gdrive_file_id(link)
    if folder_id:
        files = list_drive_images(folder_id)
        images = []
        for f in files:
            data = download_drive_image(f["id"])
            images.append((data, f["name"]))
        source_name = get_drive_item_name(folder_id)
        return (images, source_name) if include_name else images
    elif file_id:
        data = download_drive_image(file_id)
        if zipfile.is_zipfile(io.BytesIO(data)):
            images = extract_images_from_zip_bytes(data)
        else:
            images = [(data, f"image_{file_id}.jpg")]
        source_name = get_drive_item_name(file_id)
        return (images, Path(source_name).stem) if include_name else images
    else:
        raise Exception("رابط Drive غير صالح.")

def extract_images_from_zip_bytes(zip_bytes):
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        image_names = [name for name in zf.namelist() if name.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))]
        image_names.sort(key=lambda x: natural_sort_key(os.path.basename(x)))
        images = []
        for name in image_names:
            data = zf.read(name)
            images.append((data, os.path.basename(name)))
        return images

# ============================================================
# واجهة اختيار الوضع
# ============================================================
class ModeSelectView(discord.ui.View):
    def __init__(self, user_id):
        super().__init__(timeout=120)
        self.user_id = user_id
        self.thinking_enabled = None
        self.confirmed = False

    @discord.ui.button(label="سرعة عالية", emoji=emoji_manager.partial("bolt"), style=discord.ButtonStyle.secondary)
    async def high_speed(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("هذا الزر ليس لك.", ephemeral=True)
            return
        self.thinking_enabled = False
        self.confirmed = True
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="دقة كاملة", emoji=emoji_manager.partial("circlecheck"), style=discord.ButtonStyle.secondary)
    async def high_accuracy(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("هذا الزر ليس لك.", ephemeral=True)
            return
        self.thinking_enabled = True
        self.confirmed = True
        await interaction.response.defer()
        self.stop()

def mode_selection_sections():
    return [
        f"## {emoji_manager.placeholder('settings')} اختر وضع المعالجة",
        (
            f"### {emoji_manager.placeholder('bolt')} سرعة عالية — موصى به\n"
            "يحافظ على الدقة بشكل ممتاز ويعطي نتيجة أسرع لمعظم الفصول.\n"
            "**استخدمه افتراضيًا** إلا إذا كان الفصل صعبًا جدًا أو النص غير واضح."
        ),
        (
            f"### {emoji_manager.placeholder('circlecheck')} دقة كاملة\n"
            "يفعّل معالجة أعمق للحالات الصعبة جدًا.\n"
            f"{emoji_manager.placeholder('alerttriangle')} **تنبيه:** قد يطول كثيرًا، وقد يصل إلى 30 دقيقة تقريبًا إذا كان عدد الصور 10."
        ),
    ]

async def send_mode_selection(destination, view):
    if V2_SEPARATOR_SUPPORTED:
        await send_panel(destination, sections=mode_selection_sections())
        return await destination.send(content=emojize("{emoji:settings} **اختر الوضع من الأزرار بالأسفل:**"), view=view)
    return await destination.send(embed=themed_embed(description="\n\n".join(mode_selection_sections())), view=view)

def upload_prompt_text():
    return (
        f"## {emoji_manager.placeholder('folderopen')} أرسل ملفات الفصل الآن\n"
        f"{emoji_manager.placeholder('photo')} صور مباشرة حتى `{MAX_IMAGES_PER_REQUEST}`، أو ملف ZIP، أو رابط Google Drive.\n"
        f"{emoji_manager.placeholder('clock')} بعد الاستلام سأعرض مؤشر انتظار احترافي وأحدّثه أثناء كل مرحلة."
    )

# ============================================================
# الواجهة التفاعلية لإدارة الحسابات
# ============================================================
class AccountsView(discord.ui.View):
    def __init__(self, user_id, page=0):
        super().__init__(timeout=180)
        self.user_id = user_id
        self.page = page
        self.update_buttons()

    def get_data(self):
        return load_accounts_data(self.user_id)

    def update_buttons(self):
        data = self.get_data()
        accounts = data["accounts"]
        per_page = 5
        start = self.page * per_page
        end = start + per_page
        self.page_accounts = accounts[start:end]
        self.clear_items()

        for idx, acc in enumerate(self.page_accounts):
            real_idx = start + idx
            status = ""
            if data["active_ocr_index"] == real_idx:
                status += "نشط"
            btn = discord.ui.Button(
                label=f"حساب {real_idx + 1} {status}",
                style=discord.ButtonStyle.secondary,
                custom_id=f"acc_{real_idx}",
                row=0,
            )
            btn.callback = self.make_callback(real_idx)
            self.add_item(btn)

        if self.page > 0:
            prev_btn = discord.ui.Button(label="السابق", emoji="⬅️", style=discord.ButtonStyle.secondary, custom_id="prev", row=1)
            prev_btn.callback = self.prev_page
            self.add_item(prev_btn)
        if end < len(accounts):
            next_btn = discord.ui.Button(label="التالي", emoji="➡️", style=discord.ButtonStyle.secondary, custom_id="next", row=1)
            next_btn.callback = self.next_page
            self.add_item(next_btn)

        create_btn = discord.ui.Button(label="إنشاء حساب", emoji=emoji_manager.partial("circlecheck"), style=discord.ButtonStyle.secondary, custom_id="create", row=1)
        create_btn.callback = self.create_account
        self.add_item(create_btn)

        status_btn = discord.ui.Button(label="الحالة", emoji=emoji_manager.partial("chartpie"), style=discord.ButtonStyle.secondary, custom_id="status", row=1)
        status_btn.callback = self.show_status
        self.add_item(status_btn)

    def make_callback(self, idx):
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
            await interaction.followup.send("{emoji:circlecheck} تم إنشاء حساب جديد بنجاح.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"{{emoji:circlex}} فشل: {str(e)}", ephemeral=True)

    async def show_status(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("هذه القائمة ليست لك.", ephemeral=True)
            return
        data = self.get_data()
        total = len(data["accounts"])
        now_ts = int(time.time())
        active = sum(1 for a in data["accounts"] if a.get("ocr_limit_until", 0) <= now_ts)
        embed = themed_embed("{emoji:chartpie} حالة الحسابات", color_name="blue")
        embed.add_field(name="إجمالي الحسابات", value=str(total))
        embed.add_field(name="نشطة للمعالجة", value=str(active))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def show_account_detail(self, interaction: discord.Interaction, idx):
        data = self.get_data()
        if idx >= len(data["accounts"]):
            await interaction.response.send_message("الحساب غير موجود.", ephemeral=True)
            return
        acc = data["accounts"][idx]
        embed = themed_embed(f"{emoji_manager.placeholder('user')} تفاصيل الحساب {idx + 1}", color_name="green")
        embed.add_field(name=emojize("{emoji:mail} البريد"), value=acc["email"][:40], inline=False)
        embed.add_field(name=emojize("{emoji:lock} التوكن"), value=acc["token"][:30] + "...", inline=False)
        ocr_limit = acc.get("ocr_limit_until", 0)
        embed.add_field(name=emojize("{emoji:bookmark} حالة الخدمة"), value=get_remaining_time(ocr_limit), inline=True)
        embed.add_field(name=emojize("{emoji:chartpie} عدد الاستخدامات"), value=str(acc.get("ocr_count", 0)), inline=True)
        view = AccountDetailView(self.user_id, idx)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

class AccountDetailView(discord.ui.View):
    def __init__(self, user_id, acc_idx):
        super().__init__(timeout=180)
        self.user_id = user_id
        self.acc_idx = acc_idx

    @discord.ui.button(label="تعيين كخدمة نشطة", emoji=emoji_manager.partial("bookmark"), style=discord.ButtonStyle.secondary)
    async def set_active(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("هذه القائمة ليست لك.", ephemeral=True)
            return
        data = load_accounts_data(self.user_id)
        if self.acc_idx < len(data["accounts"]):
            data["active_ocr_index"] = self.acc_idx
            data["accounts"][self.acc_idx]["ocr_limit_until"] = 0
            save_accounts_data(self.user_id, data)
            await interaction.response.send_message("{emoji:circlecheck} تم تعيين الحساب كخدمة نشطة.", ephemeral=True)
        else:
            await interaction.response.send_message("الحساب غير موجود.", ephemeral=True)

    @discord.ui.button(label="تجديد التوكن", emoji=emoji_manager.partial("clock"), style=discord.ButtonStyle.secondary)
    async def refresh_token(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("هذه القائمة ليست لك.", ephemeral=True)
            return
        data = load_accounts_data(self.user_id)
        if self.acc_idx < len(data["accounts"]):
            acc = data["accounts"][self.acc_idx]
            new_token = await asyncio.to_thread(signin_qwen, acc["email"], acc["password"])
            if new_token:
                data["accounts"][self.acc_idx]["token"] = new_token
                save_accounts_data(self.user_id, data)
                await interaction.response.send_message("{emoji:circlecheck} تم تجديد التوكن.", ephemeral=True)
            else:
                await interaction.response.send_message("{emoji:circlex} فشل تجديد التوكن.", ephemeral=True)
        else:
            await interaction.response.send_message("الحساب غير موجود.", ephemeral=True)

    @discord.ui.button(label="فك الحظر", emoji=emoji_manager.partial("tv_unlock"), style=discord.ButtonStyle.secondary)
    async def unban(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("هذه القائمة ليست لك.", ephemeral=True)
            return
        data = load_accounts_data(self.user_id)
        if self.acc_idx < len(data["accounts"]):
            data["accounts"][self.acc_idx]["ocr_limit_until"] = 0
            save_accounts_data(self.user_id, data)
            await interaction.response.send_message("{emoji:circlecheck} تم فك الحظر.", ephemeral=True)
        else:
            await interaction.response.send_message("الحساب غير موجود.", ephemeral=True)

    @discord.ui.button(label="حذف الحساب", emoji=emoji_manager.partial("trash"), style=discord.ButtonStyle.secondary)
    async def delete_account(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("هذه القائمة ليست لك.", ephemeral=True)
            return
        data = load_accounts_data(self.user_id)
        if self.acc_idx < len(data["accounts"]):
            del data["accounts"][self.acc_idx]
            if data["active_ocr_index"] >= len(data["accounts"]):
                data["active_ocr_index"] = -1
            save_accounts_data(self.user_id, data)
            await interaction.response.send_message("{emoji:trash} تم حذف الحساب.", ephemeral=True)
        else:
            await interaction.response.send_message("الحساب غير موجود.", ephemeral=True)


# ============================================================
# واجهات المستخدم: الإعدادات والملف الشخصي ونظام النقاط
# ============================================================
class SettingsView(discord.ui.View):
    def __init__(self, user):
        super().__init__(timeout=240)
        self.user = user
        self.profile = get_user_profile(user)
        self._rebuild()

    def _rebuild(self):
        self.clear_items()
        settings = self.profile["settings"]
        fmt = settings.get("output_format", "txt")
        self.add_item(discord.ui.Button(label=f"صيغة الملف: {fmt.upper()}", emoji=emoji_manager.partial("folder"), style=discord.ButtonStyle.secondary, custom_id="fmt", row=0))
        self.children[-1].callback = self.toggle_format
        spacing = settings.get("bubble_spacing", True)
        self.add_item(discord.ui.Button(label=f"مسافات الفقاعات: {fmt_bool(spacing)}", emoji=emoji_manager.partial("list"), style=discord.ButtonStyle.secondary, custom_id="spacing", row=1))
        self.children[-1].callback = self.toggle_spacing
        sfx = settings.get("include_sfx", True)
        self.add_item(discord.ui.Button(label=f"المؤثرات الصوتية: {fmt_bool(sfx)}", emoji=emoji_manager.partial("music_play"), style=discord.ButtonStyle.secondary, custom_id="sfx", row=1))
        self.children[-1].callback = self.toggle_sfx

    async def _guard(self, interaction):
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("{emoji:lock} هذه اللوحة ليست لك.", ephemeral=True)
            return False
        return True

    def embed(self):
        s = self.profile["settings"]
        return themed_embed(
            title="{emoji:settings} لوحة إعدادات الاستخراج",
            description=(
                f"## إعداداتك الحالية\n{line()}\n"
                f"{emoji_manager.placeholder('folder')} **صيغة الملف:** `{s.get('output_format', 'txt').upper()}`\n"
                f"{emoji_manager.placeholder('list')} **مسافات بين الفقاعات:** `{fmt_bool(s.get('bubble_spacing', True))}`\n"
                f"{emoji_manager.placeholder('music_play')} **تضمين المؤثرات الصوتية:** `{fmt_bool(s.get('include_sfx', True))}`\n\n"
                "غيّر الخيارات من الأزرار؛ سيتم تطبيقها تلقائيًا على `/extract`."
            ),
            color_name="gold",
        )

    async def toggle_format(self, interaction):
        if not await self._guard(interaction):
            return
        current = self.profile["settings"].get("output_format", "txt")
        self.profile = update_user_settings(self.user, output_format="docx" if current == "txt" else "txt")
        self._rebuild()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def toggle_spacing(self, interaction):
        if not await self._guard(interaction):
            return
        self.profile = update_user_settings(self.user, bubble_spacing=not self.profile["settings"].get("bubble_spacing", True))
        self._rebuild()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def toggle_sfx(self, interaction):
        if not await self._guard(interaction):
            return
        self.profile = update_user_settings(self.user, include_sfx=not self.profile["settings"].get("include_sfx", True))
        self._rebuild()
        await interaction.response.edit_message(embed=self.embed(), view=self)


def profile_embed(user, profile):
    settings = profile["settings"]
    embed = themed_embed(
        title="{emoji:user} بروفايل المستخدم",
        description=(
            f"# <@{user.id}>\n{line()}\n"
            f"{emoji_manager.placeholder('star')} **النقاط المتاحة:** `{profile.get('points', 0)}`\n{line()}\n"
            f"{emoji_manager.placeholder('chartpie')} **إجمالي الاستخراجات:** `{profile.get('total_extractions', 0)}`\n{line()}\n"
            f"{emoji_manager.placeholder('shield')} **الحالة:** `{'محظور' if profile.get('is_blocked') else 'نشط'}`\n{line()}\n"
            f"{emoji_manager.placeholder('settings')} **الإخراج:** `{settings.get('output_format', 'txt').upper()}` | "
            f"**المسافات:** `{fmt_bool(settings.get('bubble_spacing', True))}` | "
            f"**المؤثرات:** `{fmt_bool(settings.get('include_sfx', True))}`"
        ),
        color_name="purple",
    )
    embed.set_thumbnail(url=user.display_avatar.url)
    return embed


# ============================================================
# الأوامر
# ============================================================
@bot.event
async def on_ready():
    print(emojize(f"{emoji_manager.placeholder('circlecheck')} ZEUS text bot جاهز (v{BOT_VERSION})"))
    try:
        synced = await bot.tree.sync()
        print(f"تمت مزامنة {len(synced)} أمر.")
    except Exception as e:
        print(f"فشل مزامنة الأوامر: {e}")
    if os.getenv("SYNC_APPLICATION_EMOJIS", "false").lower() == "true":
        try:
            result = await asyncio.to_thread(emoji_manager.sync_application_emojis, bot.user.id, BOT_TOKEN)
            print(f"[EmojiSetup] {result}")
        except Exception as e:
            print(f"[EmojiSetup] failed: {e}")
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="ZEUS Text | /help"))

@bot.tree.command(name="extract", description="استخراج النصوص من صور المانجا")
async def extract(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    user_id = interaction.user.id
    profile = get_user_profile(interaction.user)
    if profile.get("is_blocked"):
        await interaction.followup.send("{emoji:lock} **تم منعك من استخدام البوت.**", ephemeral=True)
        return
    if int(profile.get("points", 0)) <= 0:
        support = f"\n{emoji_manager.placeholder('ticket')} افتح تذكرة تجديد النقاط: {SUPPORT_SERVER_URL}" if SUPPORT_SERVER_URL else ""
        await interaction.followup.send(f"{emoji_manager.placeholder('circlex')} **لا تملك نقاطًا كافية.**\nكل فصل يستهلك نقطة واحدة.{support}", ephemeral=True)
        return
    settings = profile["settings"]

    view = ModeSelectView(user_id)
    await send_mode_selection(interaction.followup, view)
    await view.wait()
    if not view.confirmed:
        await interaction.followup.send("{emoji:circlex} **تم إلغاء العملية.**", ephemeral=True)
        return
    thinking_enabled = view.thinking_enabled

    await interaction.followup.send(upload_prompt_text())

    try:
        msg = await bot.wait_for(
            'message',
            check=lambda m: m.author == interaction.user and m.channel == interaction.channel,
            timeout=300
        )
    except asyncio.TimeoutError:
        await interaction.followup.send("{emoji:clock} **انتهى الوقت، أعد الأمر مرة أخرى.**", ephemeral=True)
        return

    status_msg = await send_status(interaction.channel, "جاري قراءة المصدر", "استلمت المصدر، وسأحدّث هذه اللوحة أثناء التحميل والاستخراج.")
    images = []
    source_name = "zeus_1"
    if msg.attachments:
        if len(msg.attachments) > MAX_IMAGES_PER_REQUEST:
            await edit_status(status_msg, "{emoji:circlex} خطأ", f"الحد الأقصى {MAX_IMAGES_PER_REQUEST} صور.", error=True)
            return
        for att in msg.attachments:
            if att.filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                data = await att.read()
                images.append((data, att.filename))
            elif att.filename.lower().endswith(".zip"):
                data = await att.read()
                images.extend(extract_images_from_zip_bytes(data))
                source_name = Path(att.filename).stem
            else:
                await edit_status(status_msg, "{emoji:circlex} ملف غير مدعوم", f"الملف `{att.filename}` غير مدعوم.", error=True)
                return
    elif msg.content.startswith("http"):
        link = msg.content.strip()
        try:
            await edit_status(status_msg, "جاري تحميل الرابط", "قد يستغرق Google Drive وقتًا أطول حسب حجم الفصل.")
            if "drive.google.com" in link:
                images, source_name = await asyncio.to_thread(process_drive_link, link, include_name=True)
            else:
                data = await asyncio.to_thread(download_image_from_url, link)
                if zipfile.is_zipfile(io.BytesIO(data)):
                    images = extract_images_from_zip_bytes(data)
                    source_name = "zeus_1"
                else:
                    images.append((data, "downloaded_image.jpg"))
                    source_name = "zeus_1"
        except Exception as e:
            await edit_status(status_msg, "{emoji:circlex} فشل معالجة الرابط", f"`{str(e)}`", error=True)
            return
    else:
        await edit_status(status_msg, "{emoji:circlex} مصدر غير صالح", "أرسل صورًا أو ملف ZIP أو رابطًا صالحًا.", error=True)
        return

    if not images:
        await edit_status(status_msg, "{emoji:circlex} لا توجد صور", "لم يتم العثور على صور صالحة.", error=True)
        return

    images.sort(key=lambda x: natural_sort_key(x[1]))
    await edit_status(status_msg, "بدأ الاستخراج الآن", f"تم العثور على `{len(images)}` صورة.", current=0, total=len(images))
    combined_text = ""
    total_images = len(images)
    for idx, (img_bytes, img_name) in enumerate(images, start=1):
        try:
            text = await asyncio.to_thread(
                extract_text_from_single_image,
                user_id,
                img_bytes,
                img_name,
                thinking_enabled,
                settings
            )
            separator = f"\n\n{line()}\n## صورة {idx}\n{line()}\n\n"
            combined_text += separator + text
            await edit_status(status_msg, "المعالجة مستمرة", f"تمت معالجة الصورة `{idx}` من `{total_images}`.", current=idx, total=total_images)
        except Exception as e:
            await edit_status(status_msg, "{emoji:circlex} خطأ أثناء المعالجة", f"الصورة `{idx}`: `{str(e)}`", error=True)
            return

    ok, profile_after = consume_point(user_id)
    if not ok:
        await edit_status(status_msg, "{emoji:circlex} لا توجد نقاط", "لا توجد نقاط كافية لإرسال النتيجة.", error=True)
        return

    output_dir = BASE_DIR / "data"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_format = settings.get("output_format", "txt")
    filename = unique_output_path(output_dir, source_name, output_format)
    if output_format == "docx":
        filename.write_bytes(make_docx_bytes(combined_text).getvalue())
    else:
        filename.write_text(combined_text, encoding="utf-8")

    file = discord.File(str(filename))
    await status_msg.delete()
    await send_panel(
        interaction.channel,
        sections=[
            f"## {emoji_manager.placeholder('circlecheck')} تم استخراج الفصل بنجاح",
            f"{emoji_manager.placeholder('photo')} تمت معالجة `{total_images}` صورة وإرفاق ملف `{filename.name}`.",
            f"{emoji_manager.placeholder('star')} تم خصم نقطة واحدة. المتبقي: `{profile_after.get('points', 0)}`.",
        ],
        content=f"{interaction.user.mention} {emoji_manager.placeholder('circlecheck')} انتهى استخراج الفصل.",
        file=file,
    )
    os.remove(filename)

    increment_account_usage(user_id, "ocr")

@bot.tree.command(name="help", description="تعليمات استخدام البوت")
async def help_command(interaction: discord.Interaction):
    sections = [
        f"# {emoji_manager.placeholder('photo')} ZEUS Text Bot",
        "بوت احترافي لاستخراج نصوص المانجا والمانهوا مع نظام نقاط وإعدادات إخراج شخصية.",
        f"{emoji_manager.placeholder('playerplay')} **/extract**\nيبدأ استخراج فصل كامل. كل عملية ناجحة تخصم **نقطة واحدة**.",
        f"{emoji_manager.placeholder('settings')} **/setting**\nلوحة تفاعلية لتغيير TXT/DOCX، مسافات الفقاعات، والمؤثرات الصوتية.",
        f"{emoji_manager.placeholder('user')} **/profile**\nيعرض بروفايلك أو بروفايل شخص آخر بشكل عام مع صورة المستخدم.",
        f"{emoji_manager.placeholder('ticket')} **تجديد النقاط**\nكل مستخدم يبدأ بـ **5 نقاط مجانية**. عند نفادها افتح تذكرة في السيرفر.",
    ]
    view = components_v2_panel(sections=sections)
    if view:
        await interaction.response.send_message(view=view)
    else:
        await interaction.response.send_message(embed=themed_embed(description="\n\n".join(sections)))

@bot.tree.command(name="setting", description="لوحة إعدادات استخراج النصوص")
async def setting(interaction: discord.Interaction):
    view = SettingsView(interaction.user)
    await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)

@bot.tree.command(name="profile", description="عرض ملفك أو بروفايل شخص آخر في بوت استخراج النصوص")
@app_commands.describe(user="المستخدم المراد عرض بروفايله")
async def profile(interaction: discord.Interaction, user: Optional[discord.User] = None):
    target = user or interaction.user
    profile_data = get_user_profile(target)
    await interaction.response.send_message(embed=profile_embed(target, profile_data))

def parse_user_id(value):
    match = re.search(r"(\d{15,22})", str(value))
    if not match:
        raise ValueError("missing user id")
    return match.group(1)

class AdminActionModal(discord.ui.Modal):
    def __init__(self, action):
        super().__init__(title="لوحة التحكم")
        self.action = action
        self.user_id = discord.ui.TextInput(label="ID المستخدم", placeholder="123456789 أو منشن", required=True)
        self.amount = discord.ui.TextInput(label="النقاط", placeholder="اتركها فارغة للحظر/فك الحظر", required=False)
        self.add_item(self.user_id)
        if action in {"add", "reset"}:
            self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message("ليست مصرح لاستخدام الأمر.", ephemeral=True)
            return
        target = parse_user_id(str(self.user_id.value))
        try:
            amount = int(str(self.amount.value or "0"))
        except ValueError:
            await interaction.response.send_message("{emoji:circlex} قيمة النقاط غير صحيحة.", ephemeral=True)
            return
        if self.action == "add":
            profile_data = admin_adjust_user(target, points_delta=amount)
            msg = f"تمت إضافة `{amount}` نقطة. الرصيد: `{profile_data.get('points', 0)}`"
        elif self.action == "reset":
            profile_data = admin_adjust_user(target, set_points=amount)
            msg = f"تم ضبط النقاط إلى `{profile_data.get('points', 0)}`"
        elif self.action == "block":
            admin_adjust_user(target, blocked=True)
            msg = "تم منع المستخدم."
        else:
            admin_adjust_user(target, blocked=False)
            msg = "تم فك منع المستخدم."
        await interaction.response.send_message(f"{{emoji:circlecheck}} **{msg}**", ephemeral=True)

class AdminPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=240)

    async def interaction_check(self, interaction):
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message("ليست مصرح لاستخدام الأمر.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="المستخدمون", emoji=emoji_manager.partial("chartpie"), style=discord.ButtonStyle.secondary)
    async def users(self, interaction, button):
        users = list_user_profiles(25)
        body = "\n".join(f"`{u.get('user_id')}` • **{u.get('display_name', u.get('username', 'Unknown'))}** • `{u.get('points', 0)}` نقطة • {'محظور' if u.get('is_blocked') else 'نشط'}" for u in users) or "لا يوجد مستخدمون بعد."
        await interaction.response.send_message(embed=themed_embed("{emoji:chartpie} آخر المستخدمين", f"{line()}{body}"), ephemeral=True)

    @discord.ui.button(label="إضافة نقاط", emoji=emoji_manager.partial("star"), style=discord.ButtonStyle.secondary)
    async def add(self, interaction, button):
        await interaction.response.send_modal(AdminActionModal("add"))

    @discord.ui.button(label="ضبط النقاط", emoji=emoji_manager.partial("adjustments"), style=discord.ButtonStyle.secondary)
    async def reset(self, interaction, button):
        await interaction.response.send_modal(AdminActionModal("reset"))

    @discord.ui.button(label="منع", emoji=emoji_manager.partial("lock"), style=discord.ButtonStyle.secondary)
    async def block(self, interaction, button):
        await interaction.response.send_modal(AdminActionModal("block"))

    @discord.ui.button(label="فك المنع", emoji=emoji_manager.partial("shieldcheck"), style=discord.ButtonStyle.secondary)
    async def unblock(self, interaction, button):
        await interaction.response.send_modal(AdminActionModal("unblock"))

    @discord.ui.button(label="حسابات OCR", emoji=emoji_manager.partial("user"), style=discord.ButtonStyle.secondary, row=1)
    async def ocr_accounts(self, interaction, button):
        data = load_accounts_data(OWNER_ID)
        if not data["accounts"]:
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                await asyncio.to_thread(create_and_save_new_account, OWNER_ID)
            except Exception as e:
                await interaction.followup.send(f"{{emoji:circlex}} فشل إنشاء حساب OCR: `{str(e)}`", ephemeral=True)
                return
            await interaction.followup.send(embed=themed_embed("{emoji:user} إدارة حسابات OCR", "اختر حسابًا لإدارته."), view=AccountsView(OWNER_ID), ephemeral=True)
            return
        await interaction.response.send_message(embed=themed_embed("{emoji:user} إدارة حسابات OCR", "اختر حسابًا لإدارته."), view=AccountsView(OWNER_ID), ephemeral=True)

    @discord.ui.button(label="حالة OCR", emoji=emoji_manager.partial("infocircle"), style=discord.ButtonStyle.secondary, row=1)
    async def ocr_status(self, interaction, button):
        data = load_accounts_data(OWNER_ID)
        now_ts = int(time.time())
        active = sum(1 for a in data["accounts"] if a.get("ocr_limit_until", 0) <= now_ts)
        total_uses = sum(a.get("ocr_count", 0) for a in data["accounts"])
        embed = themed_embed("{emoji:chartpie} حالة OCR", f"إجمالي الحسابات: `{len(data['accounts'])}`\nنشطة: `{active}`\nالاستخدامات: `{total_uses}`")
        await interaction.response.send_message(embed=embed, ephemeral=True)

def admin_panel_embed():
    return themed_embed(
        "{emoji:layoutdashboard} لوحة التحكم",
        f"# مركز إدارة بوت النصوص{line()}اختر الإجراء من الأزرار. كل العمليات مخفية ولا يراها إلا المالك."
    )

@bot.tree.command(name="zx", description="...")
async def zx(interaction: discord.Interaction):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("ليست مصرح لاستخدام الأمر.", ephemeral=True)
        return
    await interaction.response.send_message(embed=admin_panel_embed(), view=AdminPanelView())

# ============================================================
# أوامر Prefix بعلامة !
# ============================================================
@bot.command(name="help", aliases=["مساعدة", "اوامر"])
async def prefix_help(ctx):
    sections = [
        f"# {emoji_manager.placeholder('photo')} ZEUS Text Bot",
        "استخدم `/extract` أو `!extract` للبدء، و`!اعدادات` للإعدادات، و`!بروفايل` للبروفايل.",
    ]
    await send_panel(ctx.channel, sections=sections)

@bot.command(name="profile", aliases=["بروفايل"])
async def prefix_profile(ctx, target: Optional[str] = None):
    user = ctx.author
    if target:
        try:
            user_id = int(parse_user_id(target))
            user = ctx.guild.get_member(user_id) if ctx.guild else None
            user = user or await bot.fetch_user(user_id)
        except Exception:
            await ctx.reply("{emoji:circlex} لم أجد المستخدم المطلوب.", mention_author=False)
            return
    profile_data = get_user_profile(user)
    await ctx.send(embed=profile_embed(user, profile_data))

@bot.command(name="setting", aliases=["settings", "اعدادات"])
async def prefix_setting(ctx):
    view = SettingsView(ctx.author)
    await ctx.send(embed=view.embed(), view=view)

@bot.command(name="extract", aliases=["استخراج"])
async def prefix_extract(ctx):
    user_id = ctx.author.id
    profile = get_user_profile(ctx.author)
    if profile.get("is_blocked"):
        await ctx.reply("{emoji:lock} **تم منعك من استخدام البوت.**", mention_author=False)
        return
    if int(profile.get("points", 0)) <= 0:
        await ctx.reply("{emoji:circlex} **لا تملك نقاطًا كافية.**\nكل فصل يستهلك نقطة واحدة.", mention_author=False)
        return
    settings = profile["settings"]
    view = ModeSelectView(user_id)
    await send_mode_selection(ctx, view)
    await view.wait()
    if not view.confirmed:
        await ctx.send("{emoji:circlex} **تم إلغاء العملية.**")
        return
    await ctx.send(upload_prompt_text())
    try:
        msg = await bot.wait_for('message', check=lambda m: m.author == ctx.author and m.channel == ctx.channel, timeout=300)
    except asyncio.TimeoutError:
        await ctx.send("{emoji:clock} **انتهى الوقت، أعد الأمر مرة أخرى.**")
        return
    status_msg = await send_status(ctx.channel, "جاري قراءة المصدر", "استلمت المصدر، وسأحدّث هذه اللوحة أثناء التحميل والاستخراج.")
    images = []
    source_name = "zeus_1"
    try:
        if msg.attachments:
            if len(msg.attachments) > MAX_IMAGES_PER_REQUEST:
                await edit_status(status_msg, "{emoji:circlex} خطأ", f"الحد الأقصى {MAX_IMAGES_PER_REQUEST} صور.", error=True)
                return
            for att in msg.attachments:
                if att.filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                    images.append((await att.read(), att.filename))
                elif att.filename.lower().endswith(".zip"):
                    images.extend(extract_images_from_zip_bytes(await att.read()))
                    source_name = Path(att.filename).stem
                else:
                    await edit_status(status_msg, "{emoji:circlex} ملف غير مدعوم", f"`{att.filename}`", error=True)
                    return
        elif msg.content.startswith("http"):
            await edit_status(status_msg, "جاري تحميل الرابط", "قد يستغرق Google Drive وقتًا أطول.")
            link = msg.content.strip()
            if "drive.google.com" in link:
                images, source_name = await asyncio.to_thread(process_drive_link, link, include_name=True)
            else:
                data = await asyncio.to_thread(download_image_from_url, link)
                images = extract_images_from_zip_bytes(data) if zipfile.is_zipfile(io.BytesIO(data)) else [(data, "downloaded_image.jpg")]
                source_name = "zeus_1"
        else:
            await edit_status(status_msg, "{emoji:circlex} مصدر غير صالح", "أرسل صورًا أو ZIP أو رابطًا صالحًا.", error=True)
            return
    except Exception as e:
        await edit_status(status_msg, "{emoji:circlex} فشل قراءة المصدر", f"`{str(e)}`", error=True)
        return
    if not images:
        await edit_status(status_msg, "{emoji:circlex} لا توجد صور", "لم يتم العثور على صور صالحة.", error=True)
        return
    images.sort(key=lambda x: natural_sort_key(x[1]))
    await edit_status(status_msg, "بدأ الاستخراج", f"تم العثور على `{len(images)}` صورة.", current=0, total=len(images))
    combined_text = ""
    for idx, (img_bytes, img_name) in enumerate(images, start=1):
        try:
            text = await asyncio.to_thread(extract_text_from_single_image, user_id, img_bytes, img_name, view.thinking_enabled, settings)
            combined_text += f"\n\n{line()}## صورة {idx}{line()}\n\n{text}"
            await edit_status(status_msg, "المعالجة مستمرة", f"تمت معالجة الصورة `{idx}` من `{len(images)}`.", current=idx, total=len(images))
        except Exception as e:
            await edit_status(status_msg, "{emoji:circlex} خطأ أثناء المعالجة", f"الصورة `{idx}`: `{str(e)}`", error=True)
            return
    ok, profile_after = consume_point(user_id)
    if not ok:
        await edit_status(status_msg, "{emoji:circlex} لا توجد نقاط", "لا توجد نقاط كافية لإرسال النتيجة.", error=True)
        return
    output_dir = BASE_DIR / "data"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_format = settings.get("output_format", "txt")
    filename = unique_output_path(output_dir, source_name, output_format)
    if output_format == "docx":
        filename.write_bytes(make_docx_bytes(combined_text).getvalue())
    else:
        filename.write_text(combined_text, encoding="utf-8")
    await status_msg.delete()
    await send_panel(ctx.channel, sections=[f"## {emoji_manager.placeholder('circlecheck')} تم استخراج الفصل بنجاح", f"{emoji_manager.placeholder('photo')} تم إرفاق ملف `{filename.name}`.", f"{emoji_manager.placeholder('star')} تم خصم نقطة واحدة. المتبقي: `{profile_after.get('points', 0)}`."], content=f"{ctx.author.mention} {emoji_manager.placeholder('circlecheck')} انتهى استخراج الفصل.", file=discord.File(str(filename)))
    os.remove(filename)
    increment_account_usage(user_id, "ocr")

@bot.command(name="لوحة", aliases=["admin", "ادارة"])
async def prefix_admin_panel(ctx):
    if ctx.author.id != OWNER_ID:
        await ctx.reply("ليست مصرح لاستخدام الأمر.", mention_author=False)
        return
    await ctx.send(embed=admin_panel_embed(), view=AdminPanelView())

@bot.command(name="عطه", aliases=["addpoints"])
async def prefix_add_points(ctx, target: str, amount: int):
    if ctx.author.id != OWNER_ID:
        await ctx.reply("ليست مصرح لاستخدام الأمر.", mention_author=False)
        return
    target_id = parse_user_id(target)
    profile_data = admin_adjust_user(target_id, points_delta=amount)
    await ctx.send(f"{{emoji:circlecheck}} **تمت إضافة `{amount}` نقطة. الرصيد: `{profile_data.get('points', 0)}`**")

@bot.command(name="صفر", aliases=["setpoints"])
async def prefix_set_points(ctx, target: str, amount: int = 0):
    if ctx.author.id != OWNER_ID:
        await ctx.reply("ليست مصرح لاستخدام الأمر.", mention_author=False)
        return
    target_id = parse_user_id(target)
    profile_data = admin_adjust_user(target_id, set_points=amount)
    await ctx.send(f"{{emoji:circlecheck}} **تم ضبط الرصيد إلى `{profile_data.get('points', 0)}`.**")

@bot.command(name="منع", aliases=["blockuser"])
async def prefix_block(ctx, target: str):
    if ctx.author.id != OWNER_ID:
        await ctx.reply("ليست مصرح لاستخدام الأمر.", mention_author=False)
        return
    admin_adjust_user(parse_user_id(target), blocked=True)
    await ctx.send("{emoji:lock} **تم منع المستخدم.**")

@bot.command(name="فك", aliases=["unblockuser"])
async def prefix_unblock(ctx, target: str):
    if ctx.author.id != OWNER_ID:
        await ctx.reply("ليست مصرح لاستخدام الأمر.", mention_author=False)
        return
    admin_adjust_user(parse_user_id(target), blocked=False)
    await ctx.send("{emoji:circlecheck} **تم فك منع المستخدم.**")

# ============================================================
# معالجة الأخطاء
# ============================================================
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CommandOnCooldown):
        await interaction.response.send_message(f"⏳ الأمر قيد التهدئة. حاول بعد {error.retry_after:.1f} ثانية.", ephemeral=True)
    else:
        await interaction.response.send_message(f"❌ حدث خطأ غير متوقع: {str(error)}", ephemeral=True)

# ============================================================
# تشغيل البوت
# ============================================================
if __name__ == "__main__":
    if not BOT_TOKEN:
        raise RuntimeError("DISCORD_TOKEN مفقود من متغيرات البيئة الخاصة بـ text_bot/.env")
    try:
        emoji_manager.load()
        bot.run(BOT_TOKEN)
    finally:
        close_mongo()
