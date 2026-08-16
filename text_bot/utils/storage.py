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
_DEFAULT_PROFILE = {
    "points": 5,
    "total_extractions": 0,
    "is_blocked": False,
    "settings": {
        "output_format": "txt",
        "bubble_spacing": True,
        "include_sfx": True,
    },
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
    _DB.user_profiles.create_index([("user_id", ASCENDING)], unique=True)
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


def _normalize_profile(doc, user=None):
    result = deepcopy(_DEFAULT_PROFILE)
    if isinstance(doc, dict):
        settings = result["settings"]
        settings.update(doc.get("settings") or {})
        result.update({k: doc.get(k, v) for k, v in result.items() if k != "settings"})
        result["settings"] = settings
    if user is not None:
        result["user_id"] = str(user.id)
        result["username"] = getattr(user, "name", str(user.id))
        result["display_name"] = getattr(user, "display_name", result["username"])
    result.setdefault("created_at", int(time.time()))
    result["updated_at"] = int(time.time())
    return result


def load_accounts_data(user_id):
    doc = _get_db().user_accounts.find_one({"user_id": str(user_id)}, {"_id": 0})
    return _normalize(doc or {})


def save_accounts_data(user_id, data):
    payload = _normalize(data)
    payload["user_id"] = str(user_id)
    payload["updated_at"] = int(time.time())
    _get_db().user_accounts.update_one({"user_id": str(user_id)}, {"$set": payload}, upsert=True)


def get_user_profile(user):
    doc = _get_db().user_profiles.find_one({"user_id": str(user.id)}, {"_id": 0})
    profile = _normalize_profile(doc, user)
    if doc is None:
        _get_db().user_profiles.update_one({"user_id": str(user.id)}, {"$set": profile}, upsert=True)
    else:
        _get_db().user_profiles.update_one({"user_id": str(user.id)}, {"$set": {"username": profile["username"], "display_name": profile["display_name"], "updated_at": profile["updated_at"]}}, upsert=True)
    return profile


def update_user_settings(user, **settings):
    profile = get_user_profile(user)
    profile["settings"].update(settings)
    _get_db().user_profiles.update_one({"user_id": str(user.id)}, {"$set": {"settings": profile["settings"], "updated_at": int(time.time())}}, upsert=True)
    return profile


def consume_point(user_id):
    db = _get_db()
    doc = db.user_profiles.find_one({"user_id": str(user_id)}, {"_id": 0}) or {}
    profile = _normalize_profile(doc)
    if profile.get("is_blocked"):
        return False, profile
    if int(profile.get("points", 0)) <= 0:
        return False, profile
    updated = db.user_profiles.find_one_and_update(
        {"user_id": str(user_id), "points": {"$gt": 0}, "is_blocked": {"$ne": True}},
        {"$inc": {"points": -1, "total_extractions": 1}, "$set": {"updated_at": int(time.time())}},
        return_document=True,
        projection={"_id": 0},
    )
    return bool(updated), _normalize_profile(updated or profile)


def admin_adjust_user(user_id, *, points_delta=0, set_points=None, blocked=None):
    update = {"$set": {"updated_at": int(time.time())}, "$setOnInsert": {"created_at": int(time.time())}}
    if set_points is not None:
        update["$set"]["points"] = max(0, int(set_points))
    if blocked is not None:
        update["$set"]["is_blocked"] = bool(blocked)
    if points_delta:
        update["$inc"] = {"points": int(points_delta)}
    _get_db().user_profiles.update_one({"user_id": str(user_id)}, update, upsert=True)
    return _normalize_profile(_get_db().user_profiles.find_one({"user_id": str(user_id)}, {"_id": 0}) or {})


def list_user_profiles(limit=25):
    return list(_get_db().user_profiles.find({}, {"_id": 0}).sort("updated_at", -1).limit(int(limit)))


def close_mongo():
    global _CLIENT, _DB
    if _CLIENT is not None:
        _CLIENT.close()
    _CLIENT = None
    _DB = None
