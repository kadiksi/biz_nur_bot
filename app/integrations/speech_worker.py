"""Отдельный процесс распознавания: python -m app.integrations.speech_worker ФАЙЛ МОДЕЛЬ VAD ЯЗЫК

Отдельный — потому что файл пришёл извне и разбирается нативным кодом: сбой не уронит
сайт и бота, а память (около 1 ГБ) освобождается сразу. Ответ — строки «@@{json}» в stdout.
"""
from __future__ import annotations

import json
import os
import sys

PREFIX = "@@"
RATE = 16_000
MAX_SECONDS = 2 * 3600


def emit(**data) -> None:
    sys.stdout.write(PREFIX + json.dumps(data) + "\n")
    sys.stdout.flush()


def decode(path: str):
    """Любой формат (ogg, mp3, m4a, wav, видео) → моно 16 кГц."""
    import av
    import numpy as np

    out, total = [], 0
    with av.open(path) as box:
        if not box.streams.audio:
            return None
        resampler = av.AudioResampler(format="s16", layout="mono", rate=RATE)
        for frame in box.decode(audio=0):
            for r in resampler.resample(frame):
                chunk = r.to_ndarray().reshape(-1)
                total += len(chunk)
                if total > MAX_SECONDS * RATE:
                    raise OverflowError
                out.append(chunk)
        for r in resampler.resample(None):
            out.append(r.to_ndarray().reshape(-1))
    return np.concatenate(out).astype(np.float32) / 32768 if out else None


def main(path: str, model: str, vad: str, language: str) -> int:
    try:
        audio = decode(path)
    except OverflowError:
        emit(error=f"Запись длиннее {MAX_SECONDS // 3600} часов — пришлите по частям")
        return 0
    except Exception as e:  # noqa: BLE001 — битый файл или не аудио
        print(repr(e), file=sys.stderr)
        emit(error="Не получилось прочитать запись: файл повреждён или это не аудио")
        return 0
    if audio is None or len(audio) == 0:
        emit(error="В файле нет звука")
        return 0
    duration = len(audio) / RATE
    emit(duration=duration)

    from pywhispercpp.model import Model

    whisper = Model(model, n_threads=max(2, min(4, (os.cpu_count() or 4) // 2)), print_progress=False,
                    print_realtime=False, redirect_whispercpp_logs_to=None)
    # vad — распознаём только участки с речью: на тишине модель выдумывает фразы
    segments = whisper.transcribe(
        audio, language=language, vad=True, vad_model_path=vad, no_context=True,
        new_segment_callback=lambda s: emit(progress=min(1.0, s.t1 / 100 / duration)))
    emit(segments=[[s.t0 / 100, s.t1 / 100, s.text.strip()] for s in segments if s.text.strip()])
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:5]))
