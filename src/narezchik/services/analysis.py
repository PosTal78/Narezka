"""Optional local transcription and scene detection adapters."""

from __future__ import annotations

from dataclasses import dataclass
import ctypes
import json
import multiprocessing
import os
from pathlib import Path
from queue import Empty
import tempfile
from typing import Callable

from narezchik.app_paths import app_paths
from .subtitles import SubtitleCue


class AnalysisError(RuntimeError):
    pass


class AnalysisCancelled(AnalysisError):
    pass


WHISPER_MODELS = {"быстрее": "base", "сбалансированно": "small", "точнее": "medium"}
_CUDA_DLL_DIRECTORIES: list[object] = []


def _is_cuda_load_error(error: Exception) -> bool:
    """Return whether CTranslate2 selected CUDA but its runtime is unavailable."""
    message = str(error).lower()
    return any(marker in message for marker in ("cuda", "cublas", "cudnn", "cufft", "curand", "nvrtc"))


def _cuda_runtime_available() -> bool:
    """CUDA device detection alone is insufficient when runtime DLLs are missing."""
    try:
        # PyInstaller keeps CUDA libraries inside torch/lib. Add that directory
        # before CTranslate2 probes the card; otherwise a portable build falls
        # back to CPU despite a usable NVIDIA GPU.
        import torch
        torch_lib = Path(torch.__file__).resolve().parent / "lib"
        if torch_lib.is_dir() and hasattr(os, "add_dll_directory"):
            handle = os.add_dll_directory(str(torch_lib))
            _CUDA_DLL_DIRECTORIES.append(handle)
            path_value = os.environ.get("PATH", "")
            if str(torch_lib).lower() not in path_value.lower().split(os.pathsep):
                os.environ["PATH"] = f"{torch_lib}{os.pathsep}{path_value}"
        import ctranslate2
        if ctranslate2.get_cuda_device_count() < 1:
            return False
        ctypes.WinDLL("cublas64_12.dll")
        return True
    except (ImportError, OSError, RuntimeError):
        return False


def _create_whisper_model(model_name: str, model_root: Path,
                          progress: Callable[[int, int, str], None] | None):
    """Prefer GPU, but keep transcription usable on partially configured CUDA PCs."""
    options = {"compute_type": "int8", "download_root": str(model_root)}
    if progress:
        progress(0, 0, "Загружаю модель Whisper. При первом запуске она скачается в папку data/models/whisper…")
    device = "cuda" if _cuda_runtime_available() else "cpu"
    if device == "cpu" and progress:
        progress(0, 0, "GPU CUDA недоступен, расшифровка будет выполнена на CPU. Это нормально, но займёт больше времени…")
    try:
        from faster_whisper import WhisperModel
        model = WhisperModel(model_name, device=device, **options)
        if device == "cuda" and progress:
            try:
                import torch
                label = torch.cuda.get_device_name(0)
            except Exception:
                label = "GPU CUDA"
            progress(0, 0, f"Расшифровка использует {label}.")
        return model
    except Exception as error:
        if device != "cuda" or not _is_cuda_load_error(error):
            raise
        if progress:
            progress(0, 0, "Ускорение NVIDIA недоступно, продолжаю на CPU. Это нормально, но займёт больше времени…")
        from faster_whisper import WhisperModel
        return WhisperModel(model_name, device="cpu", **options)


def transcribe(path: Path, *, model_choice: str = "сбалансированно", language: str | None = None,
               cancelled: Callable[[], bool] | None = None,
               progress: Callable[[int, int, str], None] | None = None) -> tuple[list[SubtitleCue], str]:
    try:
        from faster_whisper import WhisperModel
    except ImportError as error:
        raise AnalysisError("Локальная расшифровка не установлена. Установите зависимости этапа видео.") from error
    model_name = WHISPER_MODELS.get(model_choice, model_choice)
    try:
        if cancelled and cancelled():
            raise AnalysisCancelled("Расшифровка отменена.")
        model_root = app_paths().whisper_models
        model_root.mkdir(parents=True, exist_ok=True)
        # `download_root` prevents Hugging Face from silently using AppData on C:.
        model = _create_whisper_model(model_name, model_root, progress)
        if cancelled and cancelled():
            raise AnalysisCancelled("Расшифровка отменена.")
        if progress:
            progress(0, 0, "Распознаю речь локально…")
        segments, info = model.transcribe(str(path), language=language or None, vad_filter=True)
        cues: list[SubtitleCue] = []
        for index, segment in enumerate(segments, 1):
            if cancelled and cancelled():
                raise AnalysisCancelled("Расшифровка отменена.")
            text = segment.text.strip()
            if text:
                cues.append(SubtitleCue(float(segment.start), float(segment.end), text))
            if progress:
                progress(index, 0, text)
        if not cues:
            raise AnalysisError("Whisper не нашёл распознаваемой речи.")
        return cues, getattr(info, "language", language or "unknown")
    except AnalysisCancelled:
        raise
    except Exception as error:
        raise AnalysisError(f"Не удалось расшифровать фильм: {error}") from error


@dataclass(frozen=True, slots=True)
class Scene:
    scene_id: int
    start: float
    end: float
    source_index: int = 0


def _detect_scene_values(path: str, result_queue: multiprocessing.queues.Queue) -> None:
    try:
        from scenedetect import ContentDetector, detect
    except ImportError as error:
        result_queue.put(("error", "Поиск сцен не установлен. Установите зависимости этапа видео."))
        return
    try:
        detected = detect(path, ContentDetector())
    except Exception as error:
        result_queue.put(("error", f"Не удалось определить сцены: {error}"))
        return
    result_queue.put(("ok", [(start.get_seconds(), end.get_seconds()) for start, end in detected]))


def detect_scenes(path: Path, duration: float, *, cancelled: Callable[[], bool] | None = None) -> list[Scene]:
    if cancelled and cancelled():
        raise AnalysisCancelled("Поиск сцен отменён.")
    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue()
    process = context.Process(target=_detect_scene_values, args=(str(path), result_queue))
    process.start()
    try:
        status = payload = None
        while process.is_alive():
            if cancelled and cancelled():
                process.terminate()
                process.join()
                raise AnalysisCancelled("Поиск сцен отменён.")
            try:
                status, payload = result_queue.get(timeout=0.1)
                break
            except Empty:
                continue
        if status is None:
            try:
                status, payload = result_queue.get_nowait()
            except Empty as error:
                raise AnalysisError("Поиск сцен завершился без результата.") from error
        process.join()
    finally:
        result_queue.close()
    if status != "ok":
        raise AnalysisError(payload)
    values = [Scene(index, start, end) for index, (start, end) in enumerate(payload, 1)]
    return values or [Scene(1, 0.0, duration)]


def write_scenes(scenes: list[Scene], analysis_dir: Path, source_fingerprint: str) -> Path:
    analysis_dir.mkdir(parents=True, exist_ok=True)
    destination = analysis_dir / "scenes.json"
    payload = json.dumps({"source_fingerprint": source_fingerprint, "detector": "content-default",
                          "scenes": [{"scene_id": item.scene_id, "start": item.start, "end": item.end,
                                      "source_index": item.source_index}
                                     for item in scenes]}, ensure_ascii=False, indent=2) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(prefix=".scenes-", suffix=".tmp", dir=analysis_dir, text=True)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise
    return destination
