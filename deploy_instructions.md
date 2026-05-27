# Deployment Guide: Free 24/7 Hosting for Xscraper Telegram Bot

This guide explains how to deploy the Telegram bot to a **100% free** cloud hosting service (Render or Koyeb) so it runs 24/7.

Because we added a background health-check web server inside `bot.py`, it will easily pass the health checks required by these platforms.

---

## Step 1: Extract Local Twitter Cookies

Twitter has aggressive security systems. Repeated logins from different IP addresses (like cloud servers) will trigger "suspicious activity" blocks or verification codes. 

To bypass this, we use **session cookie replication**:
1. Run the scraper or bot locally once:
   ```bash
   python bot.py
   ```
2. Once the scraper successfully runs, it will create a `cookies.json` file in your workspace directory.
3. Open `cookies.json` and copy its entire text contents.
4. When deploying, you will supply this JSON string in the `TWITTER_COOKIES_JSON` environment variable. The bot will write it to disk automatically at startup, preventing Twitter from seeing a "new login" event.

---

## Step 2: Choose Your Free Deployment Target

### Option A: Render (Free Web Service + UptimeRobot)
Render is 100% free, but free Web Services spin down (go to sleep) if they don't receive web requests for 15 minutes. We prevent this using a free uptime pinger.

1. **Push your project to GitHub**.
2. **Deploy on Render**:
   - Go to [Render.com](https://render.com/) and sign in.
   - Click **New** -> **Web Service**.
   - Connect your GitHub repository.
   - Set the following configuration:
     - **Name**: `xscraper-bot`
     - **Instance Type**: `Free`
     - **Runtime**: `Docker`
   - Scroll down, click **Advanced**, and add your [Environment Variables](#environment-variables).
   - Click **Create Web Service**.
3. **Keep it Awake (UptimeRobot)**:
   - Once Render finishes deploying, copy the public URL of your service (e.g. `https://xscraper-bot.onrender.com`).
   - Sign up for a free account at [UptimeRobot.com](https://uptimerobot.com/).
   - Click **Add New Monitor**:
     - **Monitor Type**: `HTTP(s)`
     - **Friendly Name**: `Xscraper KeepAlive`
     - **URL (or IP)**: Paste your Render service URL.
     - **Monitoring Interval**: Every `5 minutes` or `10 minutes`.
   - UptimeRobot will ping your bot's health check server regularly, keeping it online 24/7.

---

### Option B: Koyeb (Free Nanoprocess - No Sleeping)
Koyeb offers a free tier that does not automatically put containers to sleep.

1. **Push your project to GitHub**.
2. **Deploy on Koyeb**:
   - Sign in to [Koyeb.com](https://koyeb.com/).
   - Click **Create Service**.
   - Select **GitHub** as the deployment source and select your repository.
   - In the configuration settings:
     - **Instance size**: `Nano` (Free Tier, 512MB RAM).
     - **Builder**: `Docker` (Koyeb will read the `Dockerfile` automatically).
     - **Port**: Change the port setting to `8000` (which matches the background health-check server port in `bot.py`).
     - **Path**: Set health check path to `/health` or `/`.
   - Add all your [Environment Variables](#environment-variables) in the variables section.
   - Click **Deploy**.
   - Koyeb will deploy your bot and keep it active 24/7 for free.

---

## Environment Variables

Ensure the following variables are configured on Render or Koyeb:

| Variable Name | Description | Required? |
| :--- | :--- | :--- |
| `TELEGRAM_BOT_TOKEN` | Token provided by BotFather | **Yes** |
| `TWITTER_COOKIES_JSON` | The full string contents of your local `cookies.json` | **Yes** (highly recommended for stability) |
| `TWITTER_USERNAME` | Twitter account username | Optional (fallback for login if cookies expire) |
| `TWITTER_EMAIL` | Twitter account email | Optional (fallback for login if cookies expire) |
| `TWITTER_PASSWORD` | Twitter account password | Optional (fallback for login if cookies expire) |
| `PORT` | Set to `8000` (so Render/Koyeb check the correct port) | **Yes** |

---

## Verification & Management

Once deployed:
1. Send `/start` to your Telegram bot. It should respond immediately with a welcome message.
2. Send `/update` to request the latest news.
   - The first request will trigger a live scrape, take ~60 seconds, and update the cache.
   - Any requests in the next 15 minutes will return the cached results instantly, protecting your Twitter account from rate limits.
