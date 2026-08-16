import json
import re
import base64
from pathlib import Path
from typing import Any

import requests
import discord

BASE_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = BASE_DIR.parent
SYSTEM_EMOJIS_JSON = REPO_DIR / "utils" / "emojis.json"
EMOJIS_JSON = BASE_DIR / "utils" / "emojis.json"
ASSETS_DIR = BASE_DIR / "assets" / "emojis"
PLACEHOLDER_RE = re.compile(r"\{emoji:([A-Za-z0-9_]+)\}")
CUSTOM_RE = re.compile(r"^<a?:[A-Za-z0-9_]{2,32}:\d{17,22}>$")
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
        self.map = {"__color__": "gold"}
        self.load()

    @property
    def theme(self):
        return self.map.get("__color__", "gold")

    def _load_json(self, path):
        if not path.exists():
            return {}
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            return loaded if isinstance(loaded, dict) else {}
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[EmojiManager] failed to load {path}: {exc}")
            return {}

    def load_system_emojis(self):
        return {k: v for k, v in self._load_json(SYSTEM_EMOJIS_JSON).items() if k != "__color__"}

    def load(self):
        # Same runtime idea as the root bot: read the generated emoji table.
        # text_bot keeps its own generated IDs because Discord application emojis
        # belong to one bot application; copied root IDs may not render for this bot.
        local = self._load_json(EMOJIS_JSON)
        if local:
            self.map = local
        else:
            self.map = {"__color__": "gold", **self.load_system_emojis()}
        return self.map

    def save(self):
        ordered = {"__color__": self.theme}
        for key in sorted(k for k in self.map if k != "__color__"):
            ordered[key] = self.map.get(key, "")
        EMOJIS_JSON.parent.mkdir(parents=True, exist_ok=True)
        EMOJIS_JSON.write_text(json.dumps(ordered, ensure_ascii=False, indent=4) + "\n", encoding="utf-8")
        self.map = ordered

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
                return value
            if LEGACY_COLON_RE.match(value):
                return ""
            return value
        return ""

    def get(self, name):
        value = self._format_custom_emoji(self.map.get(name))
        return value or FALLBACKS.get(name, "")

    def partial(self, name):
        value = self.get(name)
        if isinstance(value, str) and CUSTOM_RE.match(value):
            return discord.PartialEmoji.from_str(value)
        return value or None

    def placeholder(self, name):
        return self.get(name) or ""

    def replace(self, value: Any):
        if isinstance(value, str):
            def refresh_custom(match):
                emoji_name = match.group(2)
                return self.map.get(emoji_name) or match.group(0)
            value = re.sub(r"<(a)?:([A-Za-z0-9_]{2,32}):(\d{17,22})>", refresh_custom, value)
            return PLACEHOLDER_RE.sub(lambda m: self.get(m.group(1)) or "", value)
        if isinstance(value, list):
            return [self.replace(v) for v in value]
        if isinstance(value, tuple):
            return tuple(self.replace(v) for v in value)
        if isinstance(value, dict):
            return {k: self.replace(v) for k, v in value.items()}
        return value

    def asset_dir(self):
        return ASSETS_DIR

    def validate_value(self, value):
        return bool(isinstance(value, str) and (CUSTOM_RE.match(value.strip()) or value.strip()))

    def sync_application_emojis(self, bot_user_id: int, token: str):
        if not token:
            raise RuntimeError("DISCORD_TOKEN is required to sync application emojis.")
        if not ASSETS_DIR.exists():
            raise RuntimeError(f"Emoji assets directory not found: {ASSETS_DIR}")
        headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}
        api = f"https://discord.com/api/v10/applications/{bot_user_id}/emojis"
        current = requests.get(api, headers=headers, timeout=30)
        current.raise_for_status()
        items = current.json()
        existing = {item["name"]: item for item in (items if isinstance(items, list) else items.get("items", []))}
        theme = "gold"
        prefix = THEMES[theme]["prefix"]
        fresh = {"__color__": theme}
        uploaded = 0
        for path in sorted(ASSETS_DIR.iterdir()):
            if path.suffix.lower() not in {".png", ".gif"}:
                continue
            base_name = path.stem
            discord_name = f"{prefix}_{base_name}"
            item = existing.get(discord_name)
            if item is None:
                mime = "image/gif" if path.suffix.lower() == ".gif" else "image/png"
                payload = {"name": discord_name, "image": f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"}
                created = requests.post(api, headers=headers, json=payload, timeout=30)
                created.raise_for_status()
                item = created.json()
                existing[discord_name] = item
                uploaded += 1
            animated = path.suffix.lower() == ".gif" or item.get("animated")
            fresh[base_name] = f"<{'a' if animated else ''}:{item['name']}:{item['id']}>"
        self.map = fresh
        self.save()
        return {"ok": True, "uploaded": uploaded, "mapped": len(fresh) - 1, "theme": theme, "source": str(ASSETS_DIR)}

emoji_manager = EmojiManager()

def emojize(value: Any):
    return emoji_manager.replace(value)

def markdown_block(title: str, body: str) -> str:
    return emojize(f"**{title}**\n\n---\n\n{body}")

def themed_embed(title=None, description=None, color_name=None):
    selected = "red" if color_name == "red" else "gold"
    color = THEMES[selected]["color"]
    embed = discord.Embed(color=color, title=emojize(title) if title else None, description=emojize(description) if description else None)
    embed.timestamp = discord.utils.utcnow()
    return embed
