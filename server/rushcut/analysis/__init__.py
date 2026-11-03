"""Media grounding: ffprobe, scenes, loudness, captions, pacing, file-level ffmpeg ops.

These modules are pure Python + ffmpeg. They do not talk to an NLE.
"""
from .audio import analyze_audio
from .captions import burn_captions, generate_captions
from .edit_ops import concat_media, cut_media, mix_audio, normalize_audio, reframe_media
from .loudness import measure_loudness
from .pacing import measure_pacing, pacing_from_words
from .probe import probe_media
from .region import detect_region_presence
from .scenes import detect_scenes
from .session import lookup_session_time, repair_media, stitch_session
from .transcript import analyze_transcript, attach_speakers, speech_spans_from_words
from .whisper import transcribe_media

__all__ = [
    "analyze_audio",
    "analyze_transcript",
    "attach_speakers",
    "burn_captions",
    "concat_media",
    "cut_media",
    "detect_region_presence",
    "detect_scenes",
    "generate_captions",
    "lookup_session_time",
    "measure_loudness",
    "measure_pacing",
    "mix_audio",
    "normalize_audio",
    "pacing_from_words",
    "probe_media",
    "repair_media",
    "reframe_media",
    "speech_spans_from_words",
    "stitch_session",
    "transcribe_media",
]
