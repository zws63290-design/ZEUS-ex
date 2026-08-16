# Text Extractor Bot

نسخة معزولة من `ex.py` داخل مجلد مستقل، مع تخزين MongoDB ونظام إيموجيات مركزي مستوحى من بوت السيستم.

## التشغيل

```bash
cd text_bot
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python bot.py
```

## متغيرات البيئة

- `DISCORD_TOKEN`: توكن بوت استخراج النصوص.
- `MONGODB_URI`: رابط MongoDB، وهو مطلوب للتخزين.
- `MONGODB_DB`: اسم قاعدة البيانات، افتراضيًا `text_extractor_bot`.
- `TEMP_API_URL` و `GDRIVE_API_KEY`: مفاتيح الخدمات الخارجية.
- `EMOJI_THEME`: أحد `gold`, `blue`, `red`, `green`, `purple`, `pink`.
- `SYNC_APPLICATION_EMOJIS=true`: يفعّل مزامنة Application Emojis عند الجاهزية.

## الصور

لا يحتوي هذا المجلد على صور الإيموجيات الثنائية. انسخها يدويًا من بوت السيستم إلى:

```text
text_bot/assets/emojis/<theme>/
```

مثال: `text_bot/assets/emojis/gold/circlecheck.png`.
