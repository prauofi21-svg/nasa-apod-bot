# 🌌🇮🇷 اتوماسیون کانال تلگرام «Daily Science & Space»

سیستم کاملاً **رایگان و پایدار** روی GitHub Actions که روزانه دو دسته محتوای دوزبانه (فارسی + انگلیسی) در [@daily_sciences](https://t.me/daily_sciences) منتشر می‌کند:

| Workflow | زمان (تهران) | محتوا |
|---|---|---|
| **NASA APOD Daily Post** | هر روز ۲۱:۰۰ (+ پشتیبان ۲۳:۰۰) | عکس/ویدئوی روز ناسا + ترجمه فارسی جذاب + متن اصلی انگلیسی |
| **Reddit Top Daily Post** | هر روز ۱۳:۳۰ (+ پشتیبان ۱۵:۰۰) | ۵ پست برتر r/science+space+astronomy + ترجمه فارسی + عکس |

## Secrets (همگی ثبت شده‌اند)

| Secret | کاربرد |
|---|---|
| `TELEGRAM_BOT_TOKEN` | بات @Agentxza_bot |
| `TELEGRAM_CHAT_ID` | کانال `-1004229980593` |
| `NASA_API_KEY` | api.nasa.gov |
| `GROK_API_KEY` | کلید هوش مصنوعی (Grok/Groq) برای ترجمه فارسی |

## معماری ترجمه (`llm_translator.py`)

کلید API به‌صورت خودکار تشخیص داده می‌شود: اول **Groq** (api.groq.com) سپس **xAI Grok** (api.x.ai) امتحان می‌شود و اولین سرویسِ پذیرنده استفاده می‌گردد. اگر هیچ‌سرویسی در دسترس نباشد، پست‌ها **فقط انگلیسی** ارسال می‌شوند تا سیستم هرگز متوقف نشود.

- ناسا: کپشن دوزبانه (عنوان فارسی + انگلیسی + تاریخ + اعتبار) → پیام «🇮🇷 ترجمه فارسی» → پیام «🌍 English original»
- ردیت: هر پست = عنوان فارسی + خلاصه فارسی + آمار (آپ‌ووت/کامنت/سابردیت) + عنوان انگلیسی، همراه عکس در صورت وجود

## ضدتکرار و پایداری

- `state.json` (ناسا) و `state_reddit.json` (ردیت) پست‌های ارسال‌شده را یاد می‌آورند و در ریپو کامیت می‌شوند
- اجرای پشتیبان فقط در صورت شکست اجرای اصلی پست می‌فرستد
- کامیت روزانه state ها ریپو را همیشه «فعال» نگه می‌دارد (جلوگیری از خاموشی ۶۰روزه GitHub)

## دستورات مفید (اجرا در Actions → Run workflow)

- **NASA APOD Daily Post**: تیک `force` = ارسال مجدد همان روز
- **Reddit Top Daily Post**: تیک `dry_run` = عیب‌یابی (دریافت + ترجمه بدون ارسال)، تیک `force` = نادیده‌گرفتن ضدتکرار

اجرا محلی:

```bash
pip install -r requirements.txt
python reddit_top_bot.py --diag        # عیب‌یابی اتصال LLM + Reddit
python reddit_top_bot.py --dry-run     # پیش‌نمایش بدون ارسال
python nasa_apod_bot.py --dry-run
```

## متغیرهای قابل تنظیم (در فایل workflow)

| متغیر | پیش‌فرض | توضیح |
|---|---|---|
| `SUBREDDITS` | `science+space+astronomy` | سابردیت‌ها (+ جداکننده) |
| `POSTS_COUNT` | `5` | تعداد پست روزانه ردیت |
| `MIN_SCORE` | `50` | حداقل امتیاز پست ردیت |
| `CHANNEL_SIGNATURE` | خالی | امضای پای پست‌ها |
| `SEND_VIDEO_IF_POSSIBLE` | `true` | آپلود فایل ویدئو در روزهای ویدئویی APOD |

اگر endpoint عمومی JSON ردیت از IP گیت‌هاب مسدود شد، می‌توانید با ساختن اپ رایگان در reddit.com/prefs/apps مقادیر `REDDIT_CLIENT_ID` و `REDDIT_CLIENT_SECRET` را به‌عنوان Secret اضافه کنید تا از OAuth رسمی ردیت استفاده شود (در کد پشتیبانی شده است).

## ساختار پروژه

```
nasa_apod_bot.py        بات ناسا: دریافت APOD + عکس/ویدئو + پست دوزبانه
reddit_top_bot.py       بات ردیت: top 5 + ترجمه + عکس + ضدتکرار
llm_translator.py       ترجمه هوش مصنوعی با تشخیص خودکار سرویس (Groq/xAI)
requirements.txt        requests, yt-dlp, Pillow
.github/workflows/      دو workflow زمان‌بندی‌شده + پشتیبان
state.json              وضعیت ضدتکرار ناسا (خودکار)
state_reddit.json       وضعیت ضدتکرار ردیت (خودکار)
```
