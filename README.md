# 🌌 NASA APOD Telegram Channel Automation

Posts NASA's Astronomy Picture of the Day (image or video) to the Telegram
channel **@daily_sciences** once a day, fully automated, free forever, running
on GitHub Actions.

- Main run: **21:00 Tehran** (17:30 UTC) daily
- Backup run: 23:00 Tehran (19:30 UTC) — only fires if the main run failed
- Duplicate protection via `state.json`
- English posts: title, date, explanation, credit — no links

## Secrets (already configured)

| Secret | Purpose |
|---|---|
| `TELEGRAM_BOT_TOKEN` | @Agentxza_bot token |
| `TELEGRAM_CHAT_ID` | `-1004229980593` (@daily_sciences) |
| `NASA_API_KEY` | api.nasa.gov key |

## Manual actions

- **Run now:** Actions tab → *NASA APOD Daily Post* → Run workflow
  (tick `force` to re-post today's APOD)
- **Change post time:** edit the cron line in
  `.github/workflows/nasa-apod-daily.yml` (Tehran = UTC + 3:30)

## Useful commands (local)

```bash
pip install -r requirements.txt
NASA_API_KEY=... python nasa_apod_bot.py --dry-run      # preview, no send
python nasa_apod_bot.py --force                          # re-post today
python nasa_apod_bot.py --detect-chat                    # find chat ids
```

## Files

```
nasa_apod_bot.py                        main bot script
requirements.txt                        python deps (requests, yt-dlp)
.github/workflows/nasa-apod-daily.yml   daily schedule + backup run
state.json                              auto-created after first success
```
