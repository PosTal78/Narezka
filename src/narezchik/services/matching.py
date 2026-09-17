"""Local video index and deterministic candidate selection for stage three.

The model adapter is deliberately optional: imports and the first model download only
happen when the user starts analysis, never while opening a project.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
from pathlib import PurePath
import re
import shutil
import subprocess
import tempfile
import time
from typing import Callable, Iterable
import uuid

from narezchik.models import Segment, SegmentMatch, VideoFragment
from .analysis import AnalysisCancelled, AnalysisError, Scene
from .media import media_tool
from .subtitles import SubtitleCue


INDEX_MODEL_VERSION = "blip-base+multilingual-clip-v4-strict"
PARTIAL_INDEX_FILENAME = "video_index.partial.json"
MATCH_THRESHOLD = 0.55
AUTO_ACCEPT_SCORE = 0.62
AUTO_ACCEPT_MARGIN = 0.06
_WORD = re.compile(r"[\w'-]+", re.UNICODE)


def _validate_artifact_path(value: str) -> None:
    if not isinstance(value, str) or not value:
        raise AnalysisError("Индекс содержит некорректный путь к ключевому кадру.")
    path = PurePath(value.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts:
        raise AnalysisError("Путь к ключевому кадру должен находиться внутри проекта.")


@dataclass(frozen=True, slots=True)
class IndexedScene:
    scene_id: int
    start: float
    end: float
    subtitle_text: str
    description: str
    entities: tuple[str, ...]
    thumbnail_path: str | None
    embedding: tuple[float, ...] = ()
    frame_descriptions: tuple[str, ...] = ()
    frame_embeddings: tuple[tuple[float, ...], ...] = ()
    frame_thumbnail_paths: tuple[str, ...] = ()
    source_index: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.scene_id, int) or isinstance(self.scene_id, bool) or self.scene_id < 1:
            raise AnalysisError("Индекс содержит некорректный ID сцены.")
        if (not all(isinstance(value, (int, float)) and not isinstance(value, bool)
                    and math.isfinite(value) for value in (self.start, self.end))
                or self.start < 0 or self.end <= self.start):
            raise AnalysisError("Индекс содержит некорректные границы сцены.")
        for value in (self.subtitle_text, self.description):
            if not isinstance(value, str):
                raise AnalysisError("Индекс содержит некорректное описание сцены.")
        if any(not isinstance(value, str) for value in self.entities + self.frame_descriptions):
            raise AnalysisError("Индекс содержит некорректные подписи кадров.")
        for path in ((self.thumbnail_path,) if self.thumbnail_path else ()) + self.frame_thumbnail_paths:
            _validate_artifact_path(path)
        if not isinstance(self.source_index, int) or isinstance(self.source_index, bool) or self.source_index < 0:
            raise AnalysisError("Индекс содержит некорректную часть источника.")
        for embedding in (self.embedding,) + self.frame_embeddings:
            if any(not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value)
                   for value in embedding):
                raise AnalysisError("Индекс содержит некорректный embedding.")

    def to_dict(self) -> dict[str, object]:
        return {"scene_id": self.scene_id, "start": self.start, "end": self.end,
                "subtitle_text": self.subtitle_text, "description": self.description,
                "entities": list(self.entities), "thumbnail_path": self.thumbnail_path,
                "embedding": list(self.embedding), "frame_descriptions": list(self.frame_descriptions),
                "frame_embeddings": [list(value) for value in self.frame_embeddings],
                "frame_thumbnail_paths": list(self.frame_thumbnail_paths), "source_index": self.source_index}

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "IndexedScene":
        if not isinstance(data, dict):
            raise AnalysisError("Индекс содержит некорректную сцену.")
        return cls(data["scene_id"], data["start"], data["end"],
                   data.get("subtitle_text", ""), data.get("description", ""),
                   tuple(data.get("entities", [])), data.get("thumbnail_path"),
                   tuple(data.get("embedding", [])), tuple(data.get("frame_descriptions", [])),
                   tuple(tuple(value) for value in data.get("frame_embeddings", [])),
                   tuple(data.get("frame_thumbnail_paths", [])), int(data.get("source_index", 0)))


def _words(text: str) -> set[str]:
    return {word.lower() for word in _WORD.findall(text) if len(word) > 2}


def _cue_text(scene: Scene, cues: Iterable[SubtitleCue]) -> str:
    # Scene and subtitle ranges are half-open.  A cue ending exactly where the
    # next scene starts must not be copied into both scenes.
    return " ".join(cue.text for cue in cues if cue.end > scene.start and cue.start < scene.end)


def _entities(description: str) -> tuple[str, ...]:
    """Stable lightweight labels for UI/search; semantic model output remains cached."""
    return tuple(sorted(_words(description))[:12])


def local_captioner() -> Callable[[Path], str]:
    """Load the visual model once and return a captioner for one analysis run."""
    try:
        from transformers import pipeline
        from narezchik.app_paths import app_paths
    except ImportError as error:
        raise AnalysisError("Анализ изображений не установлен. Установите зависимости этапа подбора кадров.") from error
    try:
        model_root = app_paths().vision_models
        model_root.mkdir(parents=True, exist_ok=True)
        import torch
        device = 0 if torch.cuda.is_available() else -1
        captioner = pipeline("image-to-text", model="Salesforce/blip-image-captioning-base", device=device,
                             model_kwargs={"cache_dir": str(model_root)})
        def caption(image: Path) -> str:
            value = captioner(str(image), max_new_tokens=32)
            return str(value[0].get("generated_text", "")).strip()
        def batch(images: list[Path]) -> list[str]:
            nonlocal captioner, device
            try:
                values = captioner([str(image) for image in images], max_new_tokens=32,
                                   batch_size=min(6, len(images)))
            except RuntimeError as error:
                if device < 0 or "out of memory" not in str(error).lower():
                    raise
                torch.cuda.empty_cache()
                try:
                    values = [captioner(str(image), max_new_tokens=32) for image in images]
                except RuntimeError as second_error:
                    if "out of memory" not in str(second_error).lower():
                        raise
                    del captioner;torch.cuda.empty_cache();device=-1
                    captioner = pipeline("image-to-text", model="Salesforce/blip-image-captioning-base", device=-1,
                                         model_kwargs={"cache_dir": str(model_root)})
                    caption.device_label = "CPU fallback после нехватки памяти CUDA"  # type: ignore[attr-defined]
                    values = [captioner(str(image), max_new_tokens=32) for image in images]
            result=[]
            for value in values:
                item=value[0] if isinstance(value,list) else value
                result.append(str(item.get("generated_text", "")).strip())
            return result
        caption.batch = batch  # type: ignore[attr-defined]
        caption.device_label = torch.cuda.get_device_name(0) if device == 0 else "CPU"  # type: ignore[attr-defined]
        return caption
    except Exception as error:
        raise AnalysisError(f"Не удалось загрузить локальную модель анализа изображения: {error}") from error


def local_embedder() -> Callable[[str], tuple[float, ...]]:
    try:
        from sentence_transformers import SentenceTransformer
        from narezchik.app_paths import app_paths
    except ImportError as error:
        raise AnalysisError("Смысловой подбор не установлен. Установите зависимости этапа подбора кадров.") from error
    try:
        root = app_paths().vision_models; root.mkdir(parents=True, exist_ok=True)
        import torch
        device = "cuda" if torch.cuda.is_available() else "cpu"
        # One multilingual CLIP model embeds both the Russian narration and the
        # actual pixels.  BLIP captions remain a UI hint, not the sole evidence.
        model = SentenceTransformer("sentence-transformers/clip-ViT-B-32-multilingual-v1", cache_folder=str(root), device=device)
        def encode(text: str) -> tuple[float, ...]:
            nonlocal device
            try:
                values=model.encode(text,normalize_embeddings=True,show_progress_bar=False)
            except RuntimeError as error:
                if device!='cuda' or 'out of memory' not in str(error).lower():raise
                torch.cuda.empty_cache();model.to('cpu');device='cpu';values=model.encode(text,normalize_embeddings=True,show_progress_bar=False)
            return tuple(float(value) for value in values.tolist())
        def encode_images(images: list[Path]) -> list[tuple[float, ...]]:
            nonlocal device
            try:
                from PIL import Image
                opened = [Image.open(path).convert("RGB") for path in images]
                try:
                    values = model.encode(opened, normalize_embeddings=True, show_progress_bar=False)
                finally:
                    for image in opened:
                        image.close()
            except RuntimeError as error:
                if device != 'cuda' or 'out of memory' not in str(error).lower():
                    raise
                torch.cuda.empty_cache();model.to('cpu');device='cpu'
                return encode_images(images)
            return [tuple(float(value) for value in row.tolist()) for row in values]
        encode.encode_images = encode_images  # type: ignore[attr-defined]
        return encode
    except Exception as error:
        raise AnalysisError(f"Не удалось загрузить локальную смысловую модель: {error}") from error


def _extract_thumbnail(video: Path, output: Path, at: float) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run([media_tool("ffmpeg"), "-y", "-ss", f"{at:.3f}", "-i", str(video),
                                "-frames:v", "1", "-vf", "scale=480:-2", str(output)],
                               capture_output=True, text=True, check=False)
    if completed.returncode != 0 or not output.is_file():
        raise AnalysisError(completed.stderr.strip() or "FFmpeg не смог извлечь ключевой кадр.")


_DEFAULT_EXTRACT_THUMBNAIL = _extract_thumbnail


def _extract_scene_thumbnails(video: Path, outputs: list[Path], moments: tuple[float, ...]) -> None:
    """Extract three independently seeked frames with one FFmpeg process."""
    if _extract_thumbnail is not _DEFAULT_EXTRACT_THUMBNAIL:
        for output,moment in zip(outputs,moments):_extract_thumbnail(video,output,moment)
        return
    for output in outputs:output.parent.mkdir(parents=True,exist_ok=True)
    command=[media_tool("ffmpeg"),"-y"]
    for moment in moments:command += ["-ss",f"{moment:.3f}","-i",str(video)]
    for index,output in enumerate(outputs):
        command += ["-map",f"{index}:v:0","-frames:v","1","-vf","scale=480:-2",str(output)]
    completed=subprocess.run(command,capture_output=True,text=True,check=False)
    if completed.returncode!=0 or not all(output.is_file() for output in outputs):
        raise AnalysisError(completed.stderr.strip() or "FFmpeg не смог извлечь ключевые кадры сцены.")


def _write_index(destination: Path, source_fingerprint: str, scenes: list[IndexedScene], *,
                 complete: bool, revision: str) -> None:
    payload = {"source_fingerprint": source_fingerprint, "model_version": INDEX_MODEL_VERSION,
               "revision": revision, "complete": complete,
               "scenes": [item.to_dict() for item in scenes]}
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{destination.stem}-", suffix=".tmp", dir=destination.parent, text=True)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n");stream.flush();os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _write_part_cache(destination: Path, descriptions: tuple[str, ...],
                      frame_embeddings: tuple[tuple[float, ...], ...],
                      combined_embedding: tuple[float, ...]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".part-cache-", suffix=".tmp", dir=destination.parent, text=True)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump({"descriptions": list(descriptions),
                       "frame_embeddings": [list(value) for value in frame_embeddings],
                       "combined_embedding": list(combined_embedding)}, stream, ensure_ascii=False, indent=2)
            stream.write("\n");stream.flush();os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _part_cache_directory(analysis_dir: Path, fingerprint: str, start: float, end: float,
                          subtitle_text: str) -> Path:
    identity = json.dumps({"model": INDEX_MODEL_VERSION, "start": start, "end": end,
                           "subtitles": subtitle_text}, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return analysis_dir / "part-cache" / fingerprint / hashlib.sha256(identity).hexdigest()


def seed_part_cache(project_root: Path, source: object, index_path: Path) -> int:
    """Promote an existing complete index into reusable per-file cache entries."""
    analysis_dir = project_root / "analysis"
    try:
        indexed = read_index(index_path, source.fingerprint, source_duration=source.duration)
    except Exception:
        return 0
    created = 0
    for item in indexed:
        try:
            part = source.source_parts[item.source_index];offset = source.offsets[item.source_index]
            directory = _part_cache_directory(analysis_dir, part.fingerprint, item.start-offset,
                                              item.end-offset, item.subtitle_text)
            targets = [directory / f"frame-{number}.jpg" for number in range(1,4)]
            sources = [project_root / value for value in item.frame_thumbnail_paths]
            if not all(target.is_file() for target in targets):
                directory.mkdir(parents=True, exist_ok=True)
                for original,target in zip(sources,targets):
                    if not target.is_file():
                        try:os.link(original,target)
                        except OSError:shutil.copy2(original,target)
            cache_data=directory/'data.json'
            if not cache_data.is_file():
                _write_part_cache(cache_data,item.frame_descriptions,item.frame_embeddings,item.embedding)
            created += 1
        except (OSError,IndexError,AttributeError):
            continue
    return created


def discard_index_revision(index_path: Path) -> None:
    """Best-effort cleanup for a generated revision that was never activated."""
    match = re.fullmatch(r"video_index-([0-9a-f]{32})\.json", index_path.name)
    if match is None:
        return
    try:
        index_path.unlink(missing_ok=True)
        thumbnail_dir = index_path.parent / "thumbnails" / match.group(1)
        if thumbnail_dir.is_symlink() or not thumbnail_dir.is_dir():
            return
        files = list(thumbnail_dir.iterdir())
        if any(not item.is_file() or re.fullmatch(r"scene-\d{5}-[123]\.jpg", item.name) is None
               for item in files):
            return
        for item in files:
            item.unlink()
        thumbnail_dir.rmdir()
    except OSError:
        pass


def _embedding(value: Iterable[float], label: str) -> tuple[float, ...]:
    try:
        result = tuple(float(item) for item in value)
    except (TypeError, ValueError) as error:
        raise AnalysisError(f"Модель вернула некорректный {label}.") from error
    if not result or any(not math.isfinite(item) for item in result):
        raise AnalysisError(f"Модель вернула некорректный {label}.")
    return result


def _validate_scene_sequence(scenes: list[Scene]) -> None:
    if not scenes:
        raise AnalysisError("Для подбора нужны сохранённые границы сцен.")
    identifiers: set[int] = set()
    previous_end = 0.0
    for scene in scenes:
        if (not isinstance(scene.scene_id, int) or isinstance(scene.scene_id, bool) or scene.scene_id < 1
                or scene.scene_id in identifiers or not isinstance(scene.source_index, int)
                or isinstance(scene.source_index, bool) or scene.source_index < 0):
            raise AnalysisError("Границы сцен содержат повторяющийся или некорректный ID.")
        if (not all(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
                    for value in (scene.start, scene.end))
                or scene.start < previous_end or scene.end <= scene.start):
            raise AnalysisError("Границы сцен должны быть конечными, положительными и последовательными.")
        identifiers.add(scene.scene_id)
        previous_end = scene.end


def _has_complete_scene_data(item: IndexedScene) -> bool:
    if (len(item.frame_descriptions) != 3 or len(item.frame_embeddings) != 3
            or len(item.frame_thumbnail_paths) != 3 or not item.embedding
            or any(not value for value in item.frame_embeddings)
            or any(not value.strip() for value in item.frame_descriptions)
            or item.thumbnail_path != item.frame_thumbnail_paths[1]):
        return False
    dimensions = {len(item.embedding), *(len(value) for value in item.frame_embeddings)}
    return len(dimensions) == 1


def _scene_is_reusable(item: IndexedScene, scene: Scene, subtitle_text: str,
                       project_root: Path, thumbnail_dir: Path) -> bool:
    if (abs(item.start - scene.start) > 1e-6 or abs(item.end - scene.end) > 1e-6
            or item.source_index != scene.source_index
            or item.subtitle_text != subtitle_text or not _has_complete_scene_data(item)):
        return False
    paths = [project_root / value for value in item.frame_thumbnail_paths]
    return all(path.parent.resolve() == thumbnail_dir.resolve() and path.is_file() for path in paths)


def _resumable_scenes(partial: Path, source_fingerprint: str, scenes: list[Scene],
                      cues: list[SubtitleCue]) -> tuple[str | None, dict[int, IndexedScene]]:
    if not partial.is_file():
        return None, {}
    try:
        payload = json.loads(partial.read_text(encoding="utf-8"))
        revision = payload.get("revision")
        if (payload.get("source_fingerprint") != source_fingerprint
                or payload.get("model_version") != INDEX_MODEL_VERSION
                or payload.get("complete") is not False
                or not isinstance(revision, str) or re.fullmatch(r"[0-9a-f]{32}", revision) is None):
            return None, {}
        current = {scene.scene_id: scene for scene in scenes}
        result: dict[int, IndexedScene] = {}
        thumbnail_dir = partial.parent / "thumbnails" / revision
        for item in (IndexedScene.from_dict(value) for value in payload.get("scenes", [])):
            if item.scene_id in result:
                return None, {}
            scene = current.get(item.scene_id)
            if scene and _scene_is_reusable(item, scene, _cue_text(scene, cues),
                                            partial.parent.parent, thumbnail_dir):
                result[item.scene_id] = item
        return revision, result
    except (OSError, TypeError, ValueError, json.JSONDecodeError, AnalysisError):
        return None, {}


def build_index(video: Path | list[tuple], scenes: list[Scene], cues: list[SubtitleCue], analysis_dir: Path,
                source_fingerprint: str, *, captioner: Callable[[Path], str] | None = None,
                embedder: Callable[[str], tuple[float, ...]] | None = None,
                cancelled: Callable[[], bool] | None = None,
                progress: Callable[[int, int, str], None] | None = None) -> Path:
    """Index start, middle and end frames, retaining resumable partial progress."""
    _validate_scene_sequence(scenes)
    project_root = analysis_dir.parent.resolve()
    resolved_analysis = analysis_dir.resolve()
    if resolved_analysis.parent != project_root:
        raise AnalysisError("Папка анализа должна находиться внутри проекта.")
    thumbnail_root = analysis_dir / "thumbnails"
    if thumbnail_root.exists() and not thumbnail_root.resolve().is_relative_to(resolved_analysis):
        raise AnalysisError("Папка ключевых кадров выходит за пределы проекта.")
    captioner = captioner or local_captioner()
    embedder = embedder or local_embedder()
    partial = analysis_dir / PARTIAL_INDEX_FILENAME
    resumed_revision, existing = _resumable_scenes(partial, source_fingerprint, scenes, cues)
    revision = resumed_revision or uuid.uuid4().hex
    thumbnails = analysis_dir / "thumbnails" / revision
    expected_thumbnails = thumbnail_root.resolve() / revision
    if thumbnails.exists() and thumbnails.resolve() != expected_thumbnails:
        raise AnalysisError("Ревизия ключевых кадров выходит за пределы проекта.")
    destination = analysis_dir / f"video_index-{revision}.json"
    started=time.monotonic()
    staged: list[IndexedScene] = []
    def stop_if_cancelled() -> None:
        if cancelled and cancelled():
            preserved = {item.scene_id: item for item in existing.values()}
            preserved.update({item.scene_id: item for item in staged})
            ordered = [preserved[scene.scene_id] for scene in scenes if scene.scene_id in preserved]
            _write_index(partial, source_fingerprint, ordered, complete=False, revision=revision)
            raise AnalysisCancelled("Анализ фильма отменён.")
    for position, scene in enumerate(scenes, 1):
        stop_if_cancelled()
        if scene.scene_id in existing:
            staged.append(existing[scene.scene_id])
            if progress:
                progress(position, len(scenes), f"Использую сохранённый анализ сцены {position}/{len(scenes)}")
            continue
        span = max(0.01, scene.end - scene.start)
        moments = (scene.start + min(0.05, span / 2), (scene.start + scene.end) / 2,
                   scene.end - min(0.05, span / 2))
        inputs = video if isinstance(video, list) else [(video, 0.0, None)]
        try:
            source_value = inputs[scene.source_index]
            source_path, source_offset = source_value[:2]
            part_fingerprint = source_value[2] if len(source_value) > 2 else None
        except IndexError as error:
            raise AnalysisError("Сцена относится к отсутствующей части фильма.") from error
        subtitle_text = _cue_text(scene, cues)
        cache_directory = None
        if part_fingerprint:
            cache_directory = _part_cache_directory(analysis_dir,part_fingerprint,scene.start-source_offset,
                                                     scene.end-source_offset,subtitle_text)
        thumbnails_for_scene = ([(cache_directory / f"frame-{number}.jpg") for number in range(1,4)]
                                if cache_directory else
                                [thumbnails / f"scene-{scene.scene_id:05d}-{number}.jpg" for number in range(1,4)])
        cache_data = cache_directory / "data.json" if cache_directory else None
        if cache_data and cache_data.is_file() and all(item.is_file() for item in thumbnails_for_scene):
            try:
                cached=json.loads(cache_data.read_text(encoding="utf-8"));descriptions=tuple(cached["descriptions"])
                frame_embeddings=tuple(tuple(value) for value in cached["frame_embeddings"])
                combined_embedding=tuple(cached["combined_embedding"])
                relative_paths=tuple(str(item.relative_to(analysis_dir.parent)).replace("\\", "/") for item in thumbnails_for_scene)
                item=IndexedScene(scene.scene_id,scene.start,scene.end,subtitle_text," ".join(descriptions),
                                  _entities(" ".join(descriptions)),relative_paths[1],combined_embedding,
                                  descriptions,frame_embeddings,relative_paths,scene.source_index)
                if _has_complete_scene_data(item):
                    staged.append(item);_write_index(partial,source_fingerprint,staged,complete=False,revision=revision)
                    if progress:progress(position,len(scenes),f'Использую кэш части: сцена {position}/{len(scenes)}')
                    continue
            except (OSError,KeyError,TypeError,ValueError,json.JSONDecodeError,AnalysisError):
                pass
        stop_if_cancelled()
        _extract_scene_thumbnails(source_path,thumbnails_for_scene,
                                  tuple(moment-source_offset for moment in moments))
        batch = getattr(captioner, "batch", None)
        descriptions_list = ([value.strip() for value in batch(thumbnails_for_scene)] if batch
                             else [captioner(thumbnail).strip() for thumbnail in thumbnails_for_scene])
        if len(descriptions_list) != 3 or any(not value for value in descriptions_list):
            raise AnalysisError("Локальная vision-модель вернула пустое описание ключевого кадра.")
        descriptions = tuple(descriptions_list)
        relative_paths = tuple(str(item.relative_to(analysis_dir.parent)).replace("\\", "/") for item in thumbnails_for_scene)
        description = " ".join(descriptions)
        image_encoder = getattr(embedder, "encode_images", None)
        if image_encoder:
            frame_embeddings_list = [_embedding(value, "визуальный embedding кадра")
                                     for value in image_encoder(thumbnails_for_scene)]
        else:
            frame_embeddings_list = []
            for item in descriptions:
                stop_if_cancelled()
                frame_embeddings_list.append(_embedding(embedder(" ".join((subtitle_text, item))), "embedding кадра"))
        stop_if_cancelled()
        frame_embeddings = tuple(frame_embeddings_list)
        combined_embedding = _embedding(embedder(" ".join((subtitle_text, description))), "embedding сцены")
        staged.append(IndexedScene(scene.scene_id, scene.start, scene.end, subtitle_text, description,
                                   _entities(description), relative_paths[1],
                                   combined_embedding, descriptions,
                                   frame_embeddings, relative_paths, scene.source_index))
        if cache_data:
            _write_part_cache(cache_data, descriptions, frame_embeddings, combined_embedding)
        _write_index(partial, source_fingerprint, staged, complete=False, revision=revision)
        if progress:
            device=getattr(captioner,"device_label",None)
            speed=position/max(.001,time.monotonic()-started)
            progress(position, len(scenes), f"Анализ сцены {position}/{len(scenes)} — {speed:.2f} сц/с"+(f" — {device}" if device else ""))
    _write_index(destination, source_fingerprint, staged, complete=True, revision=revision)
    try:
        partial.unlink(missing_ok=True)
    except OSError:
        pass
    return destination


def read_index(path: Path, source_fingerprint: str, model_version: str = INDEX_MODEL_VERSION,
               source_duration: float | None = None) -> list[IndexedScene]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload["source_fingerprint"] != source_fingerprint:
            raise AnalysisError("Сохранённый индекс относится к другому фильму.")
        if payload.get("model_version") != model_version:
            raise AnalysisError("Сохранённый индекс создан другой версией моделей.")
        if payload.get("complete") is not True:
            raise AnalysisError("Анализ фильма ещё не завершён. Продолжите его перед подбором кадров.")
        scenes = [IndexedScene.from_dict(item) for item in payload["scenes"]]
        if not scenes:
            raise AnalysisError("Сохранённый индекс не содержит сцен.")
        _validate_scene_sequence([Scene(item.scene_id, item.start, item.end) for item in scenes])
        if source_duration is not None and (not math.isfinite(source_duration) or source_duration <= 0
                                            or scenes[-1].end > source_duration + 0.02):
            raise AnalysisError("Сохранённый индекс выходит за длительность исходного фильма.")
        for scene in scenes:
            if not _has_complete_scene_data(scene):
                raise AnalysisError(f"Сцена {scene.scene_id}: индекс ключевых кадров неполон.")
            if not all((path.parent.parent / value).is_file() for value in scene.frame_thumbnail_paths):
                raise AnalysisError(f"Сцена {scene.scene_id}: ключевой кадр отсутствует.")
        return scenes
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise AnalysisError(f"Не удалось прочитать индекс фильма: {error}") from error


def read_scenes(path: Path, source_fingerprint: str) -> list[Scene]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload["source_fingerprint"] != source_fingerprint:
            raise AnalysisError("Границы сцен относятся к другому фильму.")
        return [Scene(int(item["scene_id"]), float(item["start"]), float(item["end"]),
                      int(item.get("source_index", 0))) for item in payload["scenes"]]
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise AnalysisError(f"Не удалось прочитать границы сцен: {error}") from error


def _score(text: str, scene: IndexedScene) -> float:
    target = _words(text)
    source = _words(" ".join((scene.subtitle_text, scene.description, " ".join(scene.entities))))
    if not target or not source:
        return 0.0
    # Jaccard is bounded and understandable; later embeddings can replace this adapter
    # without changing stored project data or the selection contract.
    return len(target & source) / len(target | source)


def _semantic_score(query: tuple[float, ...], embedding: tuple[float, ...]) -> float:
    if not query or len(query) != len(embedding):
        return 0.0
    value = (sum(left * right for left, right in zip(query, embedding)) + 1) / 2
    if not math.isfinite(value):
        raise AnalysisError("Индекс содержит некорректный embedding.")
    return max(0.0, min(1.0, value))


def _scene_score(text: str, query: tuple[float, ...], scene: IndexedScene) -> float:
    lexical = _score(text, scene)
    if not query:
        return lexical
    embeddings = scene.frame_embeddings or ((scene.embedding,) if scene.embedding else ())
    semantic = max((_semantic_score(query, value) for value in embeddings),
                   default=_semantic_score(query, scene.embedding))
    return .7 * semantic + .3 * lexical


def _chain(scene: IndexedScene, ranked: list[IndexedScene], duration: float, score: Callable[[IndexedScene], float], threshold: float) -> list[VideoFragment]:
    chosen = [scene]
    for item in sorted(ranked, key=lambda value: value.start):
        if (item.start >= chosen[-1].end and score(item) >= threshold
                and sum(value.end - value.start for value in chosen) < duration):
            chosen.append(item)
    return [VideoFragment(item.start, item.end, item.scene_id, item.thumbnail_path, item.source_index)
            for item in chosen]


def select_matches(segments: Iterable[Segment], index: list[IndexedScene], *, threshold: float = MATCH_THRESHOLD,
                   embedder: Callable[[str], tuple[float, ...]] | None = None,
                   manual_matches: Iterable[SegmentMatch] = ()) -> list[SegmentMatch]:
    """Return candidates for every segment, preserving manual choices in Project."""
    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool) or not math.isfinite(threshold) or not 0 <= threshold <= 1:
        raise AnalysisError("Порог релевантности должен находиться между 0 и 1.")
    result: list[SegmentMatch] = []
    previous_end = -math.inf
    manual_by_segment = {match.segment_id: match for match in manual_matches if match.manual and match.fragments}
    manual_context_end = -math.inf
    for segment in segments:
        if not segment.has_current_audio:
            result.append(SegmentMatch(segment.segment_id, reason="Озвучка этой реплики ещё не готова."))
            continue
        query = _embedding(embedder(segment.text), "embedding реплики") if embedder else ()
        context_used = manual_context_end > -math.inf
        relevance = lambda item: _scene_score(segment.text, query, item)
        rank_score = lambda item: min(1.0, relevance(item) + (.03 if context_used and item.start >= manual_context_end else 0))
        ranked = sorted(index, key=rank_score, reverse=True)
        eligible = [item for item in ranked if relevance(item) >= threshold]
        candidate_starts = eligible[:3] if eligible else ranked[:3]
        candidates = [_chain(item, ranked, float(segment.duration), relevance, threshold) for item in candidate_starts]
        if not eligible:
            result.append(SegmentMatch(segment.segment_id, candidates=candidates, confidence=(relevance(ranked[0]) if ranked else 0.0),
                                       reason="Слабая смысловая релевантность.", context_used=context_used))
        else:
            chosen = candidates[0]
            confidence = relevance(eligible[0])
            backwards = chosen[0].start < previous_end
            # A backward jump must be materially stronger than the normal alternative.
            if backwards:
                forward = next((item for item in ranked
                                if item.start >= previous_end and relevance(item) >= threshold), None)
                if forward and rank_score(eligible[0]) < rank_score(forward) + 0.15:
                    chosen = _chain(forward, ranked, float(segment.duration), relevance, threshold)
                    confidence = relevance(forward)
                    backwards = False
            covered = sum(item.end - item.start for item in chosen) >= float(segment.duration)
            scene_by_id = {item.scene_id: item for item in index}
            has_subtitles = bool(chosen) and all(
                item.scene_id is not None and item.scene_id in scene_by_id
                and bool(scene_by_id[item.scene_id].subtitle_text.strip()) for item in chosen
            )
            next_score = relevance(ranked[1]) if len(ranked) > 1 else 0.0
            margin = confidence - next_score
            lexical_evidence = _score(segment.text, eligible[0])
            # A score around 0.55 is merely "somewhat related" for multilingual
            # embeddings.  Do not silently turn it into a finished edit.
            strong = (margin >= AUTO_ACCEPT_MARGIN and
                      (lexical_evidence >= 0.15 or
                       (confidence >= AUTO_ACCEPT_SCORE and (lexical_evidence >= 0.04 or confidence >= 0.72))))
            reason = ("Не хватает длительности из смыслово подходящих кадров." if not covered else
                      ("Для выбранных кадров нет подтверждающего текста субтитров." if not has_subtitles else
                       ("Смысловое совпадение недостаточно однозначно." if not strong else
                        ("Мягкое предупреждение: возврат назад по сюжету." if backwards else None))))
            needs_review = not covered or not has_subtitles or not strong
            result.append(SegmentMatch(segment.segment_id, chosen if not needs_review else [], candidates, confidence,
                                       needs_review, reason, context_used=context_used))
            if not needs_review:
                previous_end = chosen[-1].end
        manual = manual_by_segment.get(segment.segment_id)
        if manual:
            manual_context_end = max(fragment.end for fragment in manual.fragments)
            previous_end = manual_context_end
    return result


def confirm_all_matches(matches: Iterable[SegmentMatch]) -> tuple[int, int]:
    """Accept the first available candidate while leaving truly unresolved rows untouched."""
    confirmed = unresolved = 0
    for match in matches:
        if not match.fragments and match.candidates:
            match.fragments = list(match.candidates[0])
        if not match.fragments:
            unresolved += 1
            continue
        match.manual = False
        match.confirmation = "bulk"
        match.needs_review = False
        match.reason = None
        confirmed += 1
    return confirmed, unresolved


def matching_report(matches: Iterable[SegmentMatch]) -> dict[str, object]:
    """Return a compact, user-readable diagnostic without changing a project."""
    values = list(matches)
    scores = [item.confidence for item in values if item.confidence is not None]
    used: list[int] = []
    backwards = 0
    previous_end = -math.inf
    for item in values:
        if not item.fragments:
            continue
        if item.fragments[0].start < previous_end:
            backwards += 1
        previous_end = max(previous_end, item.fragments[-1].end)
        used.extend(fragment.scene_id for fragment in item.fragments if fragment.scene_id is not None)
    return {"total": len(values), "strong": sum(not item.needs_review for item in values),
            "needs_review": sum(item.needs_review for item in values),
            "without_candidates": sum(not item.fragments and not item.candidates for item in values),
            "reused_scenes": len(used) - len(set(used)), "backward_jumps": backwards,
            "score": {"minimum": min(scores) if scores else None, "maximum": max(scores) if scores else None,
                      "average": sum(scores) / len(scores) if scores else None}}
