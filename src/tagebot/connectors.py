"""Clients for the external services used by the bot.

Provides thin wrappers around:

* OpenAI-compatible chat completion endpoints (e.g. Ollama) for rating
  extraction and free-form chat.
* SMB file shares for persistent storage of JSON and binary files.
* Speech-to-text backends: a Wyoming ASR server or an OpenAI-compatible
  transcription endpoint (e.g. Speaches).

Audio input of any format is normalised with ``ffmpeg`` to raw PCM using the
module-level constants ``RATE``, ``WIDTH`` and ``CHANNELS``.
"""

import io
import json
import errno
import asyncio
import wave
import ntpath

import smbclient

from wyoming.client import AsyncTcpClient
from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioStart, AudioChunk, AudioStop

from openai import AsyncOpenAI, OpenAI

from prompts import PARSE_SYSTEM_PROMPT, CHAT_SYSTEM_PROMPT



RATE, WIDTH, CHANNELS = 16000, 2, 1



def ollama_parse_response(text: str, parameters: list[str], timeout: int = 120, url: str = "http://127.0.0.1:11434/v1", api_key:str = "not-needed", model:str = None) -> dict:
    """Extracts integer ratings (1-10) for a set of parameters from free text.

    The request uses a JSON schema response format so the model returns one
    key per parameter. Values outside the 1-10 range or of a non-integer type
    are discarded.

    Args:
        text: Free-form input text, e.g. a message or speech transcript.
        parameters: Names of the parameters to extract ratings for.
        timeout: Request timeout in seconds.
        url: Base URL of the OpenAI-compatible API (including ``/v1``).
        api_key: API key for the endpoint.
        model: Name of the model to query. Required.

    Returns:
        A dict mapping every name in ``parameters`` to an ``int`` between 1
        and 10, or ``None`` if no valid rating was found for it.

    Raises:
        Exception: If ``model`` is not specified.
        openai.OpenAIError: If the API request fails.
        json.JSONDecodeError: If the response content is not valid JSON.
    """

    if model is None:
        raise Exception("You need to specify a model")

    client = OpenAI(
        base_url=url,
        api_key=api_key,
        timeout=timeout,
    )

    schema = {
        "type": "object",
        "properties": {p: {"type": ["integer", "null"], "minimum": 1, "maximum": 10} for p in parameters},
        "required": parameters,
    }

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": PARSE_SYSTEM_PROMPT.format(parameters=", ".join(parameters))},
            {"role": "user", "content": text}
            ],
        response_format={"type": "json_schema", "json_schema": {"name": "ratings", "schema": schema}},
        temperature=0,
    )

    raw = json.loads(response.choices[0].message.content)
    ratings = {}
    for p in parameters:
        value = raw.get(p)
        ratings[p] = value if isinstance(value, int) and 1 <= value <= 10 else None
    return ratings




def ollama_response_message(text: str, timeout: int = 120, url: str = "http://127.0.0.1:11434/v1", api_key:str = "not-needed", model:str = None, history: list[dict] | None = None, system_prompt: str = CHAT_SYSTEM_PROMPT, temperature: float = 0) -> str:
    """Generates a chat completion for a user message.

    Args:
        text: The new user message.
        timeout: Request timeout in seconds.
        url: Base URL of the OpenAI-compatible API (including ``/v1``).
        api_key: API key for the endpoint.
        model: Name of the model to query. Required.
        history: Previous conversation turns as ``{"role", "content"}``
            dicts, inserted between the system prompt and ``text``.
        system_prompt: System prompt sent as the first message.
        temperature: Sampling temperature passed to the model.

    Returns:
        The content of the first completion choice.

    Raises:
        Exception: If ``model`` is not specified.
        openai.OpenAIError: If the API request fails.
    """

    if model is None:
        raise Exception("You need to specify a model")

    client = OpenAI(
        base_url=url,
        api_key=api_key,
        timeout=timeout,
    )

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            *(history or []),
            {"role": "user", "content": text}
            ],
        temperature=temperature,
    )

    return response.choices[0].message.content


def ollama_list_models(timeout: int = 30, url: str = "http://127.0.0.1:11434/v1", api_key: str = "not-needed") -> list[str]:
    """Lists the models available on an OpenAI-compatible endpoint.

    Args:
        timeout: Request timeout in seconds.
        url: Base URL of the OpenAI-compatible API (including ``/v1``).
        api_key: API key for the endpoint.

    Returns:
        Model IDs, sorted alphabetically.

    Raises:
        openai.OpenAIError: If the API request fails.
    """
    client = OpenAI(base_url=url, api_key=api_key, timeout=timeout)
    return sorted(m.id for m in client.models.list())


def smb_register(server: str, username: str, password: str, port: int = 445) -> None:
    """Registers an authenticated SMB session for subsequent ``smbclient`` calls.

    Calling this again for an existing session re-establishes the connection
    if it was dropped.

    Args:
        server: Hostname or IP address of the SMB server.
        username: Account name used for authentication.
        password: Password for ``username``.
        port: TCP port of the SMB service.
    """
    smbclient.register_session(server, username=username, password=password, port=port)


def smb_write_bytes(path: str, data: bytes) -> None:
    """Writes binary data to an SMB path, creating parent directories as needed.

    Existing files are overwritten.

    Args:
        path: UNC path of the target file, e.g. ``\\\\server\\share\\dir\\file``.
        data: Content to write.

    Raises:
        OSError: If the directory cannot be created or the file cannot be written.
    """
    smbclient.makedirs(ntpath.dirname(path), exist_ok=True)
    with smbclient.open_file(path, mode="wb") as f:
        f.write(data)


def smb_write_json(path: str, obj) -> None:
    """Serialises an object as UTF-8 JSON and writes it to an SMB path.

    The output is indented and keeps non-ASCII characters unescaped.

    Args:
        path: UNC path of the target file.
        obj: JSON-serialisable object.

    Raises:
        TypeError: If ``obj`` is not JSON-serialisable.
        OSError: If the file cannot be written.
    """
    smb_write_bytes(path, json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8"))


def smb_read_json(path: str):
    """Reads and parses a UTF-8 JSON file from an SMB path.

    Args:
        path: UNC path of the source file.

    Returns:
        The parsed JSON value, or ``None`` if the file or one of its parent
        directories does not exist.

    Raises:
        OSError: For any SMB error other than a missing file.
        json.JSONDecodeError: If the file content is not valid JSON.
    """
    try:
        with smbclient.open_file(path, mode="rb") as f:
            return json.loads(f.read().decode("utf-8"))
    except OSError as err:
        # smbprotocol raises SMBOSError (errno ENOENT), not FileNotFoundError, for missing files/folders
        if err.errno == errno.ENOENT:
            return None
        raise


async def to_pcm(data: bytes) -> bytes:
    """Converts audio of any ffmpeg-supported format to raw PCM.

    The output is signed 16-bit little-endian PCM at ``RATE`` Hz with
    ``CHANNELS`` channels. Requires ``ffmpeg`` on the ``PATH``.

    Args:
        data: Encoded audio data (e.g. AAC, Opus, MP3).

    Returns:
        Raw PCM samples. Empty if ffmpeg could not decode the input.
    """
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-loglevel", "error", "-i", "pipe:0",
        "-f", "s16le", "-ar", str(RATE), "-ac", str(CHANNELS), "pipe:1",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
    )
    pcm, _ = await proc.communicate(data)
    return pcm


async def transcribe_bytes(audio: bytes, host="localhost", port=10300, language: str | None = None) -> str:
    """Transcribes audio using a Wyoming ASR server.

    The audio is converted to PCM and streamed to the server in chunks of
    2048 samples, followed by a stop event. The first ``Transcript`` event
    received is returned.

    Args:
        audio: Encoded audio data in any ffmpeg-supported format.
        host: Hostname of the Wyoming server.
        port: TCP port of the Wyoming server.
        language: Language code (e.g. ``"de"``). ``None`` leaves language
            selection to the server.

    Returns:
        The transcribed text.

    Raises:
        RuntimeError: If the connection closes without a transcript.
        OSError: If the server cannot be reached.
    """
    # language=None lets the server decide; start faster-whisper with --language auto for de/en detection
    pcm = await to_pcm(audio)
    async with AsyncTcpClient(host, port) as client:
        await client.write_event(Transcribe(language=language).event())
        await client.write_event(AudioStart(rate=RATE, width=WIDTH, channels=CHANNELS).event())
        step = 2048 * WIDTH * CHANNELS
        for i in range(0, len(pcm), step):
            await client.write_event(
                AudioChunk(rate=RATE, width=WIDTH, channels=CHANNELS, audio=pcm[i:i + step]).event()
            )
        await client.write_event(AudioStop().event())
        while (event := await client.read_event()) is not None:
            if Transcript.is_type(event.type):
                return Transcript.from_event(event).text
    raise RuntimeError("No transcript received")


async def transcribe_openai(
    audio: bytes,
    url: str,
    model: str,
    api_key: str = "not-needed",
    language: str | None = None,
    prompt: str | None = None,
    timeout: int = 300,
) -> str:
    """Transcribes audio using an OpenAI-compatible transcription endpoint.

    The audio is converted to PCM, wrapped in a WAV container and uploaded
    to ``/audio/transcriptions`` (e.g. on a Speaches server).

    Args:
        audio: Encoded audio data in any ffmpeg-supported format.
        url: Base URL of the OpenAI-compatible API (including ``/v1``).
        model: Name of the transcription model.
        api_key: API key for the endpoint.
        language: Language code (e.g. ``"de"``). ``None`` enables automatic
            language detection.
        prompt: Optional text used to bias the recognised vocabulary, e.g. a
            list of names and domain-specific terms.
        timeout: Request timeout in seconds.

    Returns:
        The transcribed text.

    Raises:
        openai.OpenAIError: If the API request fails.
    """
    pcm = await to_pcm(audio)
    wav = io.BytesIO()
    with wave.open(wav, "wb") as f:
        f.setnchannels(CHANNELS)
        f.setsampwidth(WIDTH)
        f.setframerate(RATE)
        f.writeframes(pcm)

    options = {}
    if language:
        options["language"] = language
    if prompt:
        options["prompt"] = prompt
    async with AsyncOpenAI(base_url=url, api_key=api_key, timeout=timeout) as client:
        result = await client.audio.transcriptions.create(
            model=model, file=("audio.wav", wav.getvalue(), "audio/wav"), **options
        )
    return result.text
