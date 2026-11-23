"""Расшифровка голосовых и аудиофайлов на этом компьютере (whisper.cpp). Записи никуда не отправляются.

Модель (≈ 550 МБ) скачивается один раз в data/models и сверяется по контрольной сумме:
    python -m app.integrations.speech
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sys
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path, PurePath

import httpx

from app.config import PROJECT_ROOT, settings
from app.integrations.speech_worker import PREFIX
from app.services.errors import ServiceError

log = logging.getLogger(__name__)

MODELS = PROJECT_ROOT / "data" / "models"
HF = "https://huggingface.co"
FILES = {
    "ggml-large-v3-turbo-q5_0.bin": (
        f"{HF}/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin",
        "394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2"),
    "ggml-silero-v5.1.2.bin": (
        f"{HF}/ggml-org/whisper-vad/resolve/main/ggml-silero-v5.1.2.bin",
        "29940d98d42b91fbd05ce489f3ecf7c72f0a42f027e4875919a28fb4c04ea2cf"),
}
MODEL, VAD = FILES

MAX_BYTES = 20 * 1024 * 1024   # больше боту Telegram не отдаёт
TIMEOUT = 3600
PARAGRAPH = 45                 # секунд в абзаце длинной расшифровки
EXTENSIONS = {".ogg", ".oga", ".opus", ".mp3", ".m4a", ".aac", ".wav", ".flac", ".amr", ".wma", ".aiff", ".aif",
              ".caf", ".mp4", ".mov", ".webm", ".mkv", ".3gp"}

Progress = Callable[[float], Awaitable[None]]
_one = asyncio.Lock()          # по одной записи за раз: модель занимает весь графический процессор


class SpeechError(ServiceError):
    """Ошибка расшифровки. Текст понятен человеку."""


@dataclass
class Transcript:
    duration: float
    segments: list[tuple[float, float, str]]

    @property
    def text(self) -> str:
        return " ".join(s[2] for s in self.segments)

    def timed(self) -> str:
        """Абзацы с отметками времени — для длинных записей."""
        out: list[tuple[float, list[str]]] = []
        for start, _, text in self.segments:
            if not out or start - out[-1][0] >= PARAGRAPH:
                out.append((start, []))
            out[-1][1].append(text)
        return "\n".join(f"[{fmt_time(t)}] {' '.join(parts)}" for t, parts in out)


def fmt_time(sec: float) -> str:
    s = int(sec)
    return f"{s // 3600}:{s % 3600 // 60:02}:{s % 60:02}" if s >= 3600 else f"{s // 60}:{s % 60:02}"


def is_audio(name: str | None, mime: str | None) -> bool:
    return (mime or "").startswith(("audio/", "video/")) or PurePath(name or "").suffix.lower() in EXTENSIONS


def models_ready() -> bool:
    return all((MODELS / f).exists() for f in FILES)


async def ensure_models() -> None:
    MODELS.mkdir(parents=True, exist_ok=True)
    for name, (url, digest) in FILES.items():
        path = MODELS / name
        if path.exists():
            continue
        part = path.with_suffix(".part")
        h = hashlib.sha256()
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=60) as http, http.stream("GET", url) as r:
                r.raise_for_status()
                with part.open("wb") as f:
                    async for chunk in r.aiter_bytes(1 << 20):
                        h.update(chunk)
                        f.write(chunk)
        except httpx.HTTPError as e:
            part.unlink(missing_ok=True)
            raise SpeechError("Не получилось скачать модель распознавания — проверьте интернет и повторите") from e
        if h.hexdigest() != digest:  # скачанное должно быть ровно тем, что проверяли
            part.unlink(missing_ok=True)
            raise SpeechError("Модель распознавания скачалась с ошибкой (не сошлась контрольная сумма)")
        part.replace(path)


async def _run(path: Path, on_progress: Progress | None) -> Transcript:
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "app.integrations.speech_worker", str(path), str(MODELS / MODEL), str(MODELS / VAD),
        settings.speech_language, cwd=PROJECT_ROOT, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        limit=16 * 1024 * 1024)
    duration, segments = 0.0, None
    try:
        async for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith(PREFIX):
                continue
            data = json.loads(line[len(PREFIX):])
            if "error" in data:
                raise SpeechError(data["error"])
            duration = data.get("duration", duration)
            if "segments" in data:
                segments = [tuple(s) for s in data["segments"]]
            if "progress" in data and on_progress:
                await on_progress(data["progress"])
        code = await proc.wait()
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    if segments is None:
        log.error("speech worker exited with code %s", code)
        raise SpeechError("Расшифровка сорвалась. Пришлите запись ещё раз")
    if not segments:
        raise SpeechError("Не расслышал речи в записи")
    return Transcript(duration, segments)


async def transcribe(data: bytes, name: str, on_progress: Progress | None = None) -> Transcript:
    if len(data) > MAX_BYTES:
        raise SpeechError(f"Запись больше {MAX_BYTES // 1024 // 1024} МБ — пришлите по частям")
    async with _one:
        await ensure_models()
        with tempfile.TemporaryDirectory(prefix="cg-speech-") as tmp:  # запись удаляется сразу
            path = Path(tmp) / ("audio" + PurePath(name or "").suffix.lower()[:8])
            path.write_bytes(data)
            try:
                async with asyncio.timeout(TIMEOUT):
                    return await _run(path, on_progress)
            except TimeoutError:
                raise SpeechError("Расшифровка заняла слишком много времени — пришлите запись по частям") from None


if __name__ == "__main__":
    asyncio.run(ensure_models())
    print("Модели распознавания готовы:", MODELS)
