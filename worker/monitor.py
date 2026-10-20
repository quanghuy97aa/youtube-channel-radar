
import os
import re
import json
import html
import logging
from datetime import datetime, timezone

import requests
import feedparser
from google.cloud import firestore
from google.oauth2 import service_account

# YouTube Channel ID must begin with UC and have 24 characters.
CHANNEL_PATTERN = re.compile(r"^UC[A-Za-z0-9_-]{22}$")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
DATABASE_ID = os.environ["FIRESTORE_DATABASE_ID"]

service_info = json.loads(
    os.environ["FIREBASE_SERVICE_ACCOUNT_JSON"]
)

credentials = service_account.Credentials.from_service_account_info(
    service_info
)

db = firestore.Client(
    project=service_info["project_id"],
    credentials=credentials,
    database=DATABASE_ID
)


def now():
    return datetime.now(timezone.utc).isoformat()


def send_telegram(message):
    api_url = (
        f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    )

    response = requests.post(
        api_url,
        json={
            "chat_id": CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": False
        },
        timeout=30
    )

    response.raise_for_status()
    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(f"Telegram failed: {result}")


def get_video_id(entry):
    video_id = entry.get("yt_videoid")

    if video_id:
        return video_id

    entry_id = entry.get("id", "")

    if entry_id.startswith("yt:video:"):
        return entry_id.split("yt:video:", 1)[1]

    return None


def check_channel(document):
    data = document.to_dict() or {}

    channel_id = str(data.get("channelId", "")).strip()
    channel_name = data.get("name") or channel_id
    owner_uid = data.get("ownerUid", "")

    if data.get("isActive", True) is False:
        return

    if not CHANNEL_PATTERN.fullmatch(channel_id):
        logging.warning(
            "Invalid YouTube channel ID for %s: %s",
            channel_name,
            channel_id
        )
        return

    if not owner_uid:
        logging.warning(
            "Missing ownerUid for %s",
            channel_name
        )
        return

    logging.info("Checking: %s", channel_name)

    rss_url = (
        "https://www.youtube.com/feeds/videos.xml"
        f"?channel_id={channel_id}"
    )

    response = requests.get(
        rss_url,
        timeout=30,
        headers={
            "User-Agent": "Mozilla/5.0 YouTubeRadar/1.0"
        }
    )

    response.raise_for_status()

    feed = feedparser.parse(response.content)

    if not feed.entries:
        logging.warning(
            "No RSS entries for %s",
            channel_name
        )
        return

    first_run = not data.get("rssInitializedAt")
    new_videos = []

    for entry in feed.entries[:15]:
        video_id = get_video_id(entry)

        if not video_id:
            continue

        # Owner-specific document IDs prevent cross-user conflicts.
        document_id = f"{owner_uid}_{video_id}"
        video_ref = db.collection("videos").document(document_id)

        if video_ref.get().exists:
            continue

        title = entry.get("title", "Video moi")
        video_url = (
            f"https://www.youtube.com/watch?v={video_id}"
        )
        thumbnail = (
            f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
        )

        video_data = {
            "videoId": video_id,
            "ownerUid": owner_uid,
            "channelId": channel_id,
            "channelTitle": channel_name,
            "title": title,
            "videoUrl": video_url,
            "thumbnail": thumbnail,
            "publishedAt": entry.get("published", ""),
            "detectedAt": now()
        }

        video_ref.set(video_data)

        if not first_run:
            new_videos.append(video_data)

    update_data = {
        "lastCheckedAt": now()
    }

    if first_run:
        update_data["rssInitializedAt"] = now()

    document.reference.update(update_data)

    if first_run:
        logging.info(
            "Initial RSS scan complete for %s",
            channel_name
        )
        return

    for video in reversed(new_videos):
        safe_name = html.escape(channel_name)
        safe_title = html.escape(video["title"])
        safe_url = html.escape(
            video["videoUrl"],
            quote=True
        )

        message = (
            "🔴 <b>VIDEO YOUTUBE MỚI</b>\n\n"
            f"📺 Kênh: <b>{safe_name}</b>\n"
            f"🎬 {safe_title}\n\n"
            f'<a href="{safe_url}">▶️ Xem video</a>'
        )

        try:
            send_telegram(message)
            logging.info(
                "Telegram notification sent: %s",
                video["videoId"]
            )
        except Exception:
            logging.exception(
                "Telegram delivery failed for %s",
                video["videoId"]
            )


def main():
    logging.info("Starting YouTube Radar")

    channels = list(
        db.collection("channels").stream()
    )

    logging.info(
        "Found %d channel records",
        len(channels)
    )

    for document in channels:
        try:
            check_channel(document)
        except Exception:
            logging.exception(
                "Error checking document %s",
                document.id
            )

    logging.info("YouTube Radar scan finished")


if __name__ == "__main__":
    main()
