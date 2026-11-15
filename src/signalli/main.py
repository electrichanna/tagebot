import asyncio
import base64
import configparser
import inspect
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
    ollama_list_models,
    ollama_parse_response,
    ollama_response_message,
    smb_read_json,
    smb_register,
    smb_write_bytes,
    smb_write_json,
    transcribe_bytes,
    transcribe_openai,
)
from prompts import CHAT_SYSTEM_PROMPT, DIARY_REMINDER, RATING_REMINDER

REMINDER_JOB_ID = "evening-reminder"


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Diary:
    """Shared logic: parameters, reminders, transcription and storage on the SMB share."""

    def __init__(self, bot: SignalBot, config: configparser.ConfigParser) -> None:
        self.bot = bot
        self.owner = config["signal"]["owner_number"]

        openai = config["openai"]
        self.llm_url = f"{openai['api_url'].rstrip('/')}:{openai['api_port']}/v1"
        self.default_llm_model = openai.get("model", OLLAMA_MODEL)
        self.default_temperature = openai.getfloat("temperature", fallback=0.0)
        self.llm_api_key = openai.get("api_key", "not-needed")
        extra = config.get("ai", "add_system_prompt", fallback="").strip()
        self.chat_system_prompt = CHAT_SYSTEM_PROMPT + ("\n" + extra if extra else "")

        # Speech-to-text: "wyoming" (vocabulary only via server restart) or "speaches" (vocabulary per request)
        self.asr_backend = config.get("asr", "backend", fallback="wyoming").strip().lower()
        if self.asr_backend not in ("wyoming", "speaches"):
            raise ValueError(f"[asr] backend must be wyoming or speaches, not {self.asr_backend!r}")
        self.asr_language = (
            config.get("asr", "language", fallback="") or config.get("wyoming", "language", fallback="")
        ).strip() or None

        if self.asr_backend == "wyoming":
            wyoming = config["wyoming"]
            self.asr_host = wyoming["api_url"]
            self.asr_port = int(wyoming["api_port"])
        else:
            speaches = config["speaches"]
            self.asr_url = f"{speaches['api_url'].rstrip('/')}:{speaches['api_port']}/v1"
            self.asr_model = speaches.get("model", "Systran/faster-whisper-large-v3")
            self.asr_api_key = speaches.get("api_key", "not-needed")

        diary = config["diary"]
        self.default_reminder_time = diary.get("reminder_time", "21:00")
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

    # ---- vocabulary (names and terms the transcription should get right) --------

    @property
    def vocabulary(self) -> list[str]:
        return self._setting("vocabulary", [])

    @vocabulary.setter
    def vocabulary(self, value: list[str]) -> None:
        self.bot.storage.save("vocabulary", value)

    @property
    def initial_prompt(self) -> str:
        """The vocabulary as faster-whisper initial prompt (--initial-prompt of the Wyoming server)."""
        return ", ".join(self.vocabulary) + "." if self.vocabulary else ""

    # ---- settings changeable in chat (stored, config value is the default) -------

    def _setting(self, key: str, default):
        return self.bot.storage.read(key) if self.bot.storage.exists(key) else default

    @property
    def llm_model(self) -> str:
        return self._setting("llm_model", self.default_llm_model)

    @llm_model.setter
    def llm_model(self, value: str) -> None:
        self.bot.storage.save("llm_model", value)

    @property
    def temperature(self) -> float:
        return self._setting("temperature", self.default_temperature)

    @temperature.setter
    def temperature(self, value: float) -> None:
        self.bot.storage.save("temperature", value)

    @property
    def reminder_time(self) -> str:
        """HH:MM, local time."""
        return self._setting("reminder_time", self.default_reminder_time)

    @reminder_time.setter
    def reminder_time(self, value: str) -> None:
        self.bot.storage.save("reminder_time", value)
        self.schedule_reminder()

    # ---- reminders --------------------------------------------------------------

    def schedule_reminder(self) -> None:
        """(Re)create the daily reminder job at the current reminder_time."""
        hour, minute = self.reminder_time.split(":")
        self.bot.scheduler.add_job(
            self.send_reminder, "cron", hour=int(hour), minute=int(minute), id=REMINDER_JOB_ID, replace_existing=True
        )

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
                text = await self.transcribe(audio)
                extension = mimetypes.guess_extension(attachment.content_type or "") or ".bin"
                return text.strip(), audio, extension
        return (context.message.text or "").strip(), None, None

    async def transcribe(self, audio: bytes) -> str:
        if self.asr_backend == "speaches":
            return await transcribe_openai(
                audio,
                url=self.asr_url,
                model=self.asr_model,
                api_key=self.asr_api_key,
                language=self.asr_language,
                prompt=self.initial_prompt or None,
            )
        return await transcribe_bytes(audio, self.asr_host, self.asr_port, self.asr_language)

    @property
    def transcript_model(self) -> str:
        """Stored with each voice diary entry."""
        return f"speaches:{self.asr_model}" if self.asr_backend == "speaches" else "wyoming"

    @property
    def vocab_needs_restart(self) -> bool:
        """Wyoming only knows the --initial-prompt given at server start."""
        return self.asr_backend == "wyoming"

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
                entry["transcript_model"] = self.transcript_model

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
            temperature=self.temperature,
        )

    async def list_models(self) -> list[str]:
        return await asyncio.to_thread(ollama_list_models, url=self.llm_url, api_key=self.llm_api_key)


class PingCommand(DataMessageHandler):
    def __init__(self, diary: Diary) -> None:
        self.diary = diary

    @text_triggered("Ping")
    async def handle_data_message(self, context: DataMessageContext) -> None:
        if not context.message.source_number == self.diary.owner:
            logging.exception("message from unknown source came through")
            return
        await context.send(SendMessage(text="Pong"))


class SlashCommand:
    def __init__(self, command: str, method, helpstr: str, usage: str = "", raw_args: bool = False) -> None:
        assert callable(method), "method must be callable"
        assert command.startswith("/"), "command must start with /"

        self.command = command.lower()
        self.method = method
        self.usage = usage
        # raw_args: the method gets the text after the command as one string instead of a word list
        self.raw_args = raw_args
        self._helpstr = helpstr

    def matches(self, command: str) -> bool:
        # exact match, so e.g. /param does not catch /params
        return command.lower() == self.command

    @property
    def helpstr(self) -> str:
        """returns full help line, including command, usage and help text"""
        usage = f" {self.usage}" if self.usage else ""
        return f"{self.command}{usage} - {self._helpstr}"

    @helpstr.setter
    def helpstr(self, value: str) -> None:
        """set the help string (only the description part)"""
        self._helpstr = value


class SlashCommandHandler(DataMessageHandler):
    """Dispatches /commands to the registered SlashCommands. Unknown commands show the help."""

    def __init__(self, diary: Diary) -> None:
        self.diary = diary
        self.commands: list[SlashCommand] = []

        self._add_cmd([
            SlashCommand(
                command="/help",
                method=self._cmd_help,
                helpstr="this help",
            ),
            SlashCommand(
                command="/params",
                method=self._cmd_params,
                usage="[add|remove <name> ...]",
                helpstr="no arg: show parameters, add: add parameters, remove: remove parameters",
            ),
            SlashCommand(
                command="/remind",
                method=self._cmd_remind,
                helpstr="send the evening check-in now",
            ),
            SlashCommand(
                command="/temp",
                method=self._cmd_temp,
                usage="[<0-2>]",
                helpstr="show or change the chat temperature",
            ),
            SlashCommand(
                command="/schedule",
                method=self._cmd_schedule,
                usage="[HHMM]",
                helpstr="show or change the check-in time (e.g. 2130 for 21:30)",
            ),
            SlashCommand(
                command="/model",
                method=self._cmd_model,
                usage="[list|<name>]",
                helpstr="no arg: show the model, list: list all available models, <name>: change the model",
            ),
            SlashCommand(
                command="/vocab",
                method=self._cmd_vocab,
                usage="[add|remove <term>, ... | prompt]",
                helpstr="no arg: show the transcription vocabulary, add/remove: comma-separated terms "
                "(remove also takes numbers), prompt: show the Whisper prompt built from it",
                raw_args=True,
            ),
        ])

    def _add_cmd(self, command: SlashCommand | list[SlashCommand]) -> None:
        if isinstance(command, list):
            self.commands.extend(command)
        else:
            self.commands.append(command)

    def _find_cmd(self, command: str) -> SlashCommand | None:
        return next((cmd for cmd in self.commands if cmd.matches(command)), None)

    # ---- commands: each takes the arguments and returns the reply (None = no reply) ----

    def _cmd_help(self, args: list[str]) -> str:
        return "\n".join(cmd.helpstr for cmd in self.commands)

    def _cmd_params(self, args: list[str]) -> str:
        parameters = self.diary.parameters
        if not args:
            return "Parameters: " + (", ".join(parameters) or "none")

        action, *names = (a.lower() for a in args)
        if action not in ("add", "remove") or not names:
            return "Usage: " + self._find_cmd("/params").helpstr

        if action == "add":
            invalid = [n for n in names if not re.fullmatch(r"[\w-]+", n)]
            if invalid:
                return f"Invalid name(s): {', '.join(invalid)} (letters, digits, _ and - only)"
            parameters += [n for n in names if n not in parameters]
            self.diary.parameters = parameters
            return "Parameters: " + ", ".join(parameters)

        missing = [n for n in names if n not in parameters]
        self.diary.parameters = parameters = [p for p in parameters if p not in names]
        text = "Parameters: " + (", ".join(parameters) or "none")
        if missing:
            text += f"\nNot found: {', '.join(missing)}"
        return text

    async def _cmd_remind(self, args: list[str]) -> None:
        await self.diary.send_reminder()

    def _cmd_temp(self, args: list[str]) -> str:
        if not args:
            return f"Temperature: {self.diary.temperature}"
        try:
            value = float(args[0].replace(",", "."))
        except ValueError:
            value = None
        if value is None or not 0 <= value <= 2:
            return "Temperature must be a number between 0 and 2"
        self.diary.temperature = value
        return f"Temperature set to {value}"

    def _cmd_schedule(self, args: list[str]) -> str:
        if not args:
            return f"Check-in time: {self.diary.reminder_time}"
        match = re.fullmatch(r"(\d{1,2}):?(\d{2})", args[0])
        hour, minute = (int(g) for g in match.groups()) if match else (-1, -1)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return "Time must be HHMM or HH:MM, e.g. 2130"
        self.diary.reminder_time = f"{hour:02d}:{minute:02d}"
        return f"Check-in time set to {self.diary.reminder_time}"

    async def _cmd_model(self, args: list[str]) -> str:
        if not args:
            return f"Model: {self.diary.llm_model}"
        name = args[0]
        try:
            models = await self.diary.list_models()
        except Exception:
            logging.exception("Listing models failed")
            return "⚠️ Could not fetch the model list."

        if name.lower() == "list":
            current = self.diary.llm_model
            return "Models:\n" + "\n".join(f"{'▶' if m == current else '•'} {m}" for m in models)
        if name not in models:
            return f"Unknown model: {name}\nSee /model list"
        self.diary.llm_model = name
        return f"Model set to {name}"

    def _cmd_vocab(self, text: str) -> str:
        def listing(vocabulary: list[str]) -> str:
            if not vocabulary:
                return "Vocabulary: empty"
            return "Vocabulary:\n" + "\n".join(f"{i}. {term}" for i, term in enumerate(vocabulary, 1))

        vocabulary = self.diary.vocabulary
        action, rest = (text.split(maxsplit=1) + ["", ""])[:2]
        action = action.lower()
        terms = [t.strip() for t in rest.split(",") if t.strip()]

        if not action:
            return listing(vocabulary)

        if action == "prompt":
            if not vocabulary:
                return "Vocabulary is empty, nothing to put into the prompt."
            prompt = self.diary.initial_prompt
            if self.diary.vocab_needs_restart:
                text = f"Start the Wyoming faster-whisper server with:\n--initial-prompt \"{prompt}\""
            else:
                text = f"Sent with every transcription request:\n{prompt}"
            if len(prompt) > 600:  # Whisper keeps only ~224 prompt tokens
                text += "\n⚠️ Long prompt, Whisper may cut off the end."
            return text

        if action not in ("add", "remove") or not terms:
            return "Usage: " + self._find_cmd("/vocab").helpstr

        lowered = [v.lower() for v in vocabulary]
        if action == "add":
            added = []
            for term in terms:
                if term.lower() not in lowered:
                    vocabulary.append(term)
                    lowered.append(term.lower())
                    added.append(term)
            self.diary.vocabulary = vocabulary
            reply = listing(vocabulary)
            if added and self.diary.vocab_needs_restart:
                reply += "\n\nRestart the Whisper server with the new prompt to use it, see /vocab prompt"
            return reply

        # remove: by term (case-insensitive) or by list number
        remove, missing = set(), []
        for term in terms:
            if term.isdigit() and 1 <= int(term) <= len(vocabulary):
                remove.add(int(term) - 1)
            elif term.lower() in lowered:
                remove.add(lowered.index(term.lower()))
            else:
                missing.append(term)
        self.diary.vocabulary = vocabulary = [v for i, v in enumerate(vocabulary) if i not in remove]
        reply = listing(vocabulary)
        if missing:
            reply += f"\nNot found: {', '.join(missing)}"
        return reply

    @regex_triggered(r"^/")
    async def handle_data_message(self, context: DataMessageContext) -> None:
        if not context.message.source_number == self.diary.owner:
            logging.exception("message from unknown source came through")
            return

        command, raw = (context.message.text.split(maxsplit=1) + [""])[:2]
        args = [a.strip(",") for a in raw.split() if a.strip(",")]

        cmd = self._find_cmd(command)
        result = (cmd.method if cmd else self._cmd_help)(raw.strip() if cmd and cmd.raw_args else args)
        if inspect.isawaitable(result):
            result = await result
        if result:
            await context.send(SendMessage(text=result))


class MessageHandler(DataMessageHandler):
    """Replies to a reminder are diary input, everything else is normal AI chat."""

    def __init__(self, diary: Diary) -> None:
        self.diary = diary
        self.history: deque[dict] = deque(maxlen=20)

    async def handle_data_message(self, context: DataMessageContext) -> None:
        if not context.message.source_number == self.diary.owner:
            logging.exception("message from unknown source came through")
            return

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
    bot.register(PingCommand(diary), **only_me, priority=2)
    bot.register(SlashCommandHandler(diary), **only_me, priority=2)
    bot.register(MessageHandler(diary), **only_me, priority=1)

    diary.schedule_reminder()

    bot.start()
