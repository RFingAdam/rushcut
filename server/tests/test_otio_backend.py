from __future__ import annotations

import json
from pathlib import Path

from rushcut.backends import reset_backend
from rushcut.backends.otio import OtioBackend
from rushcut import tools


def _otio() -> OtioBackend:
    backend = OtioBackend()
    reset_backend(backend)
    return backend


def test_in_memory_edit_and_otio_roundtrip(tmp_path: Path):
    _otio()
    assert tools.ping()["mode"] == "memory"
    tools.new_project(discard_unsaved=True)
    tools.set_project_video(1080, 1920, 24)
    tools.add_track("video", "V1")
    tools.add_video_event(0, str(tmp_path / "clip.mp4"), 0.0, 8.0)
    tools.trim_event(0, 0, take_offset_seconds=2.0)
    tools.add_marker(1.5, "hook")
    dest = tmp_path / "short.otio"
    saved = tools.save_project(str(dest))
    assert saved["ok"] is True
    data = json.loads(dest.read_text(encoding="utf-8"))
    assert data["OTIO_SCHEMA"] == "Timeline.1"
    assert data["tracks"]["children"][0]["kind"] == "Video"
    opened = tools.open_project(str(dest), discard_unsaved=True)
    assert opened["ok"] is True
    state = tools.get_project_state()
    assert state["events"][0]["length_seconds"] == 8.0
    assert state["width"] == 1080


def test_fcpxml_and_mlt_export(tmp_path: Path):
    _otio()
    tools.new_project(discard_unsaved=True)
    tools.add_track("video", "V1")
    tools.add_video_event(0, str(tmp_path / "a.mp4"), 0.0, 4.0)
    fcp = tmp_path / "seq.fcpxml"
    assert tools.save_project(str(fcp))["ok"] is True
    text = fcp.read_text(encoding="utf-8")
    assert "<fcpxml" in text
    assert 'version="1.9"' in text
    assert "asset-clip" in text
    mlt = tmp_path / "seq.mlt"
    assert tools.save_project(str(mlt))["ok"] is True
    mlt_text = mlt.read_text(encoding="utf-8")
    assert "<mlt" in mlt_text
    assert "producer" in mlt_text


def test_helpers_work_on_otio_backend():
    _otio()
    tools.new_project(discard_unsaved=True)
    tools.add_track("video", "V1")
    tools.add_video_event(0, r"D:\a.mp4", 0.0, 4.0)
    tools.add_video_event(0, r"D:\b.mp4", 4.0, 4.0)
    from rushcut.edit.helpers import apply_crossfade

    result = apply_crossfade(0, 0, 1, seconds=0.5)
    assert result["ok"] is True
    assert abs(result["right_start_seconds"] - 3.5) < 1e-6


def _marked_timeline(tmp_path: Path) -> None:
    _otio()
    tools.new_project(discard_unsaved=True)
    tools.set_project_video(1920, 1080, 30)
    tools.add_track("video", "V1")
    tools.add_track("audio", "A1")
    tools.add_video_event(0, str(tmp_path / "a.mp4"), 0.0, 10.0)
    tools.add_video_event(0, str(tmp_path / "b.mp4"), 10.0, 10.0)
    tools.trim_event(0, 1, take_offset_seconds=5.0)
    tools.add_audio_event(1, str(tmp_path / "a.mp4"), 0.0, 10.0)
    # Added out of order on purpose: every export sorts by time.
    tools.add_marker(12.5, "Boss fight")
    tools.add_marker(0.0, "Intro")
    tools.add_marker(65.0, "Late")


def test_otio_export_has_native_markers(tmp_path: Path):
    _marked_timeline(tmp_path)
    dest = tmp_path / "m.otio"
    saved = tools.save_project(str(dest))
    assert saved["ok"] is True and saved["markers"] == 3
    stack = json.loads(dest.read_text(encoding="utf-8"))["tracks"]
    names = [m["name"] for m in stack["markers"]]
    assert names == ["Intro", "Boss fight", "Late"]
    boss = stack["markers"][1]
    assert boss["OTIO_SCHEMA"] == "Marker.2"
    assert boss["color"] == "GREEN"
    assert boss["marked_range"]["start_time"] == {"OTIO_SCHEMA": "RationalTime.1", "value": 375.0, "rate": 30.0}
    # Re-opening still restores the markers.
    assert tools.open_project(str(dest), discard_unsaved=True)["ok"] is True
    assert len(tools.list_markers()["markers"]) == 3


def test_fcpxml_chapter_markers_sit_on_the_covering_clip(tmp_path: Path):
    import xml.etree.ElementTree as ET

    _marked_timeline(tmp_path)
    dest = tmp_path / "m.fcpxml"
    assert tools.save_project(str(dest))["ok"] is True
    root = ET.parse(dest).getroot()
    clips = root.findall(".//spine/asset-clip")
    assert [c.get("offset") for c in clips] == ["0/30s", "300/30s"]
    first = [(m.get("value"), m.get("start")) for m in clips[0].findall("chapter-marker")]
    second = [(m.get("value"), m.get("start")) for m in clips[1].findall("chapter-marker")]
    assert first == [("Intro", "0/30s")]
    # 12.5 s on the timeline is 2.5 s into a clip whose source starts at 5 s.
    assert second == [("Boss fight", "225/30s")]
    assert all(m.get("duration") == "1/30s" for m in root.iter("chapter-marker"))


def test_mlt_markers_block(tmp_path: Path):
    import xml.etree.ElementTree as ET

    _marked_timeline(tmp_path)
    dest = tmp_path / "m.mlt"
    assert tools.save_project(str(dest))["ok"] is True
    tractor = ET.parse(dest).getroot().find("tractor")
    assert tractor is not None
    block = tractor.find("properties[@name='shotcut:markers']")
    assert block is not None
    entries = block.findall("properties")
    assert [e.get("name") for e in entries] == ["0", "1", "2"]
    values = {p.get("name"): p.text for p in entries[1].findall("property")}
    assert values == {"text": "Boss fight", "start": "00:00:12.500", "end": "00:00:12.500", "color": "#00c000"}


def test_xmeml_export_places_clips_and_markers(tmp_path: Path):
    import xml.etree.ElementTree as ET

    _marked_timeline(tmp_path)
    dest = tmp_path / "m.xmeml"
    saved = tools.save_project(str(dest))
    assert saved["ok"] is True and saved["format"] == "xmeml"
    text = dest.read_text(encoding="utf-8")
    assert "<!DOCTYPE xmeml>" in text
    root = ET.fromstring(text.split("\n", 1)[1] if text.startswith("<?xml") else text)
    assert root.tag == "xmeml" and root.get("version") == "5"
    seq = root.find("sequence")
    assert seq is not None
    assert seq.findtext("duration") == "600"
    assert seq.findtext("rate/timebase") == "30" and seq.findtext("rate/ntsc") == "FALSE"
    assert seq.findtext("media/video/format/samplecharacteristics/width") == "1920"
    vclips = seq.findall("media/video/track/clipitem")
    placed = [(c.findtext("start"), c.findtext("end"), c.findtext("in"), c.findtext("out")) for c in vclips]
    assert placed == [("0", "300", "0", "300"), ("300", "600", "150", "450")]
    # The same source file is described once, then referenced by id.
    first_file = vclips[0].find("file")
    assert first_file is not None and first_file.findtext("pathurl", "").startswith("file://")
    aclip = seq.find("media/audio/track/clipitem")
    assert aclip is not None
    audio_file = aclip.find("file")
    assert audio_file is not None and audio_file.get("id") == first_file.get("id") and len(audio_file) == 0
    markers = [(m.findtext("name"), m.findtext("in"), m.findtext("out")) for m in seq.findall("marker")]
    assert markers == [("Intro", "0", "-1"), ("Boss fight", "375", "-1"), ("Late", "1950", "-1")]


def test_chapter_lists(tmp_path: Path):
    _marked_timeline(tmp_path)
    txt = tmp_path / "m.chapters.txt"
    srt = tmp_path / "m.chapters.srt"
    assert tools.save_project(str(txt))["format"] == "chapters.txt"
    assert txt.read_text(encoding="utf-8") == "00:00 Intro\n00:12 Boss fight\n01:05 Late\n"
    assert tools.save_project(str(srt))["format"] == "chapters.srt"
    assert srt.read_text(encoding="utf-8") == (
        "1\n00:00:00,000 --> 00:00:12,500\nIntro\n\n"
        "2\n00:00:12,500 --> 00:01:05,000\nBoss fight\n\n"
        "3\n00:01:05,000 --> 00:01:06,000\nLate\n"
    )


def test_exports_without_markers_are_unchanged_shape(tmp_path: Path):
    _otio()
    tools.new_project(discard_unsaved=True)
    tools.add_track("video", "V1")
    tools.add_video_event(0, str(tmp_path / "a.mp4"), 0.0, 4.0)
    mlt = tmp_path / "plain.mlt"
    assert tools.save_project(str(mlt))["markers"] == 0
    assert "shotcut:markers" not in mlt.read_text(encoding="utf-8")
    txt = tmp_path / "plain.chapters.txt"
    assert tools.save_project(str(txt))["ok"] is True
    assert txt.read_text(encoding="utf-8") == ""


def test_fcpxml_markers_off_primary_clips_land_on_gaps(tmp_path: Path):
    import xml.etree.ElementTree as ET

    _otio()
    tools.new_project(discard_unsaved=True)
    tools.set_project_video(1920, 1080, 30)
    tools.add_track("video", "V1")
    tools.add_track("video", "V2")
    tools.add_video_event(0, str(tmp_path / "a.mp4"), 5.0, 5.0)
    tools.add_video_event(0, str(tmp_path / "b.mp4"), 15.0, 5.0)
    tools.add_video_event(1, str(tmp_path / "c.mp4"), 0.0, 3.0)
    tools.add_marker(1.0, "Before first clip, over V2")
    tools.add_marker(7.0, "On clip a")
    tools.add_marker(12.0, "In the hole")
    tools.add_marker(30.0, "After the end")
    dest = tmp_path / "gaps.fcpxml"
    saved = tools.save_project(str(dest))
    assert saved["ok"] is True
    spine = ET.parse(dest).getroot().find(".//spine")
    assert spine is not None
    layout = [(el.tag, el.get("offset"), el.get("duration")) for el in spine]
    assert layout == [
        ("gap", "0/30s", "150/30s"),
        ("asset-clip", "150/30s", "150/30s"),
        ("gap", "300/30s", "150/30s"),
        ("asset-clip", "450/30s", "150/30s"),
        ("gap", "600/30s", "301/30s"),
    ]
    placed = [[(m.get("value"), m.get("start")) for m in el.findall("chapter-marker")] for el in spine]
    assert placed == [
        [("Before first clip, over V2", "30/30s")],
        [("On clip a", "60/30s")],
        [("In the hole", "60/30s")],
        [],
        [("After the end", "300/30s")],
    ]
    # Every marker the save reports is in the file.
    assert saved["markers"] == len(list(spine.iter("chapter-marker"))) == 4


def test_media_urls_are_well_formed_file_urls(tmp_path: Path):
    import xml.etree.ElementTree as ET

    from rushcut.backends.otio import _file_url

    assert _file_url(r"C:\Media\My Clip.mp4") == "file:///C:/Media/My%20Clip.mp4"
    assert _file_url("D:/Edits/a.mp4") == "file:///D:/Edits/a.mp4"
    assert _file_url(r"\\nas\share\a.mp4") == "file://nas/share/a.mp4"
    assert _file_url("/srv/media/a b.mp4") == "file:///srv/media/a%20b.mp4"
    assert _file_url("file:///already/a.mp4") == "file:///already/a.mp4"

    _otio()
    tools.new_project(discard_unsaved=True)
    tools.set_project_video(1920, 1080, 30)
    tools.add_track("video", "V1")
    tools.add_video_event(0, r"C:\Media\clip.mp4", 0.0, 2.0)
    tools.add_video_event(0, str(tmp_path / "posix.mp4"), 2.0, 2.0)
    xmeml = tmp_path / "u.xmeml"
    assert tools.save_project(str(xmeml))["ok"] is True
    urls = [el.text for el in ET.parse(xmeml).getroot().iter("pathurl")]
    assert urls == ["file:///C:/Media/clip.mp4", "file://" + str(tmp_path / "posix.mp4")]
    fcp = tmp_path / "u.fcpxml"
    assert tools.save_project(str(fcp))["ok"] is True
    srcs = [el.get("src") for el in ET.parse(fcp).getroot().iter("asset")]
    assert srcs[0] == "file:///C:/Media/clip.mp4"
    otio = tmp_path / "u.otio"
    assert tools.save_project(str(otio))["ok"] is True
    clips = json.loads(otio.read_text(encoding="utf-8"))["tracks"]["children"][0]["children"]
    refs = [c["media_reference"]["target_url"] for c in clips if c["OTIO_SCHEMA"] == "Clip.1"]
    assert refs[0] == "file:///C:/Media/clip.mp4"
