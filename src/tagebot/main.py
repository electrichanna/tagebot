"""Entry point and message handling for the Signal diary bot.

The bot sends a daily check-in to a single configured phone number. Replies to
the check-in are stored as numeric ratings or diary entries on an SMB share;
voice messages are transcribed first. Messages starting with ``/`` are handled
as commands, and all other messages are forwarded to an LLM for free-form chat.

Attributes:
    REMINDER_JOB_ID: Scheduler job ID of the daily check-in.
    LOAD_STATE_JOB_ID: Scheduler job ID of the retrying state loader.
    STATE_FILE: File name of the runtime settings file on the SMB share.
    SOURCE_URL: Source code location shown in ``/help`` (AGPL-3.0 requirement).
"""

import asyncio
import base64
import inspect
import logging
import mimetypes
import ntpath
import re
from collections import deque
from datetime import datetime

from signalbot import (
    Config,
    DataMessageContext,
    DataMessageHandler,
    SendMessage,
    InMemoryConfig,
    SignalBot,
    regex_triggered,
    text_triggered,
)

from connectors import (
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
from settings import Settings, load_settings

REMINDER_JOB_ID = "evening-reminder"
LOAD_STATE_JOB_ID = "load-state"
# chat-changeable settings, stored next to the diary on the SMB share
STATE_FILE = "settings.json"
# shown in /help; AGPL-3.0 requires offering the source of the running version to its users
SOURCE_URL = "https://github.com/electrichanna/tagebot"


def now_iso() -> str:
    """Returns the current local time as an ISO 8601 string.

    Returns:
        Timestamp with second precision and UTC offset,
        e.g. ``2024-05-01T21:00:00+02:00``.
    """
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Diary:
    """Core application logic shared by all message handlers.

    Manages the rating parameters, scheduled check-ins, audio transcription,
    LLM access and persistence on the SMB share.

    Settings that can be changed via chat commands (parameters, vocabulary,
    model, temperature, check-in time) are persisted in ``STATE_FILE`` on the
    share. The values from the environment act as defaults only. No data is
    stored on the local filesystem.

    Attributes:
        bot: The running Signal bot instance.
        owner: Phone number allowed to interact with the bot.
        bot_number: Phone number of the bot account.
        llm_url: Base URL of the OpenAI-compatible chat API.
        default_llm_model: Chat model used if none is stored in the state.
        default_temperature: Temperature used if none is stored in the state.
        llm_api_key: API key for the chat API.
        chat_system_prompt: System prompt for chat, including any configured
            extension.
        asr_backend: Transcription backend, ``"speaches"`` or ``"wyoming"``.
        asr_language: Transcription language, or ``None`` for auto-detection.
        asr_host: Wyoming server hostname.
        asr_port: Wyoming server port.
        asr_url: Base URL of the OpenAI-compatible transcription API.
        asr_model: Transcription model name.
        asr_api_key: API key for the transcription API.
        default_reminder_time: Check-in time (``HH:MM``) used if none is stored.
        default_parameters: Parameter names used if none are stored.
        smb_session: ``(server, username, password, port)`` tuple for
            :func:`connectors.smb_register`.
        base_path: UNC path of the data directory on the share.
        state_path: UNC path of ``STATE_FILE``.
    """

    def __init__(self, bot: SignalBot, settings: Settings) -> None:
        """Initialises the diary from the loaded configuration.

        Args:
            bot: The Signal bot used for sending messages and scheduling jobs.
            settings: Validated application configuration.
        """
        self.bot = bot
        self.owner = settings.signal.owner_number
        self.bot_number = settings.signal.phone_number

        llm = settings.llm
        self.llm_url = llm.base_url
        self.default_llm_model = llm.model
        self.default_temperature = llm.temperature
        self.llm_api_key = llm.api_key.get_secret_value()
        extra = llm.add_system_prompt.strip()
        self.chat_system_prompt = CHAT_SYSTEM_PROMPT + ("\n" + extra if extra else "")

        # Speech-to-text: "wyoming" (vocabulary only via server restart) or "speaches" (vocabulary per request)
        self.asr_backend = settings.asr.backend
        self.asr_language = settings.asr.language
        self.asr_host = settings.wyoming.host
        self.asr_port = settings.wyoming.port
        self.asr_url = settings.speaches.base_url
        self.asr_model = settings.speaches.model
        self.asr_api_key = settings.speaches.api_key.get_secret_value()

        self.default_reminder_time = settings.diary.reminder_time
        self.default_parameters = settings.diary.parameters

        smb = settings.smb
        self.smb_session = (smb.server, smb.username, smb.password.get_secret_value(), smb.port)
        self.base_path = ntpath.join(rf"\\{smb.server}\{smb.share}", *smb.base_dir.replace("/", "\\").split("\\"))

        self.state_path = ntpath.join(self.base_path, STATE_FILE)
        self._state: dict = {}

        self._write_lock = asyncio.Lock()

    # ---- settings changeable in chat (settings.json on the share, env value is the default) ----

    def _setting(self, key: str, default):
        """Returns a runtime setting from the cached state.

        Args:
            key: Setting name in ``STATE_FILE``.
            default: Value returned if the key is not stored.

        Returns:
            The stored value, or ``default``.
        """
        return self._state.get(key, default)

    async def save_setting(self, key: str, value) -> None:
        """Persists a runtime setting to ``STATE_FILE`` and updates the cache.

        The file is re-read under the write lock before updating, so
        concurrent changes to other keys are preserved.

        Args:
            key: Setting name.
            value: JSON-serialisable value to store.

        Raises:
            OSError: If the share cannot be read or written.
        """
        async with self._write_lock:
            state = await self.smb(smb_read_json, self.state_path) or {}
            state[key] = value
            await self.smb(smb_write_json, self.state_path, state)
            self._state = state

    async def load_state(self) -> None:
        """Loads ``STATE_FILE`` into the cache.

        Intended to run as the ``LOAD_STATE_JOB_ID`` interval job. On failure
        a warning is logged and the defaults remain in effect until the next
        run. On success the job is removed and the check-in is rescheduled,
        since the stored time may differ from the default.
        """
        try:
            self._state = await self.smb(smb_read_json, self.state_path) or {}
        except Exception as err:
            logging.warning("Could not read %s (%s), using the defaults, retrying in 5 minutes", self.state_path, err)
            return
        self.bot.scheduler.remove_job(LOAD_STATE_JOB_ID)
        self.schedule_reminder()  # the stored check-in time may differ from the default

    def start_loading_state(self) -> None:
        """Schedules :meth:`load_state` to run immediately and then every 5 minutes until it succeeds."""
        self.bot.scheduler.add_job(
            self.load_state, "interval", minutes=5, id=LOAD_STATE_JOB_ID, next_run_time=datetime.now()
        )

    @property
    def parameters(self) -> list[str]:
        """list[str]: Names of the rated parameters (a copy, safe to modify)."""
        return list(self._setting("parameters", self.default_parameters))

    # vocabulary: names and terms the transcription should get right
    @property
    def vocabulary(self) -> list[str]:
        """list[str]: Terms used to bias transcription (a copy, safe to modify)."""
        return list(self._setting("vocabulary", []))

    @property
    def initial_prompt(self) -> str:
        """str: The vocabulary formatted as a Whisper initial prompt.

        Comma-separated terms ending in a period, or an empty string if the
        vocabulary is empty. For the Wyoming backend this value has to be
        passed to the server via ``--initial-prompt``.
        """
        return ", ".join(self.vocabulary) + "." if self.vocabulary else ""

    @property
    def llm_model(self) -> str:
        """str: Name of the active chat model."""
        return self._setting("llm_model", self.default_llm_model)

    @property
    def temperature(self) -> float:
        """float: Sampling temperature for chat responses."""
        return self._setting("temperature", self.default_temperature)

    @property
    def reminder_time(self) -> str:
        """str: Local time of the daily check-in as ``HH:MM``."""
        return self._setting("reminder_time", self.default_reminder_time)

    # ---- reminders --------------------------------------------------------------

    def schedule_reminder(self) -> None:
        """Creates or replaces the daily check-in job at :attr:`reminder_time`."""
        hour, minute = self.reminder_time.split(":")
        self.bot.scheduler.add_job(
            self.send_reminder, "cron", hour=int(hour), minute=int(minute), id=REMINDER_JOB_ID, replace_existing=True
        )

    async def send_reminder(self) -> None:
        """Sends the check-in messages to the owner.

        Sends the rating request (only if parameters are configured), followed
        by the diary request. Replies to either message are identified later
        by :meth:`reminder_for`.
        """
        parameters = self.parameters
        if parameters:
            text = RATING_REMINDER.format(
                parameters="\n".join(f"• {p}" for p in parameters),
                example=" ".join(str(5 + i % 3) for i in range(len(parameters))),
                example_named=", ".join(f"{p} {7 - i}" for i, p in enumerate(parameters[:2])),
            )
            await self.bot.messages.send(SendMessage(text=text), self.owner)
        await self.bot.messages.send(SendMessage(text=DIARY_REMINDER), self.owner)

    def reminder_for(self, context: DataMessageContext) -> dict | None:
        """Identifies the check-in message that an incoming message replies to.

        A quoted message counts as a check-in if it was sent by the bot and
        starts with the first line of ``RATING_REMINDER`` or
        ``DIARY_REMINDER``. If the quote's author number is missing, only the
        text is checked.

        Args:
            context: Context of the incoming message.

        Returns:
            ``{"kind": "rating" | "diary", "date": "YYYY-MM-DD"}``, where
            ``date`` is the local date the check-in was sent (derived from the
            quote ID, a millisecond timestamp), or ``None`` if the message
            does not reply to a check-in.
        """
        quote = context.message.quote
        if quote is None or not quote.text:
            return None
        # only the bot's own messages (author_number can be missing, then the text decides)
        if quote.author == self.owner or quote.author_number not in (None, self.bot_number):
            return None
        for kind, template in (("rating", RATING_REMINDER), ("diary", DIARY_REMINDER)):
            if quote.text.startswith(template.split("\n", 1)[0]):
                date = datetime.fromtimestamp(quote.id / 1000).astimezone().date().isoformat()
                return {"kind": kind, "date": date}
        return None

    # ---- input ------------------------------------------------------------------

    async def read_input(self, context: DataMessageContext) -> tuple[str, bytes | None, str | None]:
        """Extracts the text content of a message, transcribing audio if present.

        The first audio attachment (voice note or ``audio/*`` content type)
        takes precedence over the message text.

        Args:
            context: Context of the incoming message.

        Returns:
            A tuple ``(text, audio, extension)``. For audio messages, ``text``
            is the transcript, ``audio`` the raw attachment and ``extension``
            a file extension guessed from the MIME type (``".bin"`` if
            unknown). For text messages, ``audio`` and ``extension`` are
            ``None``. ``text`` is always stripped.

        Raises:
            Exception: Any error raised by the transcription backend.
        """
        for attachment in context.message.attachments or []:
            is_audio = attachment.is_voice_note or (attachment.content_type or "").startswith("audio/")
            if is_audio and attachment.base64_content:
                audio = base64.b64decode(attachment.base64_content)
                text = await self.transcribe(audio)
                extension = mimetypes.guess_extension(attachment.content_type or "") or ".bin"
                return text.strip(), audio, extension
        return (context.message.text or "").strip(), None, None

    async def transcribe(self, audio: bytes) -> str:
        """Transcribes audio using the configured ASR backend.

        For the Speaches backend, :attr:`initial_prompt` is sent with the
        request to bias the vocabulary.

        Args:
            audio: Encoded audio data.

        Returns:
            The transcribed text.
        """
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
        """str: Identifier of the transcription backend, stored with voice diary entries.

        ``"speaches:<model>"`` for Speaches, ``"wyoming"`` for Wyoming.
        """
        return f"speaches:{self.asr_model}" if self.asr_backend == "speaches" else "wyoming"

    @property
    def vocab_needs_restart(self) -> bool:
        """bool: Whether vocabulary changes require restarting the ASR server.

        True for Wyoming, which only reads ``--initial-prompt`` at startup.
        """
        return self.asr_backend == "wyoming"

    # ---- storage ----------------------------------------------------------------

    async def smb(self, func, *args):
        """Runs a blocking SMB function in a worker thread.

        The SMB session is re-registered before each call, so connections
        dropped by a server restart are re-established transparently.

        Args:
            func: Function from :mod:`connectors` to call.
            *args: Positional arguments passed to ``func``.

        Returns:
            The return value of ``func``.
        """

        def run():
            smb_register(*self.smb_session)
            return func(*args)

        return await asyncio.to_thread(run)

    def day_path(self, date: str, filename: str) -> str:
        """Builds the UNC path of a file in a day's directory.

        Files are organised as ``<base_path>/<YYYY>/<YYYY-MM-DD>/<filename>``.

        Args:
            date: Date as ``YYYY-MM-DD``.
            filename: Name of the file within the day's directory.

        Returns:
            The full UNC path.
        """
        return ntpath.join(self.base_path, date[:4], date, filename)

    async def save_ratings(self, date: str, ratings: dict, text: str, source: str) -> dict:
        """Merges ratings into the day's ``parameters.json``.

        Ratings with value ``None`` are ignored, so existing values are only
        overwritten by new valid ratings. The raw input is appended to the
        file's ``inputs`` list for traceability.

        Args:
            date: Date of the check-in as ``YYYY-MM-DD``.
            ratings: Mapping of parameter name to rating or ``None``.
            text: The original input text or transcript.
            source: Input type, ``"text"`` or ``"voice"``.

        Returns:
            All ratings stored for the day after the merge.

        Raises:
            OSError: If the share cannot be read or written.
        """
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
        """Appends an entry to the day's ``diary.json``.

        For voice entries, the audio is stored alongside as
        ``diary_<HHMMSS>_<ms><extension>`` and referenced from the entry
        together with the transcription backend identifier.

        Args:
            date: Date of the check-in as ``YYYY-MM-DD``.
            text: Entry text or transcript.
            audio: Raw audio data, or ``None`` for text entries.
            extension: File extension for the audio file (e.g. ``".m4a"``).
            signal_timestamp: Signal message timestamp in milliseconds, used
                as the entry timestamp and for the audio file name.

        Raises:
            OSError: If the share cannot be read or written.
        """
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
        """Parses ratings from a check-in reply.

        If the text consists only of numbers (separated by whitespace, ``,``,
        ``;`` or ``/``) and their count matches the number of parameters, they
        are assigned in order without calling the LLM. Otherwise the LLM
        extracts the ratings.

        Args:
            text: Reply text or transcript.

        Returns:
            Mapping of parameter name to rating (1-10). Parameters not found
            in the text map to ``None`` when the LLM is used.
        """
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
        """Generates a chat reply with the active model and temperature.

        Args:
            text: The new user message.
            history: Previous turns as ``{"role", "content"}`` dicts.

        Returns:
            The model's reply.
        """
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
        """Lists the models available on the chat endpoint.

        Returns:
            Model IDs, sorted alphabetically.
        """
        return await asyncio.to_thread(ollama_list_models, url=self.llm_url, api_key=self.llm_api_key)


class PingCommand(DataMessageHandler):
    """Replies ``Pong`` to the exact message ``Ping`` as a connectivity check.

    Attributes:
        diary: Shared application state.
    """

    def __init__(self, diary: Diary) -> None:
        """Initialises the handler.

        Args:
            diary: Shared application state.
        """
        self.diary = diary

    @text_triggered("Ping")
    async def handle_data_message(self, context: DataMessageContext) -> None:
        """Sends ``Pong`` if the message comes from the owner.

        Args:
            context: Context of the incoming message.
        """
        if not context.message.source_number == self.diary.owner:
            logging.exception("message from unknown source came through")
            return
        await context.send(SendMessage(text="Pong"))


class SlashCommand:
    """Definition of a single ``/command``: its handler and help text.

    Attributes:
        command: Command name including the leading ``/``, lower-cased.
        method: Handler called with the arguments; returns the reply text or
            ``None``. May be a coroutine function.
        section: Heading under which the command is listed in ``/help``.
        summary: Description of the command without arguments.
        details: ``(arguments, description)`` pairs for variants with
            arguments.
        raw_args: If True, ``method`` receives the text after the command as
            a single string instead of a list of words.
    """

    def __init__(
        self,
        command: str,
        method,
        section: str,
        summary: str,
        details: list[tuple[str, str]] | None = None,
        raw_args: bool = False,
    ) -> None:
        """Initialises the command definition.

        Args:
            command: Command name including the leading ``/``.
            method: Handler callable; see :attr:`method`.
            section: Heading under which the command is listed in ``/help``.
            summary: Description of the command without arguments.
            details: ``(arguments, description)`` pairs for variants with
                arguments.
            raw_args: Pass the argument text unsplit; see :attr:`raw_args`.

        Raises:
            AssertionError: If ``method`` is not callable or ``command`` does
                not start with ``/``.
        """
        assert callable(method), "method must be callable"
        assert command.startswith("/"), "command must start with /"

        self.command = command.lower()
        self.method = method
        self.section = section
        self.summary = summary
        self.details = details or []
        self.raw_args = raw_args

    def matches(self, command: str) -> bool:
        """Checks whether a command token refers to this command.

        Matching is exact and case-insensitive, so ``/param`` does not match
        ``/params``.

        Args:
            command: First word of the incoming message.

        Returns:
            True if the token matches :attr:`command`.
        """
        # exact match, so e.g. /param does not catch /params
        return command.lower() == self.command

    @property
    def helpstr(self) -> str:
        """str: Help text: the summary line followed by one indented line per variant."""
        lines = [f"{self.command} – {self.summary}"]
        lines += [f"   {self.command} {args} – {desc}" for args, desc in self.details]
        return "\n".join(lines)


class SlashCommandHandler(DataMessageHandler):
    """Dispatches messages starting with ``/`` to the registered :class:`SlashCommand` handlers.

    Unknown commands reply with the help text. Exceptions raised by a command
    are logged and reported to the sender as a generic error message.

    Attributes:
        diary: Shared application state.
        commands: Registered commands, in ``/help`` display order.
    """

    def __init__(self, diary: Diary) -> None:
        """Initialises the handler and registers the built-in commands.

        Args:
            diary: Shared application state.
        """
        self.diary = diary
        self.commands: list[SlashCommand] = []

        self._add_cmd([
            SlashCommand(
                command="/remind",
                method=self._cmd_remind,
                section="Check-in",
                summary="send the evening check-in now",
            ),
            SlashCommand(
                command="/schedule",
                method=self._cmd_schedule,
                section="Check-in",
                summary="show the check-in time",
                details=[("<HHMM>", "change it, e.g. 2130 for 21:30")],
            ),
            SlashCommand(
                command="/params",
                method=self._cmd_params,
                section="Check-in",
                summary="show the rating parameters",
                details=[
                    ("add <name> …", "add parameters"),
                    ("remove <name> …", "remove parameters"),
                ],
            ),
            SlashCommand(
                command="/model",
                method=self._cmd_model,
                section="Chat",
                summary="show the current model",
                details=[
                    ("list", "list all available models"),
                    ("<name>", "switch to another model"),
                ],
            ),
            SlashCommand(
                command="/temp",
                method=self._cmd_temp,
                section="Chat",
                summary="show the chat temperature",
                details=[("<0-2>", "change it")],
            ),
            SlashCommand(
                command="/vocab",
                method=self._cmd_vocab,
                section="Voice messages",
                summary="show the transcription vocabulary",
                details=[
                    ("add <term>, …", "add comma-separated terms"),
                    ("remove <term|number>, …", "remove terms by name or list number"),
                ],
                raw_args=True,
            ),
            SlashCommand(
                command="/help",
                method=self._cmd_help,
                section="General",
                summary="show this help",
            ),
        ])

    def _add_cmd(self, command: SlashCommand | list[SlashCommand]) -> None:
        """Registers one or more commands.

        Args:
            command: A single command or a list of commands.
        """
        if isinstance(command, list):
            self.commands.extend(command)
        else:
            self.commands.append(command)

    def _find_cmd(self, command: str) -> SlashCommand | None:
        """Looks up a registered command by name.

        Args:
            command: Command token including the leading ``/``.

        Returns:
            The first matching command, or ``None``.
        """
        return next((cmd for cmd in self.commands if cmd.matches(command)), None)

    # ---- commands: each takes the arguments and returns the reply (None = no reply) ----

    def _cmd_help(self, args: list[str]) -> str:
        """Builds the ``/help`` text.

        Commands are grouped by section in registration order. The ``Ping``
        handler and the source code notice are appended.

        Args:
            args: Ignored.

        Returns:
            The help text.
        """
        sections: dict[str, list[str]] = {}
        for cmd in self.commands:
            sections.setdefault(cmd.section, []).append(cmd.helpstr)
        sections.setdefault("General", []).append("Ping – connection test, answers Pong")

        blocks = ["tagebot commands"]
        blocks += [f"{name.upper()}\n" + "\n".join(lines) for name, lines in sections.items()]
        blocks.append(f"tagebot is free software (AGPL-3.0). Source code:\n{SOURCE_URL}")
        return "\n\n".join(blocks)

    async def _cmd_params(self, args: list[str]) -> str:
        """Shows, adds or removes rating parameters (``/params``).

        Parameter names are lower-cased and may contain only word characters
        and ``-``. Adding an existing name is a no-op.

        Args:
            args: Empty to show the list, or ``["add" | "remove", name, ...]``.

        Returns:
            The updated parameter list, a usage message, or a validation error.
        """
        parameters = self.diary.parameters
        if not args:
            return "Parameters: " + (", ".join(parameters) or "none")

        action, *names = (a.lower() for a in args)
        if action not in ("add", "remove") or not names:
            return "Usage:\n" + self._find_cmd("/params").helpstr

        if action == "add":
            invalid = [n for n in names if not re.fullmatch(r"[\w-]+", n)]
            if invalid:
                return f"Invalid name(s): {', '.join(invalid)} (letters, digits, _ and - only)"
            parameters += [n for n in names if n not in parameters]
            await self.diary.save_setting("parameters", parameters)
            return "Parameters: " + ", ".join(parameters)

        missing = [n for n in names if n not in parameters]
        parameters = [p for p in parameters if p not in names]
        await self.diary.save_setting("parameters", parameters)
        text = "Parameters: " + (", ".join(parameters) or "none")
        if missing:
            text += f"\nNot found: {', '.join(missing)}"
        return text

    async def _cmd_remind(self, args: list[str]) -> None:
        """Sends the check-in messages immediately (``/remind``).

        Args:
            args: Ignored.

        Returns:
            None; the check-in messages serve as the reply.
        """
        await self.diary.send_reminder()

    async def _cmd_temp(self, args: list[str]) -> str:
        """Shows or sets the chat temperature (``/temp``).

        Args:
            args: Empty to show the value, or ``[value]`` with a number
                between 0 and 2. A decimal comma is accepted.

        Returns:
            The current or new temperature, or a validation error.
        """
        if not args:
            return f"Temperature: {self.diary.temperature}"
        try:
            value = float(args[0].replace(",", "."))
        except ValueError:
            value = None
        if value is None or not 0 <= value <= 2:
            return "Temperature must be a number between 0 and 2"
        await self.diary.save_setting("temperature", value)
        return f"Temperature set to {value}"

    async def _cmd_schedule(self, args: list[str]) -> str:
        """Shows or sets the daily check-in time (``/schedule``).

        A new time is persisted and the scheduler job is replaced immediately.

        Args:
            args: Empty to show the time, or ``[time]`` as ``HHMM`` or
                ``HH:MM``.

        Returns:
            The current or new time, or a validation error.
        """
        if not args:
            return f"Check-in time: {self.diary.reminder_time}"
        match = re.fullmatch(r"(\d{1,2}):?(\d{2})", args[0])
        hour, minute = (int(g) for g in match.groups()) if match else (-1, -1)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return "Time must be HHMM or HH:MM, e.g. 2130"
        await self.diary.save_setting("reminder_time", f"{hour:02d}:{minute:02d}")
        self.diary.schedule_reminder()
        return f"Check-in time set to {self.diary.reminder_time}"

    async def _cmd_model(self, args: list[str]) -> str:
        """Shows, lists or switches the chat model (``/model``).

        A new model name is only accepted if the endpoint offers it.

        Args:
            args: Empty to show the active model, ``["list"]`` to list all
                available models, or ``[name]`` to switch.

        Returns:
            The active model, the model list (active one marked with ``▶``),
            a confirmation, or an error message.
        """
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
        await self.diary.save_setting("llm_model", name)
        return f"Model set to {name}"

    async def _cmd_vocab(self, text: str) -> str:
        """Shows, adds or removes transcription vocabulary terms (``/vocab``).

        Terms are comma-separated and may contain spaces. Duplicates are
        detected case-insensitively. Terms can be removed by name or by their
        1-based position in the list.

        Args:
            text: Raw argument text, e.g. ``"add Term A, Term B"`` or
                ``"remove 2, Term A"``. Empty to show the list.

        Returns:
            The numbered vocabulary list, optionally followed by a list of
            terms not found or a restart notice for the Wyoming backend, or a
            usage message.
        """
        def listing(vocabulary: list[str]) -> str:
            """Formats the vocabulary as a numbered list."""
            if not vocabulary:
                return "Vocabulary: empty"
            return "Vocabulary:\n" + "\n".join(f"{i}. {term}" for i, term in enumerate(vocabulary, 1))

        vocabulary = self.diary.vocabulary
        action, rest = (text.split(maxsplit=1) + ["", ""])[:2]
        action = action.lower()
        terms = [t.strip() for t in rest.split(",") if t.strip()]

        if not action:
            return listing(vocabulary)

        if action not in ("add", "remove") or not terms:
            return "Usage:\n" + self._find_cmd("/vocab").helpstr

        lowered = [v.lower() for v in vocabulary]
        if action == "add":
            added = []
            for term in terms:
                if term.lower() not in lowered:
                    vocabulary.append(term)
                    lowered.append(term.lower())
                    added.append(term)
            await self.diary.save_setting("vocabulary", vocabulary)
            reply = listing(vocabulary)
            if added and self.diary.vocab_needs_restart:
                reply += "\n\nRestart the Whisper server with the new --initial-prompt to use it"
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
        vocabulary = [v for i, v in enumerate(vocabulary) if i not in remove]
        await self.diary.save_setting("vocabulary", vocabulary)
        reply = listing(vocabulary)
        if missing:
            reply += f"\nNot found: {', '.join(missing)}"
        return reply

    @regex_triggered(r"^/")
    async def handle_data_message(self, context: DataMessageContext) -> None:
        """Parses a ``/command`` message, runs its handler and sends the reply.

        Arguments are split on whitespace with surrounding commas removed,
        unless the command uses ``raw_args``. Both sync and async handlers are
        supported.

        Args:
            context: Context of the incoming message.
        """
        if not context.message.source_number == self.diary.owner:
            logging.exception("message from unknown source came through")
            return

        command, raw = (context.message.text.split(maxsplit=1) + [""])[:2]
        args = [a.strip(",") for a in raw.split() if a.strip(",")]

        cmd = self._find_cmd(command)
        try:
            result = (cmd.method if cmd else self._cmd_help)(raw.strip() if cmd and cmd.raw_args else args)
            if inspect.isawaitable(result):
                result = await result
        except Exception:
            logging.exception("Command %s failed", command)
            result = f"⚠️ {command} failed. Is the SMB share reachable? Details are in the bot log."
        if result:
            await context.send(SendMessage(text=result))


class MessageHandler(DataMessageHandler):
    """Routes regular (non-command) messages.

    Replies to a rating check-in are stored as ratings, replies to a diary
    check-in as diary entries, and all other messages are answered by the
    LLM. Voice messages are transcribed first and the transcript is echoed
    back to the sender.

    Attributes:
        diary: Shared application state.
        history: The most recent chat messages (up to 20, i.e. 10 exchanges)
            passed to the LLM as context. Kept in memory only.
    """

    def __init__(self, diary: Diary) -> None:
        """Initialises the handler with an empty chat history.

        Args:
            diary: Shared application state.
        """
        self.diary = diary
        self.history: deque[dict] = deque(maxlen=20)

    async def handle_data_message(self, context: DataMessageContext) -> None:
        """Reads the message content and dispatches it to the matching handler.

        Messages without text or audio (e.g. reactions) are ignored.

        Args:
            context: Context of the incoming message.
        """
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
        """Answers a message with the LLM and records the exchange in the history.

        A typing indicator is shown while waiting. Failed requests are not
        added to the history.

        Args:
            context: Context of the incoming message.
            text: Message text or transcript.
        """
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
        """Parses and stores ratings from a check-in reply, then replies with a summary.

        The summary lists all configured parameters with their stored value,
        or ``–`` if none has been recorded yet for the day.

        Args:
            context: Context of the incoming message.
            date: Date of the check-in as ``YYYY-MM-DD``.
            text: Reply text or transcript.
            source: Input type, ``"text"`` or ``"voice"``.
        """
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
        """Stores a diary entry and confirms it with a 📝 reaction.

        Args:
            context: Context of the incoming message.
            date: Date of the check-in as ``YYYY-MM-DD``.
            text: Entry text or transcript.
            audio: Raw audio data, or ``None`` for text entries.
            extension: File extension for the audio file.
        """
        try:
            await self.diary.save_diary_entry(date, text, audio, extension, context.message.timestamp)
        except Exception:
            logging.exception("Saving diary entry failed")
            await context.reply(SendMessage(text="⚠️ Could not save the diary entry."))
            return
        await context.react("📝")


if __name__ == "__main__":
    settings = load_settings()

    bot = SignalBot(
        Config(
            connection_mode=settings.signal.connection_mode,
            signal_service=settings.signal.api_url,
            phone_number=settings.signal.phone_number,
            logging_level=logging.INFO,
            storage=InMemoryConfig(),
        )
    )
    diary = Diary(bot, settings)

    # Only answer the owner's number, never groups. Commands win over chat (priority).
    only_me = {"contacts": [diary.owner], "groups": False}
    bot.register(PingCommand(diary), **only_me, priority=2)
    bot.register(SlashCommandHandler(diary), **only_me, priority=2)
    bot.register(MessageHandler(diary), **only_me, priority=1)

    diary.schedule_reminder()
    diary.start_loading_state()

    bot.start()
