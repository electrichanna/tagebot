# signalli

A personal Signal bot for a daily check-in and diary, with a local AI chat on the side.

Every evening signalli asks you to rate a few parameters (mood, pain, energy, …) from 1 to 10 and asks what happened during the day. You answer in Signal with text or voice messages, in German or English. The answers are stored on an SMB share on your local network. Any other message you send gets a normal AI chat reply.

Everything runs locally: [signal-cli-rest-api](https://github.com/bbernhard/signal-cli-rest-api) for Signal, [Ollama](https://ollama.com) for the language model and a [Wyoming](https://github.com/rhasspy/wyoming) faster-whisper server for speech-to-text.

## How it works

At the configured time the bot sends you two messages:

1. **Check-in:** rate each parameter from 1 to 10.
2. **Diary:** what happened today and how did you feel about it?

Only messages sent as a **reply** to one of these two messages (long-press → Reply in Signal) count as check-in or diary input. Everything else is regular chat.

| You send | What happens |
| --- | --- |
| Reply to the check-in, e.g. `7 3 5 8` (in parameter order) | Ratings are saved, the bot confirms them |
| Reply to the check-in in free text or voice, e.g. "Stimmung 6, Energie 4" | The LLM extracts the ratings; missing ones can be sent in another reply |
| Reply to the diary message (text or voice, as many as you like) | Saved as diary entries, the bot reacts with 📝 |
| A message starting with `/` | A command, see below |
| Anything else | Normal AI chat (the last 10 exchanges are kept as context) |

Replies are filed under the date of the reminder they answer, so replying the next morning still counts for the right day.

The bot only answers the number set as `owner_number` and ignores groups.

### Commands

| Command | Description |
| --- | --- |
| `/params` | Show the current parameters |
| `/add <name> [<name> …]` | Add parameters |
| `/remove <name> [<name> …]` | Remove parameters |
| `/remind` | Send the check-in now |
| `/help` | Show the command list |
| `Ping` | Answers `Pong` (connection test) |

## Stored data

Files are written to the SMB share, one folder per day:

```
<share>/<base_dir>/
└── 2026/
    └── 2026-11-09/
        ├── parameters.json         # ratings + metadata
        ├── diary.json              # diary entries (text and transcripts)
        └── diary_210100_456.aac    # raw audio of a voice entry
```

`parameters.json`

```json
{
  "date": "2026-11-09",
  "created": "2026-11-09T21:03:12+01:00",
  "updated": "2026-11-09T21:05:40+01:00",
  "scale": { "min": 1, "max": 10 },
  "parameters": { "mood": 6, "pain": 3, "energy": 4, "sleep": 8 },
  "inputs": [
    { "timestamp": "2026-11-09T21:03:12+01:00", "type": "text", "text": "7 3 5 8" },
    { "timestamp": "2026-11-09T21:05:40+01:00", "type": "voice", "text": "Stimmung eher 6, Energie 4" }
  ]
}
```

`diary.json`

```json
{
  "date": "2026-11-09",
  "created": "2026-11-09T21:10:02+01:00",
  "updated": "2026-11-09T21:12:30+01:00",
  "entries": [
    {
      "timestamp": "2026-11-09T21:10:00+01:00",
      "saved": "2026-11-09T21:10:02+01:00",
      "type": "text",
      "text": "Spazieren im Park, fühlte mich ruhig."
    },
    {
      "timestamp": "2026-11-09T21:12:28+01:00",
      "saved": "2026-11-09T21:12:30+01:00",
      "type": "voice",
      "text": "Heute war ein guter Tag.",
      "audio_file": "diary_211228_456.aac",
      "transcript_model": "wyoming"
    }
  ]
}
```

Bot state (the parameter list and which messages are reminders) is kept locally in `signalli.db` (SQLite).

## Requirements

- Python 3.12+ and [uv](https://docs.astral.sh/uv/)
- `ffmpeg` on the `PATH` (converts voice messages for transcription)
- [signal-cli-rest-api](https://github.com/bbernhard/signal-cli-rest-api) ≥ 0.101.0 in `json-rpc` mode, with a number registered for the bot (a separate number from your own)
- [Ollama](https://ollama.com) with a model that supports structured output (default `ministral-3:8b`)
- A Wyoming faster-whisper server, started with `--language auto` so that both German and English are recognised
- An SMB share with username/password access

## Setup

```sh
git clone git@github.com:electrichanna/signalli.git
cd signalli
uv sync
cp settings.ini.example settings.ini
```

Fill in `settings.ini`:

| Section | Key | Meaning |
| --- | --- | --- |
| `[signal]` | `phone_number` | Number the bot is registered with |
| | `api_url` | Address of signal-cli-rest-api, e.g. `192.168.0.170:8080` |
| | `connection_mode` | `https_only`, `http_only` or `auto` |
| | `owner_number` | Your own number; the only one the bot answers |
| `[ai]` | `add_system_prompt` | Optional extra text for the chat system prompt |
| `[openai]` | `api_url` | Ollama host **without** port, e.g. `http://192.168.0.170` |
| | `api_port` | Ollama port, usually `11434` (`/v1` is added automatically) |
| | `model` | Model name, e.g. `ministral-3:8b` |
| | `api_key` | Optional, not needed for Ollama |
| `[wyoming]` | `api_url` | Host only, e.g. `192.168.0.170` |
| | `api_port` | Usually `10300` |
| | `language` | Empty to let the server decide, or `de` / `en` to force one language |
| `[diary]` | `reminder_time` | Time of the daily check-in, 24h, e.g. `21:00` |
| | `parameters` | Initial parameters, comma-separated (later changed with `/add` and `/remove`) |
| | `state_db` | Local SQLite file for bot state |
| `[smb]` | `server`, `share` | NAS host/IP and share name |
| | `base_dir` | Folder inside the share |
| | `username`, `password` | SMB credentials |
| | `port` | Optional, default `445` |

`settings.ini` is in `.gitignore`, so the credentials are not committed.

## Run

```sh
uv run src/signalli/main.py
```

Send `Ping` to the bot to check the connection, then `/remind` to try the check-in without waiting for the evening.

## Project layout

```
src/signalli/
├── main.py         # handlers, diary logic, scheduler, startup
├── connectors.py   # Ollama (OpenAI API), Wyoming transcription, SMB storage
├── prompts.py      # system prompts and reminder texts
└── transcribe.py   # standalone transcription helper
```

## License

MIT, see [LICENSE](LICENSE).
