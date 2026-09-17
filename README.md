# Narezchik

Локальное desktop-приложение для создания видеопересказов фильмов и сериалов. Основной поток: сценарий → озвучка → анализ фильма → подбор кадров → монтаж → экспорт.

## Обязательное чтение для агента

Перед серьёзной задачей прочитайте:

1. [AGENTS.md](AGENTS.md)
2. [PRD.md](PRD.md)
3. [CHECKLIST.md](CHECKLIST.md)
4. [TASKS.md](TASKS.md)
5. Релевантные документы в [docs/](docs/)

Не начинайте этапы 2–5, пока этап 1 не завершён и не проверен.

## Стек

- Python 3.11+
- PySide6
- Edge TTS
- FFmpeg/ffprobe

Будущие локальные компоненты: faster-whisper, PySceneDetect, OpenCV, embeddings и vision-модели. Платные API не обязательны.

## Подготовка окружения

На Windows требуется Python 3.11+ и полный комплект FFmpeg, включая `ffprobe`.
После их установки откройте новую консоль, чтобы она получила обновлённый `PATH`.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
# Для локальной расшифровки и поиска сцен:
python -m pip install -e ".[video,matching]"

# Для NVIDIA GPU на Windows после установки зависимостей:
powershell -ExecutionPolicy Bypass -File scripts/enable_cuda.ps1
```

Убедитесь, что `ffmpeg` и `ffprobe` доступны в `PATH`:

```powershell
python --version
ffmpeg -version
ffprobe -version
```

## Запуск и проверка

```powershell
python -m narezchik
python -m unittest discover -s tests -v
```

## Переносимая версия для Windows

Основная версия для работы — [portable/Narezchik.exe](portable/Narezchik.exe). Она не требует Python и хранит проекты, кэш и модели в `portable/data/`; эта папка не удаляется при обновлении программы. `.venv/` — техническое окружение для разработки и сборки, а не место для запуска пользователем. Инструкция сборки: [docs/PORTABLE_RELEASE.md](docs/PORTABLE_RELEASE.md).

## Документация

- [PRD.md](PRD.md) — полный продуктовый контракт из ТЗ.
- [TASKS.md](TASKS.md) — поэтапный план.
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — границы Python-модулей.
- [docs/PROJECT_FORMAT.md](docs/PROJECT_FORMAT.md) — проект и `project.json`.
- [docs/TTS.md](docs/TTS.md) — первый этап озвучки.
- [docs/VIDEO_PIPELINE.md](docs/VIDEO_PIPELINE.md) — будущие этапы видео.
- [docs/TESTING.md](docs/TESTING.md) — условия проверки.
- [docs/PORTABLE_RELEASE.md](docs/PORTABLE_RELEASE.md) — переносимый ZIP-выпуск Windows.

## Лицензия и атрибуция

Каркас адаптирован из Vibe Coding Template. [LICENSE](LICENSE) и [NOTICE](NOTICE) сохранены в соответствии с Apache License 2.0.
