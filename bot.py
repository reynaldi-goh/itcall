import os
import sys
import json
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
SGT = ZoneInfo("Asia/Singapore")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

# token.json written at runtime from GitHub Secret GOOGLE_TOKEN_JSON
if os.getenv("GOOGLE_TOKEN_JSON") and not os.path.exists("token.json"):
    json.loads(os.getenv("GOOGLE_TOKEN_JSON"))  # validate
    with open("token.json", "w", encoding="utf-8") as f:
        f.write(os.getenv("GOOGLE_TOKEN_JSON"))

SENT_FILE = "sent.json"

# ✅ Map Google Calendar NAME -> Telegram targets
# Put your real chat IDs here
CALENDAR_ROUTES = {
    "ITC EXCO": {
        "chat_ids": ["-1003585417915"],
        "thread_id": 2,
    }
    # "ITC SUBCOMM": {
    #     "chat_ids": ["-1003133268400"],
    #     "thread_id": 3,
    # }
}

# Optional default: if event is in an unmapped calendar, skip it
SKIP_UNMAPPED_CALENDARS = True


def load_sent() -> set[str]:
    try:
        with open(SENT_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_sent(sent: set[str]) -> None:
    with open(SENT_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(list(sent)), f)


def tg_send(chat_id: str, text: str, thread_id: int | None = None) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if thread_id is not None:
        payload["message_thread_id"] = thread_id

    r = requests.post(url, json=payload, timeout=20)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram error for {chat_id}: {data}")


def tg_send_many(chat_ids: list[str], text: str, thread_id: int | None = None) -> None:
    for cid in chat_ids:
        tg_send(cid, text, thread_id)


def get_calendar_service():
    creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    return build("calendar", "v3", credentials=creds)


def nice_time(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")


def format_event_message(ev: dict, *, calendar_name: str, is_test: bool) -> str:
    title = (ev.get("summary") or "(No title)").strip()
    desc = (ev.get("description") or "").strip()
    location = (ev.get("location") or "TBC").strip()

    start = ev.get("start", {})
    end = ev.get("end", {})

    if start.get("dateTime"):
        start_dt = datetime.fromisoformat(start["dateTime"]).astimezone(SGT)
        end_dt = (
            datetime.fromisoformat(end["dateTime"]).astimezone(SGT)
            if end.get("dateTime")
            else start_dt + timedelta(hours=1)
        )
        date_str = start_dt.strftime("%d %B %Y")
        time_str = f"{nice_time(start_dt)} - {nice_time(end_dt)}"
    else:
        date_only = datetime.fromisoformat(start["date"]).date()
        date_str = date_only.strftime("%d %B %Y")
        time_str = "All day"

    header = "🧪 TEST Reminder" if is_test else "📢 Reminder"

    lines = [f"{header}: {title}", ""]
    if desc:
        lines += [desc, ""]
    lines += [
        f"🗓 Date: {date_str}",
        f"⏰ Time: {time_str}",
        f"📍 Venue: {location}",
        "",
        "See you all there 🔥",
    ]
    return "\n".join(lines).strip()


def list_calendars(service) -> list[dict]:
    items: list[dict] = []
    page_token = None
    while True:
        res = service.calendarList().list(pageToken=page_token).execute()
        items.extend(res.get("items", []))
        page_token = res.get("nextPageToken")
        if not page_token:
            break
    return items


def list_events_tomorrow(service, calendar_id: str) -> list[dict]:
    now = datetime.now(SGT)
    start = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)

    res = service.events().list(
        calendarId=calendar_id,
        timeMin=start.astimezone(timezone.utc).isoformat(),
        timeMax=end.astimezone(timezone.utc).isoformat(),
        singleEvents=True,
        orderBy="startTime",
        maxResults=50,
    ).execute()

    return res.get("items", [])


def run_daily(*, is_test: bool):
    if not TELEGRAM_TOKEN:
        raise RuntimeError("Missing TELEGRAM_TOKEN.")
    if not os.path.exists("token.json"):
        raise RuntimeError("token.json not found (GOOGLE_TOKEN_JSON secret missing or not written).")

    service = get_calendar_service()
    sent = load_sent()

    calendars = list_calendars(service)

    # Build: calendar name -> calendar id
    name_to_id: dict[str, str] = {}
    for cal in calendars:
        name = (cal.get("summary") or "").strip()
        cid = cal.get("id")
        if name and cid:
            name_to_id[name] = cid

    # Only process calendars you mapped
    for cal_name, route in CALENDAR_ROUTES.items():
        cal_id = name_to_id.get(cal_name)
        if not cal_id:
            print(f"⚠️ Calendar not found in calendarList: {cal_name}")
            continue

        events = list_events_tomorrow(service, cal_id)
        if not events:
            print(f"✅ No events tomorrow for {cal_name}")
            continue

        for ev in events:
            ev_id = ev.get("id", "")
            start = ev.get("start", {})
            start_key = start.get("dateTime") or start.get("date") or ""
            key = f"{cal_id}:{ev_id}:{start_key}:T-1"

            if not is_test and key in sent:
                continue

            msg = format_event_message(ev, calendar_name=cal_name, is_test=is_test)
            tg_send_many(route["chat_ids"], msg, route.get("thread_id"))

            if not is_test:
                sent.add(key)

    if not is_test:
        save_sent(sent)

# cmd stands for command
def cmd_help(chat_id: str, msg: dict) -> None:
    text = (
        "📋 Available commands:\n\n"
        "/help — Show this message\n"
        "/about — About this bot\n"
        "/events — Events this month\n"
        "/funfact — Random fun fact"
    )
    tg_send(chat_id, text)

def cmd_about(chat_id: str, msg: dict) -> None:
    tg_send(chat_id, "ℹ️ About: placeholder")

def cmd_events(chat_id: str, msg: dict) -> None:
    tg_send(chat_id, "🗓 Events this month: placeholder")

def cmd_funfact(chat_id: str, msg: dict) -> None:
    tg_send(chat_id, "💡 Fun fact: placeholder")

COMMANDS = {
    "/help": cmd_help,
    "/about": cmd_about,
    "/events": cmd_events,
    "/funfact": cmd_funfact,
}

def handle_updates(offset: int | None = None) -> int | None:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    params = {"timeout": 30, "offset": offset}
    r = requests.get(url, params=params, timeout=40)
    updates = r.json().get("result", [])

    for update in updates:
        offset = update["update_id"] + 1
        msg = update.get("message", {})
        text = msg.get("text", "").strip()
        chat_id = str(msg.get("chat", {}).get("id", ""))

        if not text or not chat_id:
            continue

        command = text.split()[0].lower()
        handler = COMMANDS.get(command)
        if handler:
            try:
                handler(chat_id, msg)
            except Exception as e:
                print(f"Error handling {command}: {e}")
                tg_send(chat_id, "⚠️ Something went wrong.")

    return offset

def run_bot() -> None:
    print("🤖 Bot listening for commands...")
    offset = None
    while True:
        try:
            offset = handle_updates(offset)
        except Exception as e:
            print(f"Polling error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    if "--bot" in sys.argv:
        run_bot()
    else:
        run_daily(is_test=("--test" in sys.argv))
