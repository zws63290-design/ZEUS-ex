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
- `SYNC_APPLICATION_EMOJIS`: يفعّل مزامنة Application Emojis عند الجاهزية، وهو مفعّل افتراضيًا. اضبطه على `false` فقط إذا كنت تريد تعطيل المزامنة.

## الصور

يعتمد بوت استخراج النصوص على الملفات الثنائية الموجودة في:

```text
text_bot/assets/emojis/
```

عند التشغيل يرفع هذه الملفات كـ Application Emojis لتطبيق بوت استخراج النصوص ويحدّث جدول الإيموجيات المحلي تلقائيًا، بنفس فكرة بوت السيستم في الجذر.
