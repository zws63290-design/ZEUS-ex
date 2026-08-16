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

from utils.storage import close_mongo, load_accounts_data, save_accounts_data
from utils.emojis import emoji_manager, emojize, themed_embed, markdown_block

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
def extract_text_from_single_image(user_id, image_bytes, image_name, thinking_enabled, chat_id=None):
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

    prompt = (
        "استخرج جميع النصوص من هذه الصورة (مانجا/مانهوا) بدقة عالية. "
        "رتبها حسب ترتيب القراءة الصحيح (من اليمين إلى اليسار ومن الأعلى إلى الأسفل). "
        "أعد النص فقط بدون أي تعليقات إضافية أو ترجمة."
    )

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
            return extract_text_from_single_image(user_id, image_bytes, image_name, thinking_enabled, new_chat_id)
        if line_str.startswith("data: "):
            data_content = line_str[6:].strip()
            if data_content == "[DONE]":
                break
            try:
                data_json = json.loads(data_content)
                if is_rate_limited_response(data_json):
                    mark_account_rate_limited(user_id, "ocr")
                    new_chat_id = create_new_chat(user_id, "ocr")
                    return extract_text_from_single_image(user_id, image_bytes, image_name, thinking_enabled, new_chat_id)
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

def process_drive_link(link):
    folder_id = extract_gdrive_folder_id(link)
    file_id = extract_gdrive_file_id(link)
    if folder_id:
        files = list_drive_images(folder_id)
        images = []
        for f in files:
            data = download_drive_image(f["id"])
            images.append((data, f["name"]))
        return images
    elif file_id:
        data = download_drive_image(file_id)
        if zipfile.is_zipfile(io.BytesIO(data)):
            return extract_images_from_zip_bytes(data)
        else:
            return [(data, f"image_{file_id}.jpg")]
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

    @discord.ui.button(label="✅ دقة عالية", style=discord.ButtonStyle.success)
    async def high_accuracy(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("هذا الزر ليس لك.", ephemeral=True)
            return
        self.thinking_enabled = True
        self.confirmed = True
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="⚡ سرعة عالية", style=discord.ButtonStyle.primary)
    async def high_speed(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("هذا الزر ليس لك.", ephemeral=True)
            return
        self.thinking_enabled = False
        self.confirmed = True
        await interaction.response.defer()
        self.stop()

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
                status += "📖 "
            btn = discord.ui.Button(
                label=f"حساب {real_idx + 1} {status}",
                style=discord.ButtonStyle.secondary,
                custom_id=f"acc_{real_idx}",
                row=0,
            )
            btn.callback = self.make_callback(real_idx)
            self.add_item(btn)

        if self.page > 0:
            prev_btn = discord.ui.Button(label="⬅️ السابق", style=discord.ButtonStyle.primary, custom_id="prev", row=1)
            prev_btn.callback = self.prev_page
            self.add_item(prev_btn)
        if end < len(accounts):
            next_btn = discord.ui.Button(label="التالي ➡️", style=discord.ButtonStyle.primary, custom_id="next", row=1)
            next_btn.callback = self.next_page
            self.add_item(next_btn)

        create_btn = discord.ui.Button(label="➕ إنشاء حساب", style=discord.ButtonStyle.success, custom_id="create", row=1)
        create_btn.callback = self.create_account
        self.add_item(create_btn)

        status_btn = discord.ui.Button(label="📊 الحالة", style=discord.ButtonStyle.secondary, custom_id="status", row=1)
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
            await interaction.followup.send("✅ تم إنشاء حساب جديد بنجاح.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ فشل: {str(e)}", ephemeral=True)

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
        embed.add_field(name="📧 البريد", value=acc["email"][:40], inline=False)
        embed.add_field(name="🔐 التوكن", value=acc["token"][:30] + "...", inline=False)
        ocr_limit = acc.get("ocr_limit_until", 0)
        embed.add_field(name="📖 حالة الخدمة", value=get_remaining_time(ocr_limit), inline=True)
        embed.add_field(name="📊 عدد الاستخدامات", value=str(acc.get("ocr_count", 0)), inline=True)
        view = AccountDetailView(self.user_id, idx)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

class AccountDetailView(discord.ui.View):
    def __init__(self, user_id, acc_idx):
        super().__init__(timeout=180)
        self.user_id = user_id
        self.acc_idx = acc_idx

    @discord.ui.button(label="📖 تعيين كخدمة نشطة", style=discord.ButtonStyle.primary)
    async def set_active(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("هذه القائمة ليست لك.", ephemeral=True)
            return
        data = load_accounts_data(self.user_id)
        if self.acc_idx < len(data["accounts"]):
            data["active_ocr_index"] = self.acc_idx
            data["accounts"][self.acc_idx]["ocr_limit_until"] = 0
            save_accounts_data(self.user_id, data)
            await interaction.response.send_message("✅ تم تعيين الحساب كخدمة نشطة.", ephemeral=True)
        else:
            await interaction.response.send_message("الحساب غير موجود.", ephemeral=True)

    @discord.ui.button(label="🔄 تجديد التوكن", style=discord.ButtonStyle.secondary)
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
                await interaction.response.send_message("✅ تم تجديد التوكن.", ephemeral=True)
            else:
                await interaction.response.send_message("❌ فشل تجديد التوكن.", ephemeral=True)
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
            await interaction.response.send_message("✅ تم فك الحظر.", ephemeral=True)
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
            if data["active_ocr_index"] >= len(data["accounts"]):
                data["active_ocr_index"] = -1
            save_accounts_data(self.user_id, data)
            await interaction.response.send_message("🗑️ تم حذف الحساب.", ephemeral=True)
        else:
            await interaction.response.send_message("الحساب غير موجود.", ephemeral=True)

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

    # اختيار الوضع
    view = ModeSelectView(user_id)
    await interaction.followup.send("اختر وضع المعالجة:", view=view)
    await view.wait()
    if not view.confirmed:
        await interaction.followup.send("تم إلغاء العملية.", ephemeral=True)
        return
    thinking_enabled = view.thinking_enabled

    await interaction.followup.send(
        "📤 أرسل الصور (حتى 5) مباشرة، أو ملف ZIP يحتوي على الصور، أو رابط Google Drive.\n"
        "سيتم ترتيب الصور تلقائيًا حسب الأرقام في أسمائها."
    )

    try:
        msg = await bot.wait_for(
            'message',
            check=lambda m: m.author == interaction.user and m.channel == interaction.channel,
            timeout=300
        )
    except asyncio.TimeoutError:
        await interaction.followup.send("انتهى الوقت، أعد الأمر مرة أخرى.", ephemeral=True)
        return

    images = []
    if msg.attachments:
        if len(msg.attachments) > MAX_IMAGES_PER_REQUEST:
            await interaction.followup.send(f"الحد الأقصى {MAX_IMAGES_PER_REQUEST} صور.", ephemeral=True)
            return
        for att in msg.attachments:
            if att.filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                data = await att.read()
                images.append((data, att.filename))
            elif att.filename.lower().endswith(".zip"):
                data = await att.read()
                images.extend(extract_images_from_zip_bytes(data))
            else:
                await interaction.followup.send(f"الملف {att.filename} غير مدعوم.", ephemeral=True)
                return
    elif msg.content.startswith("http"):
        link = msg.content.strip()
        try:
            if "drive.google.com" in link:
                images = await asyncio.to_thread(process_drive_link, link)
            else:
                data = await asyncio.to_thread(download_image_from_url, link)
                if zipfile.is_zipfile(io.BytesIO(data)):
                    images = extract_images_from_zip_bytes(data)
                else:
                    images.append((data, "downloaded_image.jpg"))
        except Exception as e:
            await interaction.followup.send(f"فشل معالجة الرابط: {str(e)}", ephemeral=True)
            return
    else:
        await interaction.followup.send("أرسل صورًا أو ملف ZIP أو رابطًا صالحًا.", ephemeral=True)
        return

    if not images:
        await interaction.followup.send("لم يتم العثور على صور صالحة.", ephemeral=True)
        return

    images.sort(key=lambda x: natural_sort_key(x[1]))

    status_msg = await interaction.channel.send("⏳ جارٍ المعالجة...")
    combined_text = ""
    total_images = len(images)
    for idx, (img_bytes, img_name) in enumerate(images, start=1):
        try:
            text = await asyncio.to_thread(
                extract_text_from_single_image,
                user_id,
                img_bytes,
                img_name,
                thinking_enabled
            )
            separator = f"\n\n========== صورة {idx} ==========\n\n"
            combined_text += separator + text
            await status_msg.edit(content=f"⏳ تمت معالجة {idx} من {total_images} صورة...")
        except Exception as e:
            await status_msg.edit(content=f"❌ حدث خطأ أثناء معالجة الصورة {idx}: {str(e)}")
            return

    output_dir = BASE_DIR / "data"
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = output_dir / f"extracted_{user_id}_{int(time.time())}.txt"
    with open(filename, "w", encoding="utf-8") as f:
        f.write(combined_text)

    file = discord.File(str(filename))
    embed = themed_embed(
        title="{emoji:circlecheck} اكتمل الاستخراج",
        description=f"**تمت معالجة `{total_images}` صورة بنجاح.**\n---\n{emoji_manager.placeholder('photo')} تم إرفاق ملف TXT بالنتيجة.",
        color_name="green",
    )
    await status_msg.delete()
    await interaction.channel.send(embed=embed, file=file)
    os.remove(filename)

    increment_account_usage(user_id, "ocr")

@bot.tree.command(name="help", description="تعليمات استخدام البوت")
async def help_command(interaction: discord.Interaction):
    embed = themed_embed(
        title="{emoji:photo} ZEUS Text Bot",
        description="**بوت مخصص لفرق الترجمة لاستخراج النصوص من صور المانجا والمانهوا بكفاءة عالية.**\n---\nاستخدم الأوامر أدناه لإدارة سير العمل.",
        color_name="purple",
    )
    embed.add_field(
        name=emojize("{emoji:playerplay} /extract"),
        value=emojize(
            "**يبدأ عملية استخراج النصوص من الصور.**\n---\n"
            "1. اختر وضع المعالجة: **دقة عالية** أو **سرعة عالية**.\n"
            "2. أرسل الصور، ملف ZIP، أو رابط Google Drive.\n"
            "3. يستخرج البوت النصوص ويرسل ملف TXT مرتب."
        ),
        inline=False,
    )
    embed.set_footer(text="ZEUS")
    await interaction.response.send_message(embed=embed)  # عامة

# ============================================================
# أوامر مخفية للمالك
# ============================================================
@bot.tree.command(name="cfg", description="...")
async def cfg(interaction: discord.Interaction):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("❌ ليس لديك صلاحية لاستخدام هذا الأمر.", ephemeral=True)
        return

    user_id = interaction.user.id
    data = load_accounts_data(user_id)
    if not data["accounts"]:
        await interaction.response.defer(thinking=True)
        try:
            await asyncio.to_thread(create_and_save_new_account, user_id)
            data = load_accounts_data(user_id)
            view = AccountsView(user_id)
            embed = themed_embed(
                title="{emoji:user} إدارة الحسابات",
                description="**تم إنشاء حساب أولي لك.**\n---\nيمكنك الآن اختيار الحساب وإدارته من الأزرار.",
                color_name="blue",
            )
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ فشل إنشاء حساب: {str(e)}", ephemeral=True)
        return

    view = AccountsView(user_id)
    embed = themed_embed(
        title="{emoji:user} إدارة الحسابات",
        description="**اختر حساباً لعرض التفاصيل أو القيام بإجراء.**\n---\nكل البيانات محفوظة في MongoDB.",
        color_name="blue",
    )
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

@bot.tree.command(name="info", description="...")
async def info(interaction: discord.Interaction):
    if interaction.user.id != OWNER_ID:
        await interaction.response.send_message("❌ ليس لديك صلاحية لاستخدام هذا الأمر.", ephemeral=True)
        return
    user_id = interaction.user.id
    data = load_accounts_data(user_id)
    total = len(data["accounts"])
    now_ts = int(time.time())
    active = sum(1 for a in data["accounts"] if a.get("ocr_limit_until", 0) <= now_ts)
    total_uses = sum(a.get("ocr_count", 0) for a in data["accounts"])
    embed = themed_embed("{emoji:chartpie} حالة الحسابات", color_name="blue")
    embed.add_field(name="إجمالي الحسابات", value=str(total))
    embed.add_field(name="نشطة للمعالجة", value=str(active))
    embed.add_field(name="إجمالي الاستخدامات", value=str(total_uses))
    await interaction.response.send_message(embed=embed, ephemeral=True)

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
