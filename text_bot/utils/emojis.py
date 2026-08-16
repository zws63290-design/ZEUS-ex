import base64
import json
import mimetypes
import os
import re
import time
from pathlib import Path
from typing import Any

import discord
import requests

BASE_DIR = Path(__file__).resolve().parents[1]
EMOJIS_JSON = BASE_DIR / "utils" / "emojis.json"
ASSETS_DIR = BASE_DIR / "assets" / "emojis"
PLACEHOLDER_RE = re.compile(r"\{emoji:([A-Za-z0-9_]+)\}")
CUSTOM_RE = re.compile(r"^<a?:[A-Za-z0-9_]{2,32}:\d{17,22}>$")
CUSTOM_PARTS_RE = re.compile(r"^<a?:([A-Za-z0-9_]{2,32}):(\d{17,22})>$")
LEGACY_COLON_RE = re.compile(r"^:[A-Za-z0-9_]{2,32}:$")
THEMES = {
    "blue": {"prefix": "b", "color": 0x5865F2},
    "red": {"prefix": "r", "color": 0xED4245},
    "green": {"prefix": "g", "color": 0x57F287},
    "purple": {"prefix": "p", "color": 0x9B59B6},
    "gold": {"prefix": "y", "color": 0xF1C40F},
    "pink": {"prefix": "pk", "color": 0xFF73FA},
}
FALLBACKS = {
    "circlecheck": "✅", "circlex": "❌", "alerttriangle": "⚠️", "infocircle": "ℹ️",
    "clock": "⏳", "settings": "⚙️", "photo": "🖼️", "user": "👤", "chartpie": "📊",
    "mail": "✉️", "trash": "🗑️", "lock": "🔒", "shield": "🛡️", "list": "📋",
    "confetti": "🎉", "message": "💬", "star": "⭐", "crown": "👑", "ticket": "🎫",
    "adjustments": "🛠️", "folderopen": "📂", "folder": "📁", "gift": "🎁", "music_play": "▶️",
}

class EmojiManager:
    def __init__(self):
        self.map = {"__color__": os.getenv("EMOJI_THEME", "gold").lower()}
        self.load()

    @property
    def theme(self):
        requested = os.getenv("EMOJI_THEME", self.map.get("__color__", "gold")).lower()
        return requested if requested in THEMES else "gold"

    def load(self):
        if EMOJIS_JSON.exists():
            try:
                self.map = json.loads(EMOJIS_JSON.read_text(encoding="utf-8"))
            except Exception:
                self.map = {"__color__": os.getenv("EMOJI_THEME", "gold").lower()}
        self.map["__color__"] = self.theme
        return self.map

    def save(self):
        ordered = {"__color__": self.theme}
        for key in sorted(k for k in self.map if k != "__color__"):
            ordered[key] = self.map.get(key, "")
        EMOJIS_JSON.parent.mkdir(parents=True, exist_ok=True)
        EMOJIS_JSON.write_text(json.dumps(ordered, ensure_ascii=False, indent=4) + "\n", encoding="utf-8")
        self.map = ordered

    def _emoji_matches_theme(self, value):
        match = CUSTOM_PARTS_RE.match(value)
        if not match:
            return False
        expected_prefix = THEMES[self.theme]["prefix"] + "_"
        return match.group(1).startswith(expected_prefix)

    def _format_custom_emoji(self, value):
        if isinstance(value, dict):
            emoji_id = str(value.get("id", "")).strip()
            emoji_name = str(value.get("name", "")).strip()
            animated = bool(value.get("animated"))
            if emoji_id and emoji_name:
                return f"<{'a' if animated else ''}:{emoji_name}:{emoji_id}>"
        if isinstance(value, str):
            value = value.strip()
            if CUSTOM_RE.match(value):
                return value if self._emoji_matches_theme(value) else ""
            # Discord bots cannot render legacy :name: custom emoji text.
            if LEGACY_COLON_RE.match(value):
                return ""
            return value
        return ""

    def get(self, name):
        value = self._format_custom_emoji(self.map.get(name))
        if value:
            return value
        return FALLBACKS.get(name, "")

    def placeholder(self, name):
        return self.get(name) or ""

    def replace(self, value: Any):
        if isinstance(value, str):
            return PLACEHOLDER_RE.sub(lambda m: self.get(m.group(1)) or "", value)
        if isinstance(value, list):
            return [self.replace(v) for v in value]
        if isinstance(value, tuple):
            return tuple(self.replace(v) for v in value)
        if isinstance(value, dict):
            return {k: self.replace(v) for k, v in value.items()}
        return value

    def asset_dir(self):
        return ASSETS_DIR / self.theme

    def validate_value(self, value):
        return bool(isinstance(value, str) and (CUSTOM_RE.match(value.strip()) or value.strip()))

    def sync_application_emojis(self, bot_user_id: int, token: str):
        """Synchronize missing Application Emojis. Does not use old system-bot IDs."""
        token = (token or "").strip()
        if not token:
            return {"ok": False, "reason": "missing token", "uploaded": 0, "mapped": 0}
        directory = self.asset_dir()
        if not directory.exists():
            return {"ok": False, "reason": f"missing assets directory: {directory}", "uploaded": 0, "mapped": 0}
        headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}
        url = f"https://discord.com/api/v10/applications/{bot_user_id}/emojis"
        r = requests.get(url, headers=headers, timeout=30)
        r.raise_for_status()
        data = r.json()
        items = data if isinstance(data, list) else data.get("items", [])
        existing = {item["name"]: item for item in items if "name" in item}
        prefix = THEMES[self.theme]["prefix"]
        uploaded = mapped = 0
        for path in sorted(directory.iterdir()):
            if path.suffix.lower() not in (".png", ".gif"):
                continue
            key = path.stem
            discord_name = f"{prefix}_{key}"
            item = existing.get(discord_name)
            if not item:
                mime = mimetypes.guess_type(path.name)[0] or ("image/gif" if path.suffix.lower() == ".gif" else "image/png")
                image = "data:%s;base64,%s" % (mime, base64.b64encode(path.read_bytes()).decode("ascii"))
                cr = requests.post(url, headers=headers, json={"name": discord_name, "image": image}, timeout=60)
                cr.raise_for_status()
                item = cr.json(); existing[discord_name] = item; uploaded += 1
                time.sleep(0.25)
            animated = path.suffix.lower() == ".gif"
            self.map[key] = f"<{'a' if animated else ''}:{item['name']}:{item['id']}>" if animated else f"<:{item['name']}:{item['id']}>"
            mapped += 1
        self.save()
        return {"ok": True, "uploaded": uploaded, "mapped": mapped, "theme": self.theme}

emoji_manager = EmojiManager()

def emojize(value: Any):
    return emoji_manager.replace(value)

def markdown_block(title: str, body: str) -> str:
    return emojize(f"**{title}**\n\n---\n\n{body}")

def themed_embed(title=None, description=None, color_name=None):
    # Theme policy: the text bot is gold-first; only explicit errors use red.
    selected = "red" if color_name == "red" else "gold"
    color = THEMES[selected]["color"]
    embed = discord.Embed(color=color, title=emojize(title) if title else None, description=emojize(description) if description else None)
    embed.timestamp = discord.utils.utcnow()
    return embed
