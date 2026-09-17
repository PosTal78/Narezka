"""File-system and external-tool integrations."""

from .project_store import ProjectLoadResult, ProjectStore
from .script_text import SegmentationMode, read_text_file, segment_text
from .media import ffprobe_is_available
from .subtitles import SubtitleCue, SubtitleError, parse_subtitles, read_subtitles, write_canonical
from .matching import (INDEX_MODEL_VERSION, PARTIAL_INDEX_FILENAME, build_index,
                       confirm_all_matches, discard_index_revision, local_captioner, local_embedder,
                       read_index, read_scenes, seed_part_cache, select_matches)
from .video import (VideoError, VideoMetadata, ensure_current_source, ensure_current_sources,
                    has_audio_stream, inspect_video, inspect_videos, probe_video)
from .timeline import (Timeline, TimelineEntry, TimelineError, add_fragment, autofill,
                       create_from_matches, load as load_timeline, remove_fragment,
                       replace_fragment, save as save_timeline, synchronize as synchronize_timeline,
                       validate as validate_timeline)
from .export import ExportCancelled, ExportError, build_export_command, export

__all__ = ["ProjectLoadResult", "ProjectStore", "SegmentationMode", "SubtitleCue", "SubtitleError",
           "VideoError", "VideoMetadata", "ensure_current_source", "ensure_current_sources", "ffprobe_is_available",
           "inspect_video", "inspect_videos", "parse_subtitles",
           "probe_video", "read_subtitles", "read_text_file", "segment_text", "write_canonical",
           "INDEX_MODEL_VERSION", "PARTIAL_INDEX_FILENAME", "build_index", "confirm_all_matches", "discard_index_revision",
           "local_captioner", "local_embedder", "read_index", "read_scenes", "seed_part_cache", "select_matches"]
