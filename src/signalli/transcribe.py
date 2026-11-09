import asyncio
import base64

from wyoming.client import AsyncTcpClient
from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioStart, AudioChunk, AudioStop

RATE, WIDTH, CHANNELS = 16000, 2, 1


async def to_pcm(data: bytes) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-loglevel", "error", "-i", "pipe:0",
        "-f", "s16le", "-ar", str(RATE), "-ac", str(CHANNELS), "pipe:1",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
    )
    pcm, _ = await proc.communicate(data)
    return pcm


async def transcribe_bytes(audio: bytes, host="192.168.1.50", port=10300) -> str:
    pcm = await to_pcm(audio)
    async with AsyncTcpClient(host, port) as client:
        await client.write_event(Transcribe(language="de").event())
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