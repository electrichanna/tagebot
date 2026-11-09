import asyncio
import base64
import configparser
import logging
import mimetypes
import ntpath
import re
from collections import deque
from datetime import datetime
from pathlib import Path

from signalbot import (
    Config,
    DataMessageContext,
    DataMessageHandler,
    SendMessage,
    SignalBot,
    SQLiteConfig,
    regex_triggered,
    text_triggered,
)

from connectors import (
    OLLAMA_MODEL,
    ollama_parse_response,
    ollama_response_message,
    smb_read_json,
    smb_register,
    smb_write_bytes,
    smb_write_json,
    transcribe_bytes,
)
from prompts import CHAT_SYSTEM_PROMPT, DIARY_REMINDER, RATING_REMINDER


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Diary:
    """Shared logic: parameters, reminders, transcription and storage on the SMB share."""

    def __init__(self, bot: SignalBot, config: configparser.ConfigParser) -> None:
        self.bot = bot
        self.owner = config["signal"]["owner_number"]

        openai = config["openai"]
        self.llm_url = f"{openai['api_url'].rstrip('/')}:{openai['api_port']}/v1"
        self.llm_model = openai.get("model", OLLAMA_MODEL)
        self.llm_api_key = openai.get("api_key", "not-needed")
        extra = config.get("ai", "add_system_prompt", fallback="").strip()
        self.chat_system_prompt = CHAT_SYSTEM_PROMPT + ("\n" + extra if extra else "")

        wyoming = config["wyoming"]
        self.asr_host = wyoming["api_url"]
        self.asr_port = int(wyoming["api_port"])
        self.asr_language = wyoming.get("language") or None

        diary = config["diary"]
        self.reminder_time = diary.get("reminder_time", "21:00")
        self.default_parameters = [
            p.strip().lower() for p in diary.get("parameters", "mood, pain").split(",") if p.strip()
        ]

        smb = config["smb"]
        self.smb_session = (smb["server"], smb["username"], smb["password"], int(smb.get("port", "445")))
        self.base_path = ntpath.join(
            rf"\\{smb['server']}\{smb['share']}", *smb.get("base_dir", "").replace("/", "\\").split("\\")
        )

        self._write_lock = asyncio.Lock()

    # ---- parameters -------------------------------------------------------------

    @property
    def parameters(self) -> list[str]:
        if self.bot.storage.exists("parameters"):
            return self.bot.storage.read("parameters")
        return list(self.default_parameters)

    @parameters.setter
    def parameters(self, value: list[str]) -> None:
        self.bot.storage.save("parameters", value)

    # ---- reminders --------------------------------------------------------------

    async def send_reminder(self) -> None:
        date = datetime.now().date().isoformat()
        parameters = self.parameters
        if parameters:
            text = RATING_REMINDER.format(
                parameters="\n".join(f"• {p}" for p in parameters),
                example=" ".join(str(5 + i % 3) for i in range(len(parameters))),
                example_named=", ".join(f"{p} {7 - i}" for i, p in enumerate(parameters[:2])),
            )
            sent = await self.bot.messages.send(SendMessage(text=text), self.owner)
            self.bot.storage.save(f"reminder:{sent.timestamp}", {"kind": "rating", "date": date})

        sent = await self.bot.messages.send(SendMessage(text=DIARY_REMINDER), self.owner)
        self.bot.storage.save(f"reminder:{sent.timestamp}", {"kind": "diary", "date": date})

    def reminder_for(self, context: DataMessageContext) -> dict | None:
        """The reminder the message replies to, or None if it is no reply to a reminder."""
        quote = context.message.quote
        if quote is None or quote.id is None:
            return None
        key = f"reminder:{quote.id}"
        return self.bot.storage.read(key) if self.bot.storage.exists(key) else None

    # ---- input ------------------------------------------------------------------

    async def read_input(self, context: DataMessageContext) -> tuple[str, bytes | None, str | None]:
        """Returns (text, audio bytes, audio extension). Voice messages are transcribed."""
        for attachment in context.message.attachments or []:
            is_audio = attachment.is_voice_note or (attachment.content_type or "").startswith("audio/")
            if is_audio and attachment.base64_content:
                audio = base64.b64decode(attachment.base64_content)
                text = await transcribe_bytes(audio, self.asr_host, self.asr_port, self.asr_language)
                extension = mimetypes.guess_extension(attachment.content_type or "") or ".bin"
                return text.strip(), audio, extension
        return (context.message.text or "").strip(), None, None

    # ---- storage ----------------------------------------------------------------

    async def smb(self, func, *args):
        """Run an SMB call in a thread, (re)connecting first so a NAS restart is no problem."""

        def run():
            smb_register(*self.smb_session)
            return func(*args)

        return await asyncio.to_thread(run)

    def day_path(self, date: str, filename: str) -> str:
        return ntpath.join(self.base_path, date[:4], date, filename)

    async def save_ratings(self, date: str, ratings: dict, text: str, source: str) -> dict:
        path = self.day_path(date, "parameters.json")
        async with self._write_lock:
            data = await self.smb(smb_read_json, path) or {
                "date": date,
                "created": now_iso(),
                "scale": {"min": 1, "max": 10},
                "parameters": {},
                "inputs": [],
            }
            data["parameters"].update({k: v for k, v in ratings.items() if v is not None})
            data["updated"] = now_iso()
            data["inputs"].append({"timestamp": now_iso(), "type": source, "text": text})
            await self.smb(smb_write_json, path, data)
        return data["parameters"]

    async def save_diary_entry(
        self, date: str, text: str, audio: bytes | None, extension: str | None, signal_timestamp: int
    ) -> None:
        received = datetime.fromtimestamp(signal_timestamp / 1000).astimezone()
        entry = {
            "timestamp": received.isoformat(timespec="seconds"),
            "saved": now_iso(),
            "type": "voice" if audio is not None else "text",
            "text": text,
        }
        async with self._write_lock:
            if audio is not None:
                audio_name = f"diary_{received:%H%M%S}_{signal_timestamp % 1000:03d}{extension}"
                await self.smb(smb_write_bytes, self.day_path(date, audio_name), audio)
                entry["audio_file"] = audio_name
                entry["transcript_model"] = "wyoming"

            path = self.day_path(date, "diary.json")
            data = await self.smb(smb_read_json, path) or {
                "date": date,
                "created": now_iso(),
                "entries": [],
            }
            data["entries"].append(entry)
            data["updated"] = now_iso()
            await self.smb(smb_write_json, path, data)

    # ---- llm --------------------------------------------------------------------

    async def parse_ratings(self, text: str) -> dict:
        parameters = self.parameters
        # Fast path: only numbers, one per parameter, in order
        if re.fullmatch(r"[\d\s,;/]+", text):
            numbers = [int(n) for n in re.findall(r"\d+", text)]
            if len(numbers) == len(parameters) and all(1 <= n <= 10 for n in numbers):
                return dict(zip(parameters, numbers))
        return await asyncio.to_thread(
            ollama_parse_response, text, parameters, url=self.llm_url, api_key=self.llm_api_key, model=self.llm_model
        )

    async def chat(self, text: str, history: list[dict]) -> str:
        return await asyncio.to_thread(
            ollama_response_message,
            text,
            url=self.llm_url,
            api_key=self.llm_api_key,
            model=self.llm_model,
            history=history,
            system_prompt=self.chat_system_prompt,
        )


class PingCommand(DataMessageHandler):
    @text_triggered("Ping")
    async def handle_data_message(self, context: DataMessageContext) -> None:
        await context.send(SendMessage(text="Pong"))


class SlashCommand(DataMessageHandler):
    """/params, /add <name>..., /remove <name>..., /remind, /help"""

    HELP = (
        "/params – show the parameters\n"
        "/add <name> [<name> ...] – add parameters\n"
        "/remove <name> [<name> ...] – remove parameters\n"
        "/remind – send the evening check-in now\n"
        "/help – this help"
    )

    def __init__(self, diary: Diary) -> None:
        self.diary = diary

    @regex_triggered(r"^/")
    async def handle_data_message(self, context: DataMessageContext) -> None:
        command, *args = context.message.text.strip().split()
        command = command.lower()
        args = [a.strip(",").lower() for a in args if a.strip(",")]
        parameters = self.diary.parameters

        if command in ("/params", "/list"):
            text = "Parameters: " + (", ".join(parameters) or "none")
        elif command == "/add" and args:
            invalid = [a for a in args if not re.fullmatch(r"[\w-]+", a)]
            if invalid:
                text = f"Invalid name(s): {', '.join(invalid)} (letters, digits, _ and - only)"
            else:
                parameters += [a for a in args if a not in parameters]
                self.diary.parameters = parameters
                text = "Parameters: " + ", ".join(parameters)
        elif command == "/remove" and args:
            missing = [a for a in args if a not in parameters]
            self.diary.parameters = parameters = [p for p in parameters if p not in args]
            text = "Parameters: " + (", ".join(parameters) or "none")
            if missing:
                text += f"\nNot found: {', '.join(missing)}"
        elif command == "/remind":
            await self.diary.send_reminder()
            return
        else:
            text = self.HELP

        await context.send(SendMessage(text=text))


class MessageHandler(DataMessageHandler):
    """Replies to a reminder are diary input, everything else is normal AI chat."""

    def __init__(self, diary: Diary) -> None:
        self.diary = diary
        self.history: deque[dict] = deque(maxlen=20)

    async def handle_data_message(self, context: DataMessageContext) -> None:
        reminder = self.diary.reminder_for(context)
        try:
            text, audio, extension = await self.diary.read_input(context)
        except Exception:
            logging.exception("Transcription failed")
            await context.reply(SendMessage(text="⚠️ Could not transcribe the voice message."))
            return
        if not text and audio is None:
            return
        if audio is not None:
            await context.reply(SendMessage(text=f"🎙️ {text or '(nothing recognised)'}"))

        if reminder is None:
            await self.handle_chat(context, text)
        elif reminder["kind"] == "rating":
            await self.handle_rating(context, reminder["date"], text, "voice" if audio else "text")
        else:
            await self.handle_diary(context, reminder["date"], text, audio, extension)

    async def handle_chat(self, context: DataMessageContext, text: str) -> None:
        await context.start_typing()
        try:
            answer = await self.diary.chat(text, list(self.history))
        except Exception:
            logging.exception("Chat failed")
            answer = "⚠️ The AI is not reachable right now."
        else:
            self.history.extend([{"role": "user", "content": text}, {"role": "assistant", "content": answer}])
        finally:
            await context.stop_typing()
        await context.send(SendMessage(text=answer))

    async def handle_rating(self, context: DataMessageContext, date: str, text: str, source: str) -> None:
        try:
            ratings = await self.diary.parse_ratings(text)
            saved = await self.diary.save_ratings(date, ratings, text, source)
        except Exception:
            logging.exception("Saving ratings failed")
            await context.reply(SendMessage(text="⚠️ Could not save the ratings."))
            return

        lines = [f"{p}: {saved[p]}" if p in saved else f"{p}: –" for p in self.diary.parameters]
        missing = [p for p in self.diary.parameters if p not in saved]
        reply = f"Saved for {date}:\n" + "\n".join(lines)
        if missing:
            reply += "\n\nMissing ones can be sent as another reply to the check-in."
        await context.reply(SendMessage(text=reply))

    async def handle_diary(
        self, context: DataMessageContext, date: str, text: str, audio: bytes | None, extension: str | None
    ) -> None:
        try:
            await self.diary.save_diary_entry(date, text, audio, extension, context.message.timestamp)
        except Exception:
            logging.exception("Saving diary entry failed")
            await context.reply(SendMessage(text="⚠️ Could not save the diary entry."))
            return
        await context.react("📝")


if __name__ == "__main__":
    config = configparser.ConfigParser()

    settings_path = Path(__file__).parent.parent.parent / "settings.ini"

    if not config.read(settings_path):
        raise SystemExit(f"Missing {settings_path} (copy settings.ini.example)")

    bot = SignalBot(
        Config(
            connection_mode=config["signal"]["connection_mode"],
            signal_service=config["signal"]["api_url"],
            phone_number=config["signal"]["phone_number"],
            logging_level=logging.INFO,
            storage=SQLiteConfig(
                db=settings_path.parent / config.get("diary", "state_db", fallback="signalli.db")
            ),
        )
    )
    diary = Diary(bot, config)

    # Only answer the owner's number, never groups. Commands win over chat (priority).
    only_me = {"contacts": [diary.owner], "groups": False}
    bot.register(PingCommand(), **only_me, priority=2)
    bot.register(SlashCommand(diary), **only_me, priority=2)
    bot.register(MessageHandler(diary), **only_me, priority=1)

    hour, minute = diary.reminder_time.split(":")
    bot.scheduler.add_job(diary.send_reminder, "cron", hour=int(hour), minute=int(minute))

    bot.start()
