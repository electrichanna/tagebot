"""Configuration loaded from environment variables and an optional ``.env`` file.

Each setting maps to an environment variable named
``TAGEBOT_<SECTION>__<KEY>``, e.g. ``TAGEBOT_SMB__USERNAME``. Any variable can
alternatively be read from a file by setting ``<NAME>_FILE`` to the file path,
e.g. ``TAGEBOT_SMB__PASSWORD_FILE=/run/secrets/smb_password``; this is the
intended mechanism for Docker secrets. See ``.env.example`` for the full list.

Attributes:
    ENV_PREFIX: Prefix shared by all environment variables.
    DEFAULT_LLM_MODEL: Chat model used when none is configured.
"""

import os
import re
import time
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Field, SecretStr, ValidationError, field_validator, model_validator
from dotenv import load_dotenv
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

ENV_PREFIX = "TAGEBOT_"
DEFAULT_LLM_MODEL = "ministral-3:8b"


def _api_base(url: str) -> str:
    """Normalises a URL to an OpenAI-compatible API base URL.

    Args:
        url: Server URL, with or without a trailing ``/v1``.

    Returns:
        The URL without trailing slashes and ending in ``/v1``.
    """
    url = url.rstrip("/")
    return url if url.endswith("/v1") else url + "/v1"


class SignalSettings(BaseModel):
    """Connection settings for the signal-cli REST API (``TAGEBOT_SIGNAL__*``).

    Attributes:
        phone_number: Phone number of the account the bot sends from.
        owner_number: The only phone number the bot accepts messages from and
            sends reminders to.
        api_url: Host and port of the signal-cli REST API.
        connection_mode: Protocol selection for the API connection.
    """

    phone_number: str
    owner_number: str
    api_url: str = "localhost:8080"
    connection_mode: Literal["https_only", "http_only", "auto"] = "auto"


class LLMSettings(BaseModel):
    """Settings for the OpenAI-compatible chat endpoint (``TAGEBOT_LLM__*``).

    Attributes:
        url: Server URL; ``/v1`` is appended if missing.
        model: Default model name; can be overridden at runtime.
        temperature: Default chat sampling temperature (0-2); can be
            overridden at runtime.
        api_key: API key for the endpoint.
        add_system_prompt: Text appended to the built-in chat system prompt.
    """

    url: str
    model: str = DEFAULT_LLM_MODEL
    temperature: float = Field(0.0, ge=0, le=2)
    api_key: SecretStr = SecretStr("not-needed")
    add_system_prompt: str = ""

    @property
    def base_url(self) -> str:
        """str: ``url`` normalised to an API base URL ending in ``/v1``."""
        return _api_base(self.url)


class ASRSettings(BaseModel):
    """Speech-recognition settings (``TAGEBOT_ASR__*``).

    Attributes:
        backend: Transcription backend to use. ``"speaches"`` supports a
            per-request vocabulary prompt; ``"wyoming"`` only uses the prompt
            configured at server start.
        language: Language code for transcription, or ``None`` for automatic
            detection.
    """

    backend: Literal["speaches", "wyoming"] = "speaches"
    language: str | None = None

    @field_validator("language")
    @classmethod
    def _empty_is_auto(cls, value: str | None) -> str | None:
        """Maps empty or whitespace-only values to ``None`` (auto-detect).

        Args:
            value: Raw configured language.

        Returns:
            The stripped language code, or ``None``.
        """
        return value.strip() or None if value else None


class SpeachesSettings(BaseModel):
    """Settings for the OpenAI-compatible transcription endpoint (``TAGEBOT_SPEACHES__*``).

    Attributes:
        url: Server URL; ``/v1`` is appended if missing. Required when
            ``ASRSettings.backend`` is ``"speaches"``.
        model: Transcription model name.
        api_key: API key for the endpoint.
    """

    url: str | None = None
    model: str = "Systran/faster-whisper-large-v3"
    api_key: SecretStr = SecretStr("not-needed")

    @property
    def base_url(self) -> str:
        """str: ``url`` normalised to an API base URL ending in ``/v1``."""
        return _api_base(self.url or "")


class WyomingSettings(BaseModel):
    """Settings for the Wyoming ASR server (``TAGEBOT_WYOMING__*``).

    Attributes:
        host: Server hostname. Required when ``ASRSettings.backend`` is
            ``"wyoming"``.
        port: Server TCP port.
    """

    host: str | None = None
    port: int = 10300


class DiarySettings(BaseModel):
    """Defaults for the daily check-in (``TAGEBOT_DIARY__*``).

    Both values can be overridden at runtime via chat commands.

    Attributes:
        reminder_time: Local time of the daily check-in, normalised to
            ``HH:MM``.
        parameters: Names of the rated parameters. Accepts a comma-separated
            string; names are stripped and lower-cased.
    """

    reminder_time: str = "21:00"
    parameters: Annotated[list[str], NoDecode] = ["mood", "pain", "energy", "sleep"]

    @field_validator("reminder_time")
    @classmethod
    def _check_time(cls, value: str) -> str:
        """Validates and normalises the reminder time.

        Args:
            value: Time as ``HH:MM`` or ``HHMM``.

        Returns:
            The time formatted as zero-padded ``HH:MM``.

        Raises:
            ValueError: If the value is not a valid time of day.
        """
        match = re.fullmatch(r"(\d{1,2}):?(\d{2})", value.strip())
        if not match or int(match[1]) > 23 or int(match[2]) > 59:
            raise ValueError("must be HH:MM, e.g. 21:00")
        return f"{int(match[1]):02d}:{match[2]}"

    @field_validator("parameters", mode="before")
    @classmethod
    def _split(cls, value):
        """Parses the parameter list.

        Args:
            value: Comma-separated string or iterable of names.

        Returns:
            Non-empty names, stripped and lower-cased.
        """
        if isinstance(value, str):
            value = value.split(",")
        return [p.strip().lower() for p in value if p.strip()]


class SMBSettings(BaseModel):
    """Settings for the SMB share used as storage (``TAGEBOT_SMB__*``).

    Attributes:
        server: Hostname or IP address of the SMB server.
        share: Name of the share.
        username: Account name for authentication.
        password: Password for ``username``.
        base_dir: Directory inside the share where all data is stored. Both
            ``/`` and ``\\`` are accepted as separators.
        port: TCP port of the SMB service.
    """

    server: str
    share: str
    username: str
    password: SecretStr
    base_dir: str = ""
    port: int = 445


class Settings(BaseSettings):
    """Root configuration object, populated from ``TAGEBOT_*`` environment variables.

    Attributes:
        signal: Signal API connection settings.
        llm: Chat model settings.
        asr: Speech-recognition backend selection.
        speaches: OpenAI-compatible transcription settings.
        wyoming: Wyoming ASR server settings.
        diary: Check-in defaults.
        smb: Storage share settings.
    """

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_nested_delimiter="__",
        extra="ignore",
    )

    signal: SignalSettings
    llm: LLMSettings
    asr: ASRSettings = ASRSettings()
    speaches: SpeachesSettings = SpeachesSettings()
    wyoming: WyomingSettings = WyomingSettings()
    diary: DiarySettings = DiarySettings()
    smb: SMBSettings

    @model_validator(mode="after")
    def _check_asr_backend(self) -> "Settings":
        """Ensures the settings required by the selected ASR backend are present.

        Returns:
            The validated settings instance.

        Raises:
            ValueError: If the server address for the selected backend is
                missing.
        """
        if self.asr.backend == "speaches" and not self.speaches.url:
            raise ValueError(f"{ENV_PREFIX}SPEACHES__URL is required with {ENV_PREFIX}ASR__BACKEND=speaches")
        if self.asr.backend == "wyoming" and not self.wyoming.host:
            raise ValueError(f"{ENV_PREFIX}WYOMING__HOST is required with {ENV_PREFIX}ASR__BACKEND=wyoming")
        return self


def _read_file_variables() -> None:
    """Resolves ``TAGEBOT_<NAME>_FILE`` variables into ``TAGEBOT_<NAME>``.

    For each such variable, the referenced file is read and its content is
    assigned to the variable without the ``_FILE`` suffix, unless that
    variable is already set. Only a trailing line break is removed, so other
    whitespace in secrets is preserved.

    Raises:
        SystemExit: If a referenced file cannot be read.
    """
    for name, path in list(os.environ.items()):
        if name.startswith(ENV_PREFIX) and name.endswith("_FILE"):
            try:
                value = Path(path).read_text(encoding="utf-8").rstrip("\r\n")
            except OSError as err:
                raise SystemExit(f"{name}: cannot read {path} ({err.strerror})") from None
            os.environ.setdefault(name.removesuffix("_FILE"), value)


def load_settings() -> Settings:
    """Loads and validates the configuration.

    Reads ``.env`` from the working directory (existing environment variables
    take precedence), applies the ``TZ`` variable to the process, resolves
    ``*_FILE`` variables and builds a :class:`Settings` instance.

    Returns:
        The validated settings.

    Raises:
        SystemExit: If the configuration is invalid. The message lists each
            missing or invalid variable by its environment variable name.
    """
    # .env first (real environment variables win), so <NAME>_FILE also works in .env
    load_dotenv(".env", override=False)
    if hasattr(time, "tzset"):  # not on Windows
        time.tzset()  # apply TZ from .env to local times
    _read_file_variables()
    try:
        return Settings()
    except ValidationError as err:
        lines = []
        for error in err.errors():
            loc = [str(part) for part in error["loc"]]
            if not loc:
                lines.append(f"  {error['msg']}")
            elif len(loc) == 1 and error["type"] == "missing":
                # a whole section is missing: list its required variables
                section = Settings.model_fields[loc[0]].annotation
                names = [n for n, f in section.model_fields.items() if f.is_required()]
                lines += [f"  {ENV_PREFIX}{loc[0].upper()}__{n.upper()}: Field required" for n in names]
            else:
                lines.append(f"  {ENV_PREFIX}{'__'.join(loc).upper()}: {error['msg']}")
        raise SystemExit("Invalid configuration (see .env.example):\n" + "\n".join(lines)) from None
