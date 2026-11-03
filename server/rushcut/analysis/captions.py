"""SRT / ASS captions plus optional ffmpeg burn-in.

Chunking groups word timestamps by max characters, max words, inter-word
gap and trailing punctuation, then extends each cue slightly without
overlapping the next. Style presets cover 16:9 and 9:16 (MarginV, outline,
shadow). Optional karaoke-style active-word emphasis is an ASS preset.
"""
from __future__ import annotations

import os
import re
from typing import Any

from .ffmpeg_util import which_ffmpeg

# Vertical shorts: short uppercase groups. Landscape allows a longer line.
DEFAULT_MAX_WORDS = 4
DEFAULT_MAX_GAP = 0.5
DEFAULT_EXTEND = 0.35
VERTICAL_MAX_CHARS = 18
LANDSCAPE_MAX_CHARS = 42

_PUNCT_TRAIL = re.compile(r"[.!?…]+[\"')\]]*$")
_WORD_STRIP = re.compile(r"^[^A-Za-z0-9']+|[^A-Za-z0-9']+$")

STYLE_PRESETS: dict[str, dict[str, Any]] = {
    "default": {
        "name": "default",
        "aspect": "16:9",
        "play_x": 1920,
        "play_y": 1080,
        "font": "Arial",
        "size": 48,
        "outline": 4,
        "shadow": 2,
        "margin_v": 40,
        "uppercase": False,
        "max_chars": LANDSCAPE_MAX_CHARS,
        "max_words": DEFAULT_MAX_WORDS,
    },
    "vertical": {
        "name": "vertical",
        "aspect": "9:16",
        "play_x": 1080,
        "play_y": 1920,
        "font": "Arial",
        "size": 64,
        "outline": 6,
        "shadow": 2,
        "margin_v": 160,
        "uppercase": True,
        "max_chars": VERTICAL_MAX_CHARS,
        "max_words": DEFAULT_MAX_WORDS,
    },
}

# Deprecated alias kept so existing callers of style="shorts" still resolve.
STYLE_PRESETS["shorts"] = dict(STYLE_PRESETS["vertical"], name="shorts")
STYLE_PRESETS["portrait"] = dict(STYLE_PRESETS["vertical"], name="portrait")
STYLE_PRESETS["landscape"] = dict(STYLE_PRESETS["default"], name="landscape")


def _ts_srt(seconds: float) -> str:
    if seconds < 0:
        seconds = 0
    ms = int(round(seconds * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, milli = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{milli:03d}"


def _ts_ass(seconds: float) -> str:
    if seconds < 0:
        seconds = 0
    cs = int(round(seconds * 100))
    h, rem = divmod(cs, 360_000)
    m, rem = divmod(rem, 6_000)
    s, c = divmod(rem, 100)
    return f"{h:d}:{m:02d}:{s:02d}.{c:02d}"


def _word_start(word: dict[str, Any]) -> float:
    return float(word.get("start_seconds") or word.get("start") or 0)


def _word_end(word: dict[str, Any]) -> float:
    return float(word.get("end_seconds") or word.get("end") or _word_start(word))


def _word_text(word: dict[str, Any]) -> str:
    return str(word.get("word") or word.get("text") or "").strip()


def _normalize_token(text: str) -> str:
    return _WORD_STRIP.sub("", text).casefold()


def _style_preset(style: str) -> dict[str, Any]:
    key = (style or "default").lower()
    if key in STYLE_PRESETS:
        return dict(STYLE_PRESETS[key])
    return dict(STYLE_PRESETS["default"])


def apply_fix_map(
    words: list[dict[str, Any]],
    fix_map: dict[str, str] | list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Replace word text from a find/replace map. Applied before chunking."""
    if not fix_map:
        return [dict(w) for w in words]
    mapping: dict[str, str] = {}
    if isinstance(fix_map, dict):
        mapping = {str(k): str(v) for k, v in fix_map.items()}
    else:
        for item in fix_map:
            find = item.get("find") or item.get("from") or item.get("src")
            repl = item.get("replace") or item.get("to") or item.get("dst")
            if find is not None and repl is not None:
                mapping[str(find)] = str(repl)
    folded = {k.casefold(): v for k, v in mapping.items()}
    out: list[dict[str, Any]] = []
    for word in words:
        item = dict(word)
        token = _word_text(item)
        if not token:
            out.append(item)
            continue
        bare = _normalize_token(token)
        repl = mapping.get(token) or folded.get(token.casefold()) or folded.get(bare)
        if repl is not None:
            # Keep surrounding punctuation from the original token when the
            # replacement is a bare word.
            prefix = token[: len(token) - len(token.lstrip())]
            core = token.strip()
            suffix = core[len(_normalize_token(core)) :] if _normalize_token(core) else ""
            item["word"] = f"{prefix}{repl}{suffix}"
            item["original_word"] = token
        out.append(item)
    return out


def apply_keep_windows(
    words: list[dict[str, Any]],
    keep_windows: list[dict[str, Any]] | list[tuple[float, float]] | None,
) -> list[dict[str, Any]]:
    """Keep only words whose midpoint falls inside a keep-window."""
    if not keep_windows:
        return [dict(w) for w in words]
    windows: list[tuple[float, float]] = []
    for win in keep_windows:
        if isinstance(win, dict):
            start = float(win.get("start") or win.get("start_seconds") or 0)
            end = float(win.get("end") or win.get("end_seconds") or start)
        else:
            start, end = float(win[0]), float(win[1])
        if end < start:
            start, end = end, start
        windows.append((start, end))
    kept: list[dict[str, Any]] = []
    for word in words:
        mid = (_word_start(word) + _word_end(word)) / 2.0
        if any(lo <= mid <= hi for lo, hi in windows):
            kept.append(dict(word))
    return kept


def mask_word_list(
    words: list[dict[str, Any]],
    mask_words: list[str] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Mask listed words in caption text and return the original spans.

    ``masked_spans`` is ``[{start, end, word}]`` so ``bleep_spans`` can
    line up audio with the caption mask.
    """
    if not mask_words:
        return [dict(w) for w in words], []
    banned = {_normalize_token(w) for w in mask_words if _normalize_token(w)}
    banned.update(w.casefold() for w in mask_words if w)
    out: list[dict[str, Any]] = []
    spans: list[dict[str, Any]] = []
    for word in words:
        item = dict(word)
        token = _word_text(item)
        bare = _normalize_token(token)
        if bare and (bare in banned or token.casefold() in banned):
            stars = "*" * max(1, len(bare))
            prefix = token[: len(token) - len(token.lstrip())]
            suffix = token[len(token.rstrip()) :] if token.rstrip() != token else ""
            # Preserve a trailing punctuation cluster after the stars.
            core = token.strip()
            punct = ""
            if bare and core.lower().startswith(bare):
                punct = core[len(bare) :]
            item["word"] = f"{prefix}{stars}{punct}{suffix}"
            item["masked"] = True
            item["original_word"] = token
            spans.append(
                {
                    "start": _word_start(word),
                    "end": _word_end(word),
                    "word": token,
                }
            )
        out.append(item)
    return out, spans


def _display_text(token: str, uppercase: bool) -> str:
    text = token.strip()
    if uppercase:
        text = text.upper()
    return text


def _ends_sentence(token: str) -> bool:
    return bool(_PUNCT_TRAIL.search(token.strip()))


def _would_exceed(cur: list[dict[str, Any]], nxt: dict[str, Any], max_chars: int) -> bool:
    parts = [_word_text(w) for w in cur] + [_word_text(nxt)]
    joined = " ".join(p for p in parts if p)
    return len(joined) > max_chars


def chunk_words(
    words: list[dict[str, Any]] | None,
    max_chars: int = LANDSCAPE_MAX_CHARS,
    max_words: int = DEFAULT_MAX_WORDS,
    max_gap: float = DEFAULT_MAX_GAP,
    extend_seconds: float = DEFAULT_EXTEND,
    split_on_punctuation: bool = True,
    uppercase: bool = False,
) -> list[dict[str, Any]]:
    """Group word timestamps into caption chunks.

    Splits when the next word would exceed ``max_chars`` or ``max_words``,
    when the gap to the next word is greater than ``max_gap`` (default
    0.5 s), or when the current word ends with trailing punctuation.
    Each chunk is extended by ``extend_seconds`` (~0.35 s) but never into
    the following chunk's start.
    """
    cleaned: list[dict[str, Any]] = []
    for word in words or []:
        token = _word_text(word)
        if not token:
            continue
        item = dict(word)
        item["word"] = token
        cleaned.append(item)
    if not cleaned:
        return []

    groups: list[list[dict[str, Any]]] = []
    cur: list[dict[str, Any]] = []
    for word in cleaned:
        if not cur:
            cur = [word]
            continue
        gap = _word_start(word) - _word_end(cur[-1])
        over_words = len(cur) >= max_words
        over_chars = _would_exceed(cur, word, max_chars)
        over_gap = gap > max_gap
        punct = split_on_punctuation and _ends_sentence(_word_text(cur[-1]))
        if over_words or over_chars or over_gap or punct:
            groups.append(cur)
            cur = [word]
        else:
            cur.append(word)
    if cur:
        groups.append(cur)

    chunks: list[dict[str, Any]] = []
    for i, group in enumerate(groups):
        start = _word_start(group[0])
        raw_end = _word_end(group[-1])
        next_start = _word_start(groups[i + 1][0]) if i + 1 < len(groups) else None
        end = raw_end + max(0.0, float(extend_seconds))
        if next_start is not None:
            end = min(end, next_start)
        if end <= start:
            end = raw_end if raw_end > start else start + 0.04
        texts = [_display_text(_word_text(w), uppercase) for w in group]
        chunk_words_out = []
        for w in group:
            item = dict(w)
            item["word"] = _display_text(_word_text(w), uppercase)
            chunk_words_out.append(item)
        chunks.append(
            {
                "start_seconds": start,
                "end_seconds": end,
                "raw_end_seconds": raw_end,
                "text": " ".join(t for t in texts if t).strip(),
                "words": chunk_words_out,
            }
        )
    return chunks


def _group_words(
    words: list[dict[str, Any]],
    max_words: int = DEFAULT_MAX_WORDS,
    max_seconds: float = 1.6,
    max_chars: int = LANDSCAPE_MAX_CHARS,
    max_gap: float = DEFAULT_MAX_GAP,
    extend_seconds: float = DEFAULT_EXTEND,
    split_on_punctuation: bool = True,
    uppercase: bool = False,
) -> list[dict[str, Any]]:
    """Backward-compatible wrapper: ``max_seconds`` becomes a span cap."""
    chunks = chunk_words(
        words,
        max_chars=max_chars,
        max_words=max_words,
        max_gap=max_gap,
        extend_seconds=extend_seconds,
        split_on_punctuation=split_on_punctuation,
        uppercase=uppercase,
    )
    if max_seconds and max_seconds > 0:
        split: list[dict[str, Any]] = []
        for chunk in chunks:
            group = list(chunk.get("words") or [])
            if not group:
                split.append(chunk)
                continue
            span = _word_end(group[-1]) - _word_start(group[0])
            if span < max_seconds or len(group) <= 1:
                split.append(chunk)
                continue
            # Rare: a punctuation-free burst longer than max_seconds.
            mid = max(1, len(group) // 2)
            split.extend(
                chunk_words(
                    group[:mid],
                    max_chars=max_chars,
                    max_words=max_words,
                    max_gap=max_gap,
                    extend_seconds=extend_seconds,
                    split_on_punctuation=split_on_punctuation,
                    uppercase=uppercase,
                )
            )
            split.extend(
                chunk_words(
                    group[mid:],
                    max_chars=max_chars,
                    max_words=max_words,
                    max_gap=max_gap,
                    extend_seconds=extend_seconds,
                    split_on_punctuation=split_on_punctuation,
                    uppercase=uppercase,
                )
            )
        return split
    return chunks


def _cues_from_input(
    segments: list[dict[str, Any]] | None,
    words: list[dict[str, Any]] | None,
    group_words: bool,
    *,
    max_chars: int,
    max_words: int,
    max_gap: float,
    extend_seconds: float,
    split_on_punctuation: bool,
    uppercase: bool,
) -> list[dict[str, Any]]:
    if words and group_words:
        return chunk_words(
            words,
            max_chars=max_chars,
            max_words=max_words,
            max_gap=max_gap,
            extend_seconds=extend_seconds,
            split_on_punctuation=split_on_punctuation,
            uppercase=uppercase,
        )
    cues: list[dict[str, Any]] = []
    for seg in segments or []:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        if uppercase:
            text = text.upper()
        start = float(seg.get("start_seconds") or seg.get("start") or 0)
        end = float(seg.get("end_seconds") or seg.get("end") or start + 1.5)
        if end <= start:
            end = start + 1.2
        cues.append({"start_seconds": start, "end_seconds": end, "text": text, "words": []})
    return cues


def _ass_header(
    style: str,
    *,
    outline: int | None = None,
    shadow: int | None = None,
    margin_v: int | None = None,
    active_word: bool = False,
) -> str:
    preset = _style_preset(style)
    play_x = int(preset["play_x"])
    play_y = int(preset["play_y"])
    font = str(preset["font"])
    size = int(preset["size"])
    outline_w = int(preset["outline"] if outline is None else outline)
    shadow_w = int(preset["shadow"] if shadow is None else shadow)
    margin = int(preset["margin_v"] if margin_v is None else margin_v)
    # White fill. Karaoke uses SecondaryColour for unsung, Primary for sung.
    primary = "&H00FFFFFF"
    secondary = "&H0000FFFF" if active_word else "&H000000FF"
    if active_word:
        # Active word is yellow; upcoming words stay white.
        primary = "&H0000FFFF"
        secondary = "&H00FFFFFF"
    outline_c = "&H00000000"
    return (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {play_x}\n"
        f"PlayResY: {play_y}\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{font},{size},{primary},{secondary},{outline_c},"
        f"&H80000000,-1,0,0,0,100,100,0,0,1,{outline_w},{shadow_w},2,60,60,{margin},1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )


def _karaoke_text(cue: dict[str, Any]) -> str:
    words = list(cue.get("words") or [])
    if not words:
        return (cue.get("text") or "").replace("\n", "\\N")
    parts: list[str] = []
    for i, word in enumerate(words):
        start = _word_start(word)
        end = _word_end(word)
        dur_cs = max(1, int(round((end - start) * 100)))
        token = _word_text(word).replace("\n", "\\N")
        if i + 1 < len(words):
            gap = _word_start(words[i + 1]) - end
            if gap > 0:
                dur_cs += int(round(gap * 100))
        parts.append(f"{{\\k{dur_cs}}}{token}")
    return " ".join(parts)


def generate_captions(
    path: str | None = None,
    segments: list[dict[str, Any]] | None = None,
    words: list[dict[str, Any]] | None = None,
    output_dir: str | None = None,
    style: str = "default",
    group_words: bool = True,
    stem: str | None = None,
    max_chars: int | None = None,
    max_words: int = DEFAULT_MAX_WORDS,
    max_gap: float = DEFAULT_MAX_GAP,
    extend_seconds: float = DEFAULT_EXTEND,
    split_on_punctuation: bool = True,
    mask_words: list[str] | None = None,
    uppercase: bool | None = None,
    outline: int | None = None,
    shadow: int | None = None,
    margin_v: int | None = None,
    fix_map: dict[str, str] | list[dict[str, Any]] | None = None,
    keep_windows: list[dict[str, Any]] | list[tuple[float, float]] | None = None,
    active_word: bool = False,
    initial_prompt: str | None = None,
) -> dict[str, Any]:
    """Write SRT + ASS next to the media (or output_dir). No NLE required.

    Chunking params (``max_chars``, ``max_words``, ``max_gap``, punctuation
    splits, ``extend_seconds``) are applied after ``fix_map`` and
    ``keep_windows``. ``mask_words`` returns ``masked_spans`` for bleeps.
    ``initial_prompt`` is forwarded to Whisper when ``path`` is transcribed.
    """
    preset = _style_preset(style)
    if max_chars is None:
        max_chars = int(preset["max_chars"])
    if uppercase is None:
        # Only auto-uppercase the vertical preset when the caller did not
        # pass words/segments that existing tests expect to stay mixed-case.
        # Explicit style="vertical" + words still uppercases (shorts look).
        uppercase = bool(preset.get("uppercase")) if (style or "").lower() in {
            "vertical",
            "shorts",
            "portrait",
        } else False

    working_words = list(words or [])
    transcribed = None
    if not working_words and not segments and path:
        from .whisper import transcribe_media

        transcribed = transcribe_media(path, initial_prompt=initial_prompt)
        if not transcribed.get("ok"):
            return transcribed
        working_words = list(transcribed.get("words") or [])
        if not working_words and transcribed.get("segments"):
            segments = list(transcribed.get("segments") or [])

    masked_spans: list[dict[str, Any]] = []
    if working_words:
        working_words = apply_fix_map(working_words, fix_map)
        working_words = apply_keep_windows(working_words, keep_windows)
        working_words, masked_spans = mask_word_list(working_words, mask_words)

    cues = _cues_from_input(
        segments,
        working_words or None,
        group_words,
        max_chars=int(max_chars),
        max_words=max_words,
        max_gap=max_gap,
        extend_seconds=extend_seconds,
        split_on_punctuation=split_on_punctuation,
        uppercase=bool(uppercase),
    )
    if not cues:
        return {"ok": False, "error": "no caption cues (pass segments or words)"}

    base = stem
    if not base:
        if path:
            base = os.path.splitext(os.path.basename(path))[0]
        else:
            base = "captions"
    dest = output_dir or (os.path.dirname(os.path.abspath(path)) if path else os.getcwd())
    from ..output_policy import check_output_path

    refused = check_output_path(dest)
    if refused:
        return refused
    os.makedirs(dest, exist_ok=True)
    srt_path = os.path.join(dest, base + ".srt")
    ass_path = os.path.join(dest, base + ".ass")

    srt_lines: list[str] = []
    for i, cue in enumerate(cues, start=1):
        srt_lines.append(str(i))
        srt_lines.append(f"{_ts_srt(cue['start_seconds'])} --> {_ts_srt(cue['end_seconds'])}")
        srt_lines.append(cue["text"])
        srt_lines.append("")
    with open(srt_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(srt_lines))

    ass = [
        _ass_header(
            style,
            outline=outline,
            shadow=shadow,
            margin_v=margin_v,
            active_word=active_word,
        )
    ]
    for cue in cues:
        if active_word:
            text = _karaoke_text(cue)
        else:
            text = (cue["text"] or "").replace("\n", "\\N")
        ass.append(
            f"Dialogue: 0,{_ts_ass(cue['start_seconds'])},{_ts_ass(cue['end_seconds'])},"
            f"Default,,0,0,0,,{text}\n"
        )
    with open(ass_path, "w", encoding="utf-8") as fh:
        fh.writelines(ass)

    result: dict[str, Any] = {
        "ok": True,
        "style": style,
        "preset": {
            "aspect": preset["aspect"],
            "play_x": preset["play_x"],
            "play_y": preset["play_y"],
            "margin_v": int(preset["margin_v"] if margin_v is None else margin_v),
            "outline": int(preset["outline"] if outline is None else outline),
            "shadow": int(preset["shadow"] if shadow is None else shadow),
            "uppercase": bool(uppercase),
            "active_word": bool(active_word),
        },
        "cue_count": len(cues),
        "srt_path": srt_path,
        "ass_path": ass_path,
        "cues": cues,
        "masked_spans": masked_spans,
        "chunking": {
            "max_chars": int(max_chars),
            "max_words": max_words,
            "max_gap": max_gap,
            "extend_seconds": extend_seconds,
            "split_on_punctuation": split_on_punctuation,
        },
    }
    if initial_prompt:
        result["initial_prompt"] = initial_prompt
    if transcribed is not None:
        result["transcript"] = {
            "ok": transcribed.get("ok"),
            "word_count": len(transcribed.get("words") or []),
        }
    return result


def burn_captions(
    media_path: str,
    ass_path: str,
    output_path: str | None = None,
    fontsdir: str | None = None,
) -> dict[str, Any]:
    """Burn an ASS file into a new MP4 with ffmpeg / libass. No VEGAS required."""
    if not os.path.isfile(media_path):
        return {"ok": False, "error": f"file not found: {media_path}"}
    if not os.path.isfile(ass_path):
        return {"ok": False, "error": f"ASS not found: {ass_path}"}
    if not output_path:
        root, ext = os.path.splitext(media_path)
        output_path = root + ".captioned" + (ext or ".mp4")
    # Escape the ASS path for the subtitles filter (Windows + Linux).
    ass_esc = ass_path.replace("\\", "/").replace(":", "\\:").replace("'", r"\'")
    filt = f"ass='{ass_esc}'"
    if fontsdir:
        fonts_esc = fontsdir.replace("\\", "/").replace(":", "\\:").replace("'", r"\'")
        filt += f":fontsdir='{fonts_esc}'"
    from ..render.atomic import run_ffmpeg_atomic

    written = run_ffmpeg_atomic(
        [
            which_ffmpeg("ffmpeg"),
            "-hide_banner",
            "-y",
            "-i",
            media_path,
            "-vf",
            filt,
            "-c:a",
            "copy",
        ],
        output_path,
        timeout=300,
    )
    if not written.get("ok"):
        return written
    written["ass_path"] = ass_path
    return written
