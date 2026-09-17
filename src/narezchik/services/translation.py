"""Offline subtitle translation used when a source film is not in Russian."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterable

from narezchik.app_paths import app_paths
from .analysis import AnalysisCancelled, AnalysisError
from .subtitles import SubtitleCue

NLLB_MODEL = "facebook/nllb-200-distilled-600M"
_LANGUAGES = {"ja": "jpn_Jpan", "en": "eng_Latn", "ko": "kor_Hang", "zh": "zho_Hans",
              "de": "deu_Latn", "fr": "fra_Latn", "es": "spa_Latn", "it": "ita_Latn",
              "pt": "por_Latn", "uk": "ukr_Cyrl", "ru": "rus_Cyrl"}


def translate_to_russian(cues: Iterable[SubtitleCue], source_language: str, *,
                         cancelled: Callable[[], bool] | None = None,
                         progress: Callable[[int, int, str], None] | None = None) -> list[SubtitleCue]:
    """Translate recognized dialogue without changing its source timecodes."""
    source = _LANGUAGES.get(source_language.lower())
    if not source:
        raise AnalysisError(f"Для языка «{source_language}» локальный перевод пока недоступен. Импортируйте русские SRT/VTT.")
    values = list(cues)
    if not values:
        return []
    try:
        import torch
        from transformers import AutoModelForSeq2SeqLM, AutoTokenizer
    except ImportError as error:
        raise AnalysisError("Локальный перевод не установлен. Установите зависимости подбора кадров.") from error
    root: Path = app_paths().translation_models
    root.mkdir(parents=True, exist_ok=True)
    if progress:
        progress(0, len(values), "Загружаю локальный переводчик в русский. При первом запуске будет скачана модель около 1–1,5 ГБ…")
    device = "cuda" if torch.cuda.is_available() else "cpu"
    try:
        tokenizer = AutoTokenizer.from_pretrained(NLLB_MODEL, cache_dir=str(root), src_lang=source)
        model = AutoModelForSeq2SeqLM.from_pretrained(NLLB_MODEL, cache_dir=str(root)).to(device)
    except Exception as error:
        raise AnalysisError(f"Не удалось загрузить локальный переводчик: {error}") from error
    target_id = tokenizer.convert_tokens_to_ids("rus_Cyrl")
    translated: list[SubtitleCue] = []
    try:
        for number, cue in enumerate(values, 1):
            if cancelled and cancelled():
                raise AnalysisCancelled("Локальный перевод отменён.")
            inputs = tokenizer(cue.text, return_tensors="pt", truncation=True, max_length=256).to(device)
            output = model.generate(**inputs, forced_bos_token_id=target_id, max_new_tokens=96)
            text = tokenizer.batch_decode(output, skip_special_tokens=True)[0].strip()
            translated.append(SubtitleCue(cue.start, cue.end, text or cue.text))
            if progress:
                progress(number, len(values), f"Перевожу в русский: {number}/{len(values)}")
    finally:
        del model
        if device == "cuda":
            torch.cuda.empty_cache()
    return translated
