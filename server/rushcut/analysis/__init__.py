"""Media grounding: ffprobe, scenes, loudness, captions, pacing, file-level ffmpeg ops.

These modules are pure Python + ffmpeg. They do not talk to an NLE.
"""
from .audio import analyze_audio
from .audio_ops import (
    add_music_bed,
    bleep_spans,
    insert_audio,
    loop_fades,
    replace_audio_span,
)
from .captions import burn_captions, chunk_words, generate_captions
from .cleanup import analyze_noise_floor, audio_cleanup
from .edit_ops import concat_media, cut_media, mix_audio, normalize_audio, reframe_media
from .loudness import master_audio, measure_loudness
from .pacing import measure_pacing, pacing_from_words
from .probe import file_still_writing, probe_media
from .region import detect_region_presence
from .scenes import detect_scenes
from .session import lookup_session_time, repair_media, stitch_session
from .speakers import attribute_speakers
from .transcript import analyze_transcript, attach_speakers, speech_spans_from_words
from .whisper import transcribe_media

__all__ = [
    "add_music_bed",
    "analyze_audio",
    "analyze_noise_floor",
    "analyze_transcript",
    "attach_speakers",
    "attribute_speakers",
    "audio_cleanup",
    "bleep_spans",
    "burn_captions",
    "chunk_words",
    "concat_media",
    "cut_media",
    "detect_region_presence",
    "detect_scenes",
    "file_still_writing",
    "generate_captions",
    "insert_audio",
    "lookup_session_time",
    "loop_fades",
    "master_audio",
    "measure_loudness",
    "measure_pacing",
    "mix_audio",
    "normalize_audio",
    "pacing_from_words",
    "probe_media",
    "reframe_media",
    "repair_media",
    "replace_audio_span",
    "speech_spans_from_words",
    "stitch_session",
    "transcribe_media",
]
