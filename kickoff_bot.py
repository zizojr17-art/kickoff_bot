import os
import discord
import asyncio
import requests
from ics import Calendar
from datetime import datetime, timezone

# ======================
# CONFIG
# ======================
TOKEN = os.environ["TOKEN"]
CHANNEL_ID = [CHANNEL_ID]
ICS_URL = "URL"

TEST_MODE = True  # ⚠️ set False after testing

# ======================
# DISCORD SETUP
# ======================
intents = discord.Intents.default()
client = discord.Client(intents=intents)

sent_events = set()

# ======================
# LOAD EVENTS
# ======================
def get_events():
    try:
        data = requests.get(ICS_URL, timeout=10).text
        return list(Calendar(data).events)
    except Exception as e:
        print("ICS error:", e)
        return []

# ======================
# MAIN LOOP
# ======================
async def check_matches():
    await client.wait_until_ready()
    channel = client.get_channel(CHANNEL_ID)

    if channel is None:
        print("❌ Channel not found")
        return

    while not client.is_closed():
        try:
            events = get_events()
            now = datetime.now(timezone.utc)

            print(f"\n🔄 Events found: {len(events)}")

            for event in events:
                if not hasattr(event, "uid") or not event.begin:
                    continue

                if event.uid in sent_events:
                    continue

                start_time = event.begin.datetime

                if start_time.tzinfo is None:
                    start_time = start_time.replace(tzinfo=timezone.utc)

                minutes_left = (start_time - now).total_seconds() / 60

                print(f"⚽ {event.name} | {minutes_left:.1f} min")

                # ======================
                # ALERT LOGIC
                # ======================

                should_send = (
                    (0 < minutes_left <= 15) if not TEST_MODE else True
                )

                if should_send:
                    await channel.send(
                        f"⚽ **MATCH ALERT!**\n"
                        f"**{event.name}**\n"
                        f"⏰ Starts in ~{int(minutes_left)} minutes"
                    )

                    sent_events.add(event.uid)

        except Exception as e:
            print("Loop error:", e)

        await asyncio.sleep(60)

# ======================
# START BOT
# ======================
@client.event
async def on_ready():
    print(f"Logged in as {client.user}")
    client.loop.create_task(check_matches())

client.run(TOKEN)