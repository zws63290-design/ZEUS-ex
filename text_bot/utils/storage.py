import os
import time
from copy import deepcopy
from pymongo import MongoClient, ASCENDING

_DEFAULT_DATA = {
    "accounts": [],
    "active_image_index": -1,
    "active_video_index": -1,
    "active_image_edit_index": -1,
    "active_ocr_index": -1,
}
_CLIENT = None
_DB = None


def _get_db():
    global _CLIENT, _DB
    if _DB is not None:
        return _DB
    uri = os.getenv("MONGODB_URI", "").strip()
    if not uri:
        raise RuntimeError("MONGODB_URI مفقود: بوت استخراج النصوص يستخدم MongoDB فقط ولا يحفظ الحسابات محليًا.")
    _CLIENT = MongoClient(uri, serverSelectionTimeoutMS=5000)
    _CLIENT.admin.command("ping")
    db_name = os.getenv("MONGODB_DB", "text_extractor_bot")
    _DB = _CLIENT[db_name]
    _DB.user_accounts.create_index([("user_id", ASCENDING)], unique=True)
    return _DB


def _normalize(data):
    result = deepcopy(_DEFAULT_DATA)
    if isinstance(data, dict):
        result.update({k: data.get(k, v) for k, v in result.items()})
    for acc in result.get("accounts", []):
        acc.setdefault("image_limit_until", 0)
        acc.setdefault("video_limit_until", 0)
        acc.setdefault("image_edit_limit_until", 0)
        acc.setdefault("ocr_limit_until", 0)
        acc.setdefault("image_count", 0)
        acc.setdefault("video_count", 0)
        acc.setdefault("image_edit_count", 0)
        acc.setdefault("ocr_count", 0)
    return result


def load_accounts_data(user_id):
    doc = _get_db().user_accounts.find_one({"user_id": str(user_id)}, {"_id": 0})
    return _normalize(doc or {})


def save_accounts_data(user_id, data):
    payload = _normalize(data)
    payload["user_id"] = str(user_id)
    payload["updated_at"] = int(time.time())
    _get_db().user_accounts.update_one({"user_id": str(user_id)}, {"$set": payload}, upsert=True)


def close_mongo():
    global _CLIENT, _DB
    if _CLIENT is not None:
        _CLIENT.close()
    _CLIENT = None
    _DB = None
