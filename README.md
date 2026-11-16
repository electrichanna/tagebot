# tagebot

A personal Signal bot for a daily check-in and diary, with a local AI chat on the side. The name comes from German *Tagebuch* (diary) + bot.

Every evening tagebot asks you to rate a few parameters (e.g. mood, pain, energy) from 1 to 10 and asks what happened during the day. You answer in Signal with text or voice messages, in German or English. The answers are stored on an SMB share on your local network. Any other message you send gets a normal AI chat reply.

Everything runs locally: [signal-cli-rest-api](https://github.com/bbernhard/signal-cli-rest-api) for Signal, [Ollama](https://ollama.com) for the language model and [Speaches](https://github.com/speaches-ai/speaches) or a [Wyoming](https://github.com/rhasspy/wyoming) faster-whisper server for speech-to-text.

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

Replies are filed under the date the check-in was sent, so replying the next morning still counts for the right day. The bot recognises its check-ins by their first line, so replies to older check-ins keep working after restarts.

The bot only answers your own number (`TAGEBOT_SIGNAL__OWNER_NUMBER`) and ignores groups.

### Commands

**Check-in**

| Command | Description |
| --- | --- |
| `/remind` | Send the check-in now |
| `/schedule` | Show the check-in time |
| `/schedule <HHMM>` | Change the check-in time, e.g. `2130` for 21:30 (`21:30` works too) |
| `/params` | Show the rating parameters |
| `/params add <name> …` | Add parameters (letters, digits, `_` and `-`) |
| `/params remove <name> …` | Remove parameters |

**Chat**

| Command | Description |
| --- | --- |
| `/model` | Show the current model |
| `/model list` | List all available models |
| `/model <name>` | Switch to another model |
| `/temp` | Show the chat temperature |
| `/temp <0-2>` | Change the chat temperature |

**Voice messages**

| Command | Description |
| --- | --- |
| `/vocab` | Show the transcription vocabulary |
| `/vocab add <term>, …` | Add comma-separated terms (names, words the transcription should get right) |
| `/vocab remove <term\|number>, …` | Remove terms by name or list number |

**General**

| Command | Description |
| --- | --- |
| `/help` | Show the command list and a link to the source code |
| `Ping` | Answers `Pong` (connection test) |

An unknown command also shows the help.

## Private by default

It is possible (and advised) to run tagebot using only local inference. This is possible by having an Ollama instance and Wyoming-faster-whisper or Speaches running on your local machine or within your local network.

tagebot itself has no telemetry and only talks to the services you set in `.env`. This is where your data goes:

- **Signal:** messages are end-to-end encrypted between your phone and signal-cli-rest-api, which runs on your own machine. Signal's servers only pass on the encrypted messages.
- **Language model and speech-to-text:** your ratings, diary entries, chat messages and voice messages are sent to `TAGEBOT_LLM__URL` and `TAGEBOT_SPEACHES__URL` (or the Wyoming host). With local servers nothing leaves your network. You can also use a cloud API (any OpenAI-compatible one works), but then everything you send to the bot ends up with that provider.
- **Storage:** diary entries, ratings, audio files and settings are only written to your SMB share.

Internet access is only needed for Signal and once for downloading the models.

## Stored data

Everything is written to the SMB share; tagebot keeps no data of its own, so the container can be rebuilt or moved freely.

```
<share>/<base_dir>/
├── settings.json                   # settings changed in chat
└── 2026/
    └── 2026-11-09/
        ├── parameters.json         # ratings + metadata
        ├── diary.json              # diary entries (text and transcripts)
        └── diary_211228_456.aac    # raw audio of a voice entry
```

`settings.json` holds what you changed with `/params`, `/vocab`, `/model`, `/temp` and `/schedule`; these values override the defaults from `.env`. You can also edit the file by hand; the bot picks that up after a restart or the next change. If the share is not reachable at startup, the bot uses the defaults and retries every 5 minutes.

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
      "transcript_model": "speaches:Systran/faster-whisper-large-v3"
    }
  ]
}
```

## Requirements

- Docker with Compose, **or** Python 3.12+ with [uv](https://docs.astral.sh/uv/) and `ffmpeg` on the `PATH`
- [signal-cli-rest-api](https://github.com/bbernhard/signal-cli-rest-api) ≥ 0.101.0 **in `json-rpc` mode** (`MODE=json-rpc`), with a number registered for the bot (a separate number from your own). tagebot receives messages over the WebSocket that only this mode provides; in the default `normal` or `native` mode the bot connects but never sees any messages. `docker-compose.yml` can start one in the right mode for you.
- [Ollama](https://ollama.com) (or another OpenAI-compatible API) with a model that supports structured output (default `ministral-3:8b`)
- A speech-to-text server: [Speaches](https://github.com/speaches-ai/speaches) (recommended, OpenAI-compatible) or a Wyoming faster-whisper server
- An SMB share with username/password access

## Getting started

A step-by-step setup with Docker. [Configuration](#configuration) below explains the settings in detail.

### 1. Prepare the services

- **A phone number for the bot**, separate from your own (e.g. a second SIM or a landline that can receive an SMS or call).
- **Ollama with the model:** `ollama pull ministral-3:8b`. Check it from the machine that will run tagebot: `curl http://<ollama-host>:11434/v1/models` should list it.
- **Speaches with the model**, downloaded once:
  ```sh
  curl -X POST http://<speaches-host>:8000/v1/models/Systran/faster-whisper-large-v3
  ```
  Or a Wyoming faster-whisper server instead (set `TAGEBOT_ASR__BACKEND=wyoming`).
- **An SMB share** on your NAS and a user that can write to it.

### 2. Get the code

```sh
git clone https://github.com/electrichanna/tagebot.git
cd tagebot
```

### 3. Fill in `.env`

```sh
cp .env.example .env
```

Set at least the variables in the [table below](#configuration). Leave `TAGEBOT_SMB__PASSWORD_FILE=secrets/smb_password` as it is to use Docker secrets or change it to `TAGEBOT_SMB__PASSWORD=yourpassword` to just put the password as plaintext in your environment (not recommended);

### 4. Store the secrets (SMB password and API keys)

```sh
mkdir -m 700 secrets
touch secrets/smb_password
nano secrets/smb_password      # type your password in the file
chmod 644 secrets/smb_password
```

The file holds only the password; a line break at the end is ignored. Writing it in an editor keeps it out of the shell history.

The folder is private (`700`) so other users on the host can't read the files. The files themselves have to be readable for the bot's user inside the container (`644`). Compose mounts each file directly, so the folder permission doesn't apply there.

If your LLM or Speaches endpoint needs an API key, put it in `secrets/llm_api_key` or `secrets/speaches_api_key` and enable the matching `_FILE` line in `.env`. For Docker, also add it as a secret in `docker-compose.yml` the same way as `smb_password`, with `TAGEBOT_LLM__API_KEY_FILE: /run/secrets/llm_api_key` (or the Speaches equivalent) under `environment`.

### 5. Set up signal-cli-rest-api in `json-rpc` mode

tagebot only works with signal-cli-rest-api in `json-rpc` mode (see [Requirements](#requirements)).

**Using one you already run:** make sure it is started with `MODE=json-rpc` and the bot number is registered there. Set `TAGEBOT_SIGNAL__API_URL` to its `host:port` and continue with step 6.

**Using the bundled one:** it is already configured with `MODE: json-rpc` in `docker-compose.yml`. Add these to `.env`:

```env
TAGEBOT_SIGNAL__API_URL=signal-api:8080
COMPOSE_PROFILES=signal
```

Start only that service and connect the bot number:

```sh
docker compose up -d signal-api
```

- **Link** (easiest if the number is already a Signal account on a phone): open `http://localhost:8080/v1/qrcodelink?device_name=tagebot` in a browser, then scan the code on the phone under *Settings → Linked devices → +*.
- **Register** the number fresh: follow the register and verify steps in the [signal-cli-rest-api docs](https://github.com/bbernhard/signal-cli-rest-api). Signal usually asks for a captcha.

If linking or registering doesn't work in `json-rpc` mode, set `MODE: normal` in `docker-compose.yml`, connect the number, and **switch back to `MODE: json-rpc`** afterwards (`docker compose up -d signal-api`). Otherwise tagebot receives no messages.

### 6. Start tagebot

```sh
docker compose up -d --build
docker compose logs -f tagebot
```

If a setting is missing or invalid, the log names the exact variable. Fix it in `.env` and run `docker compose up -d` again.

### 7. Test it

From your own phone, message the bot:

1. `Ping` → answers `Pong`.
2. `/help` → shows the command list.
3. `/remind` → both check-in messages arrive.
4. Reply to the rating check-in (long-press → Reply) with e.g. `7 3 5 8` → the bot confirms the ratings.
5. Reply to the diary check-in with a voice message → the bot sends the transcript and reacts with 📝.
6. On the NAS, `<base_dir>/<year>/<date>/` now has `parameters.json`, `diary.json` and the audio file.

If `Ping` gets no answer, check that signal-cli-rest-api runs in `json-rpc` mode and look at `docker compose logs tagebot`.

### 8. Adjust in chat (optional)

`/schedule 2130`, `/params add focus`, `/vocab add Hanna`, `/model list`. These are saved in `settings.json` on the share, so they survive rebuilds.

### Updating

```sh
git pull
docker compose up -d --build
```

## Configuration

All settings are environment variables named `TAGEBOT_<SECTION>__<KEY>` (two underscores between section and key), read from `.env`. [.env.example](.env.example) lists every setting with its default. The required ones:

| Variable | Meaning |
| --- | --- |
| `TZ` | Time zone for the check-in, e.g. `Europe/Berlin` |
| `TAGEBOT_SIGNAL__PHONE_NUMBER` | Number the bot is registered with |
| `TAGEBOT_SIGNAL__OWNER_NUMBER` | Your own number; the only one the bot answers |
| `TAGEBOT_SIGNAL__API_URL` | Address of signal-cli-rest-api, e.g. `signal-api:8080` or `192.168.0.170:8080` |
| `TAGEBOT_LLM__URL` | Ollama URL including the port, e.g. `http://192.168.0.170:11434` (`/v1` is added automatically) |
| `TAGEBOT_ASR__BACKEND` | `speaches` (vocabulary sent per request) or `wyoming` (vocabulary needs a server restart) |
| `TAGEBOT_SPEACHES__URL` | Speaches URL including the port, e.g. `http://192.168.0.170:8000` (for `speaches`) |
| `TAGEBOT_WYOMING__HOST` | Wyoming host, e.g. `192.168.0.170` (for `wyoming`) |
| `TAGEBOT_SMB__SERVER`, `TAGEBOT_SMB__SHARE` | NAS host/IP and share name |
| `TAGEBOT_SMB__BASE_DIR` | Folder inside the share, e.g. `tagebot` (optional, default: top level) |
| `TAGEBOT_SMB__USERNAME` | SMB user |
| `TAGEBOT_SMB__PASSWORD_FILE` | File with the SMB password, `secrets/smb_password` (see [Secrets](#secrets)) |

Missing or invalid values stop the bot at startup with a list of the affected variables.

## Run without Docker

Follow steps 1–5 above, then start the bot from the repository root (`.env` is read from the current directory):

```sh
uv sync
uv run src/tagebot/main.py
```

## Project layout

```
├── Dockerfile
├── docker-compose.yml   # tagebot + optional signal-cli-rest-api
├── .env.example         # all settings with their defaults
└── src/tagebot/
    ├── main.py          # handlers, diary logic, scheduler, startup
    ├── settings.py      # configuration from environment variables
    ├── connectors.py    # Ollama (OpenAI API), Speaches/Wyoming transcription, SMB storage
    └── prompts.py       # system prompts and check-in texts
```

## Running on NixOS

This is how I run tagebot on my NixOS machine. Everything runs on one machine: signal-cli-rest-api, tagebot and Speaches as Docker containers, Ollama as a NixOS service. The GPU part is for an NVIDIA card; leave it out to run on the CPU.

Add this to your `configuration.nix` (it needs `config` and `pkgs` in the module arguments):

```nix
# User that owns the tagebot files
users.users.diary = {
  isSystemUser = true;
  group = "diary";
  home = "/var/lib/diary";
  createHome = true;
};
users.groups.diary = { };
systemd.tmpfiles.rules = [ "d /var/lib/diary/secrets 0700 diary diary -" ];

environment.systemPackages = [ pkgs.git ];

# NVIDIA GPU for Ollama and Speaches (optional)
nixpkgs.config.allowUnfree = true;
nixpkgs.config.cudaSupport = true;
services.xserver.videoDrivers = [ "nvidia" ];
hardware.nvidia.open = true;
hardware.nvidia.package = config.boot.kernelPackages.nvidiaPackages.stable;
hardware.nvidia-container-toolkit.enable = true;

virtualisation.docker.enable = true;
virtualisation.docker.daemon.settings.features.cdi = true;  # GPU access for containers
virtualisation.oci-containers.backend = "docker";

virtualisation.oci-containers.containers = {
  signal-api = {
    image = "bbernhard/signal-cli-rest-api:0.101";
    environment.MODE = "json-rpc";
    ports = [ "127.0.0.1:8080:8080" ];  # localhost only
    volumes = [ "/home/hanni/.local/share/signal-api:/home/.local/share/signal-cli" ];
  };

  speaches = {
    image = "ghcr.io/speaches-ai/speaches:latest-cuda";  # or :latest-cpu without a GPU
    ports = [ "127.0.0.1:8000:8000" ];
    volumes = [ "hf-hub-cache:/home/ubuntu/.cache/huggingface/hub" ];
    extraOptions = [ "--device=nvidia.com/gpu=all" ];  # remove without a GPU
  };

  tagebot = {
    image = "tagebot:latest";  # built locally, see below
    dependsOn = [ "signal-api" "speaches" ];
    extraOptions = [ "--network=host" ];  # reaches the other services on 127.0.0.1
    environmentFiles = [ "/var/lib/diary/tagebot.env" ];
    environment = {
      TAGEBOT_SIGNAL__API_URL = "127.0.0.1:8080";
      TAGEBOT_SMB__PASSWORD_FILE = "/run/secrets/smb_password";
    };
    volumes = [ "/var/lib/diary/secrets/smb_password:/run/secrets/smb_password:ro" ];
  };
};

services.ollama = {
  enable = true;
  package = pkgs.ollama-cuda;  # or pkgs.ollama without a GPU
  loadModels = [ "ministral-3:8b" ];
};
```

After `sudo nixos-rebuild switch`:

1. **Get the code and build the image:**
   ```sh
   sudo -u diary git clone https://github.com/electrichanna/tagebot.git /var/lib/diary/src
   sudo docker build -t tagebot:latest /var/lib/diary/src
   ```
2. **Configure:** copy `.env.example` to `/var/lib/diary/tagebot.env` and fill it in. Because tagebot uses the host network, point it at localhost:
   ```sh
   TAGEBOT_LLM__URL=http://127.0.0.1:11434
   TAGEBOT_SPEACHES__URL=http://127.0.0.1:8000
   ```
3. **Store the SMB password** in `/var/lib/diary/secrets/smb_password` as in [step 4](#4-store-the-secrets-smb-password-and-api-keys) (`chmod 644`; the folder is already private).
4. **Download the Speaches model** once:
   ```sh
   curl -X POST http://127.0.0.1:8000/v1/models/Systran/faster-whisper-large-v3
   ```
5. **Link the Signal number:** port 8080 is only open on the machine itself, so tunnel to it with `ssh -L 8080:localhost:8080 <nixos-host>` and open `http://localhost:8080/v1/qrcodelink?device_name=tagebot` on your computer (see [step 5](#5-set-up-signal-cli-rest-api-in-json-rpc-mode)).
6. **Start tagebot:** `sudo systemctl restart docker-tagebot`, logs with `journalctl -u docker-tagebot -f`.

To update:

```sh
sudo -u diary git -C /var/lib/diary/src pull
sudo docker build -t tagebot:latest /var/lib/diary/src
sudo systemctl restart docker-tagebot
```

### Without Docker on NixOS

The Docker image already contains ffmpeg. To [run tagebot with uv](#run-without-docker) directly on NixOS instead, ffmpeg has to be on the `PATH`. Binaries that uv downloads (its Python and prebuilt wheels) expect libraries in the usual Linux locations, which NixOS doesn't have; `nix-ld` provides them, including the ffmpeg libraries for software that links against them:

```nix
environment.systemPackages = with pkgs; [ git uv ffmpeg ];

programs.nix-ld.enable = true;
programs.nix-ld.libraries = with pkgs; [ ffmpeg_8-headless ];
```

## License

GNU Affero General Public License v3.0 or later (AGPL-3.0-or-later), see [LICENSE](LICENSE). If you run a modified version that other people use over a network, you have to offer them its source code.
