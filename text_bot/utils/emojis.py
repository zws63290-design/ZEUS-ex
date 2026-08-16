import json
import os
import re
from pathlib import Path
from typing import Any

import discord

BASE_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = BASE_DIR.parent
SYSTEM_EMOJIS_JS = REPO_DIR / "utils" / "emojis.js"
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
        self.system_map = self.load_system_emojis()
        self.map = {"__color__": "system", **self.system_map}
        self.load()

    @property
    def theme(self):
        return "system"

    def load_system_emojis(self):
        values = {}
        if not SYSTEM_EMOJIS_JS.exists():
            return values
        text = SYSTEM_EMOJIS_JS.read_text(encoding="utf-8")
        pattern = re.compile(r"(\w+):\s*\{\s*id:\s*'([0-9]{17,22})'.*?toString:\s*\(\)\s*=>\s*'(<a?:[^']+>)'", re.S)
        for key, _emoji_id, rendered in pattern.findall(text):
            values[key] = rendered
        return values

    def load(self):
        # The text bot intentionally mirrors the root system emoji table exactly.
        # Ignore copied text_bot/utils/emojis.json values because they may belong to another bot/theme.
        self.map = {"__color__": "system", **self.system_map}
        return self.map

    def save(self):
        ordered = {"__color__": "system"}
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

    def partial(self, name):
        value = self.get(name)
        if isinstance(value, str) and CUSTOM_RE.match(value):
            return discord.PartialEmoji.from_str(value)
        return value or None

    def placeholder(self, name):
        return self.get(name) or ""

    def replace(self, value: Any):
        if isinstance(value, str):
            # Mirrors the root emojiReplacer hook: refresh explicit custom emojis by name.
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
        """Keep compatibility with the old text-bot sync command; root system IDs are preferred."""
        return {"ok": True, "uploaded": 0, "mapped": len(self.system_map), "theme": "system", "reason": "using root utils/emojis.js"}

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
