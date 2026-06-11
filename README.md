# World Cup Discord Bot

A Discord bot that posts World Cup (and other football competition) match reminders, live scores, and results.

## Commands

| Command | Description |
|---|---|
| `!today` | Show all matches today |
| `!live` | Show currently live matches with scores |
| `!upcoming WC 7` | Upcoming matches for a competition in the next N days |
| `!standings WC` | League/group standings |
| `!competitions` | List supported competition codes |
| `!setchannel` | Set current channel for automatic notifications (requires Manage Channels) |

## Competition Codes

| Code | Competition |
|---|---|
| `WC` | FIFA World Cup |
| `CL` | UEFA Champions League |
| `PL` | Premier League |
| `BL1` | Bundesliga |
| `SA` | Serie A |
| `PD` | La Liga |
| `FL1` | Ligue 1 |

## Automatic Notifications

Run `!setchannel` in the channel you want updates in. The bot will automatically post:
- ⏰ 1-hour reminder before each match
- 🔔 15-minute reminder before kick-off
- 🚀 Kick-off announcement
- ⚽ Live score updates every 5 minutes while matches are in play
- 🏁 Full-time result

## Setup

1. Set `DISCORD_BOT_TOKEN` and `FOOTBALL_API_KEY` as environment secrets
2. Start the bot workflow
3. Invite the bot to your server with the `bot` scope + `Send Messages`, `Read Message History` permissions
4. Run `!setchannel` in your preferred channel

## Optional

Set `NOTIFY_CHANNEL_ID` env var to a channel ID to pre-configure the notification channel without needing `!setchannel`.
