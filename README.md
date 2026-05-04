# 📅 Calendar Booklet Generator

Generate a beautiful 12-month printable booklet from your Google Calendar.

Creates a compact, foldable planner PDF with color-coded events and booklet-ready page ordering.

---

## ✨ Features

- Pulls events from **all Google Calendars** (or selected ones)
- 12-month rolling view starting from any month
- Color-coded events (matching Google Calendar colors)
- Compact monthly grid layout
- Automatically imposed for **booklet printing**
- Clean typography optimized for small print

---

## 🚀 Setup

### 1. Enable Google Calendar API

- Go to Google Cloud Console
- Enable **Google Calendar API**
- Create **OAuth credentials (Desktop App)**
- Download as:

credentials.json

---

### 2. Install dependencies

Using uv:
    uv sync

Or pip:
    pip install google-api-python-client \
                google-auth-httplib2 \
                google-auth-oauthlib \
                reportlab \
                pypdf \
                python-dateutil \
                python-dotenv

---

### 3. Configure environment

Create a `.env` file:

GOOGLE_CREDENTIALS_JSON=credentials.json
GOOGLE_TOKEN_JSON=token.json

---

## ▶️ Usage

### Default (current month)

    uv run python booklet.py

### Specific start month

    uv run python booklet.py --start 072026

### Specific calendars

    uv run python booklet.py \
      --calendar-id primary \
      --calendar-id "holidays@group.v.calendar.google.com"

---

## 🖨 Print Settings (IMPORTANT)

- Paper: Letter
- Orientation: Landscape
- Duplex: Flip on short edge
- Scale: 97%
- Pages per sheet: 1

---

## 🔐 Security Notes

- credentials.json and token.json are ignored by git
- .env is ignored
- Never commit secrets

---

## 📜 License

MIT (or your preference)
