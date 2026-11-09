import json
import errno
import asyncio
import wave
import ntpath

import smbclient

from wyoming.client import AsyncTcpClient
from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioStart, AudioChunk, AudioStop

from openai import OpenAI

from prompts import PARSE_SYSTEM_PROMPT, CHAT_SYSTEM_PROMPT


OLLAMA_URL = "http://192.168.0.170:11434"   # your server's IP
OLLAMA_MODEL = "ministral-3:8b"

RATE, WIDTH, CHANNELS = 16000, 2, 1



def ollama_parse_response(text: str, parameters: list[str], timeout: int = 120, url: str = "http://127.0.0.1:11434/v1", api_key:str = "not-needed", model:str = None) -> dict:
    """Extract a 1-10 rating for each parameter from free text. Unmentioned ones are None."""

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




def ollama_response_message(text: str, timeout: int = 120, url: str = "http://127.0.0.1:11434/v1", api_key:str = "not-needed", model:str = None, history: list[dict] | None = None, system_prompt: str = CHAT_SYSTEM_PROMPT) -> str:
    """Normal chat answer. `history` is a list of earlier {"role", "content"} messages."""

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
    )

    return response.choices[0].message.content


def smb_register(server: str, username: str, password: str, port: int = 445) -> None:
    smbclient.register_session(server, username=username, password=password, port=port)


def smb_write_bytes(path: str, data: bytes) -> None:
    """Write `data` to a UNC path like \\\\server\\share\\dir\\file, creating folders."""
    smbclient.makedirs(ntpath.dirname(path), exist_ok=True)
    with smbclient.open_file(path, mode="wb") as f:
        f.write(data)


def smb_write_json(path: str, obj) -> None:
    smb_write_bytes(path, json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8"))


def smb_read_json(path: str):
    """Return the parsed JSON at `path`, or None if the file does not exist."""
    try:
        with smbclient.open_file(path, mode="rb") as f:
            return json.loads(f.read().decode("utf-8"))
    except OSError as err:
        # smbprotocol raises SMBOSError (errno ENOENT), not FileNotFoundError, for missing files/folders
        if err.errno == errno.ENOENT:
            return None
        raise


async def to_pcm(data: bytes) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-loglevel", "error", "-i", "pipe:0",
        "-f", "s16le", "-ar", str(RATE), "-ac", str(CHANNELS), "pipe:1",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
    )
    pcm, _ = await proc.communicate(data)
    return pcm


async def transcribe_bytes(audio: bytes, host="localhost", port=10300, language: str | None = None) -> str:
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