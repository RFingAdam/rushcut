"""Media grounding: ffprobe, scenes, loudness, captions, file-level ffmpeg ops.

These modules are pure Python + ffmpeg. They do not talk to an NLE.
"""
from .audio import analyze_audio
from .captions import burn_captions, generate_captions
from .edit_ops import concat_media, cut_media, mix_audio, normalize_audio, reframe_media
from .probe import probe_media
from .scenes import detect_scenes
from .transcript import analyze_transcript, attach_speakers, speech_spans_from_words
from .whisper import transcribe_media

__all__ = [
    "analyze_audio",
    "analyze_transcript",
    "attach_speakers",
    "burn_captions",
    "concat_media",
    "cut_media",
    "detect_scenes",
    "generate_captions",
    "mix_audio",
    "normalize_audio",
    "probe_media",
    "reframe_media",
    "speech_spans_from_words",
    "transcribe_media",
]
