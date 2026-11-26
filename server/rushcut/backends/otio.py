"""In-memory interchange backend: OpenTimelineIO JSON, FCPXML, MLT XML.

No NLE required. `project.save` writes a file the target editor can import:

- `.otio` — OpenTimelineIO (Resolve Studio *and* free Resolve, Premiere adapters)
- `.fcpxml` — Final Cut Pro XML 1.9 (free Resolve ImportTimelineFromFile, FCP)
- `.mlt` / `.kdenlive` — MLT XML (Shotcut, Kdenlive, `melt` render)
- `.xmeml` — Final Cut Pro 7 XML (Premiere, Resolve, older NLEs import it)
- `.chapters.txt` / `.chapters.srt` — the marker list as chapters

Timeline markers are written in each format's native marker element.

This is the free-friendly path when Resolve Studio is not available.
"""
from __future__ import annotations

import json
import os
import re
import uuid
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import quote
from xml.dom import minidom

from .protocol import capability_sheet, fail, ok, refuse_discard, unsupported

_MUTATING_METHODS = {
    "track.add",
    "track.set_composite_level",
    "track.set_props",
    "track.clear_events",
    "media.import",
    "media.place",
    "media.replace",
    "event.add_video",
    "event.add_audio",
    "event.add_title",
    "event.trim",
    "event.move",
    "event.delete",
    "event.set_motion",
    "event.set_fades",
    "event.set_opacity",
    "event.set_gain",
    "envelope.set_points",
    "envelope.clear",
    "marker.add",
    "marker.delete",
    "project.set_video",
}


class OtioBackend:
    name = "otio"

    def __init__(self) -> None:
        self.last_mode = "memory"
        self._reset()

    def _reset(self) -> None:
        self.width = 1920
        self.height = 1080
        self.fps = 24.0
        self.project_name = "Untitled"
        self.path: str | None = None
        self.tracks: list[dict[str, Any]] = []
        self.media: list[dict[str, Any]] = []
        self.markers: list[dict[str, Any]] = []
        self._selected: list[dict[str, Any]] = []
        self.created_by_rushcut = False
        self.host_dirty = False
        self.host_edit_count = 0
        self.project_id = uuid.uuid4().hex

    def ensure(self, project_path: str | None = None) -> dict[str, Any]:
        self.last_mode = "memory"
        if project_path:
            return self._open({"path": project_path, "discard_unsaved": True})
        return ok(editor=self.name, used_mode="memory", note="in-memory interchange; no NLE")

    def capabilities(self) -> dict[str, Any]:
        sheet = capability_sheet(
            self.name,
            live=False,
            notes=(
                "In-memory timeline. project.save writes .otio / .fcpxml / .mlt. "
                "Free Resolve can import OTIO and FCPXML. No transport or render."
            ),
            transport_play=False,
            transport_seek=False,
            motion_keyframes="metadata",
            fades="metadata",
            envelopes="metadata",
            titles=True,
            gain=True,
            alpha=False,
            render=False,
            markers=True,
        )
        sheet["studio_required"] = False
        sheet["platforms"] = ["windows", "macos", "linux"]
        sheet["export_formats"] = ["otio", "fcpxml", "mlt", "xmeml", "chapters.txt", "chapters.srt"]
        return sheet

    def info(self) -> dict[str, Any]:
        return ok(
            host="otio",
            mode="memory",
            editor=self.name,
            product="OpenTimelineIO / FCPXML / MLT interchange",
            studio=False,
            version=1,
        )

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = params or {}
        handler = {
            "ping": lambda _p: ok(host="otio", mode="memory", editor=self.name),
            "host.info": lambda _p: self.info(),
            "editor.info": lambda _p: self.info(),
            "editor.capabilities": lambda _p: self.capabilities(),
            "project.get_state": self._get_state,
            "project.get_selected_events": lambda _p: ok(events=list(self._selected)),
            "project.save": self._save,
            "project.new": self._new,
            "project.open": self._open,
            "project.set_video": self._set_video,
            "track.add": self._add_track,
            "track.set_composite_level": self._set_composite,
            "track.set_props": self._set_track_props,
            "track.clear_events": self._clear_track,
            "media.import": self._media_import,
            "media.place": self._place,
            "media.list": lambda _p: ok(media=list(self.media), count=len(self.media)),
            "media.replace": self._media_replace,
            "media.set_alpha": lambda _p: unsupported("media.set_alpha", self.name),
            "event.add_video": lambda p: self._place({**p, "type": "video"}),
            "event.add_audio": lambda p: self._place({**p, "type": "audio"}),
            "event.add_title": self._add_title,
            "event.trim": self._trim,
            "event.move": self._move,
            "event.delete": self._delete,
            "event.set_motion": self._set_motion,
            "event.set_fades": self._set_fades,
            "event.set_opacity": self._set_opacity,
            "event.set_gain": self._set_gain,
            "envelope.set_points": self._envelope_points,
            "envelope.clear": self._envelope_clear,
            "transport.play": lambda _p: unsupported("transport.play", self.name, "interchange only"),
            "transport.stop": lambda _p: unsupported("transport.stop", self.name, "interchange only"),
            "transport.seek": lambda _p: unsupported("transport.seek", self.name, "interchange only"),
            "render.list_templates": lambda _p: ok(templates=[], count=0, note="no renderer; export the timeline"),
            "render.start": lambda _p: unsupported(
                "render.start",
                self.name,
                "Export with project.save, then render in the target editor (or melt for .mlt).",
            ),
            "render.status": lambda _p: fail("no render job", editor=self.name),
            "marker.add": self._marker_add,
            "marker.list": lambda _p: ok(markers=list(self.markers), count=len(self.markers)),
            "marker.delete": self._marker_delete,
        }.get(method)
        if handler is None:
            return fail(f"Method not found: {method}", code=-32601, editor=self.name)
        result = handler(params)
        if isinstance(result, dict) and result.get("ok") and method in _MUTATING_METHODS:
            self.host_dirty = True
            self.host_edit_count += 1
        if isinstance(result, dict):
            result = dict(result)
            result.setdefault("editor", self.name)
            result.setdefault("used_mode", "memory")
        return result

    # ----- project ----------------------------------------------------
    def _get_state(self, _p: dict[str, Any]) -> dict[str, Any]:
        events = []
        tracks = []
        vcount = acount = 0
        end = 0.0
        for i, tr in enumerate(self.tracks):
            if tr["type"] == "video":
                vcount += 1
            elif tr["type"] == "audio":
                acount += 1
            tracks.append(
                {
                    "index": i,
                    "track_index": i,
                    "type": tr["type"],
                    "name": tr["name"],
                    "mute": tr.get("mute", False),
                }
            )
            for ei, ev in enumerate(tr["events"]):
                events.append(
                    {
                        "track_index": i,
                        "event_index": ei,
                        "start_seconds": ev["start_seconds"],
                        "length_seconds": ev["length_seconds"],
                        "media_path": ev.get("media_path"),
                        "media_name": ev.get("name"),
                        "take_offset_seconds": ev.get("take_offset_seconds", 0.0),
                    }
                )
                end = max(end, ev["start_seconds"] + ev["length_seconds"])
        return ok(
            length_seconds=end,
            video_track_count=vcount,
            audio_track_count=acount,
            tracks=tracks,
            events=events,
            project_name=self.project_name,
            fps=self.fps,
            width=self.width,
            height=self.height,
            project_id=self.project_id,
            created_by_rushcut=self.created_by_rushcut,
            dirty=self.host_dirty,
            host_edit_count=self.host_edit_count,
            untitled=not bool(self.path),
            project_path=self.path,
            track_count=len(self.tracks),
            event_count=len(events),
        )

    def _content_counts(self) -> tuple[int, int]:
        events = sum(len(tr.get("events") or []) for tr in self.tracks)
        return len(self.tracks), events

    def _guard_discard(self, p: dict[str, Any]) -> dict[str, Any] | None:
        tracks, events = self._content_counts()
        error = refuse_discard(
            track_count=tracks,
            event_count=events,
            untitled=not bool(self.path),
            created_by_rushcut=self.created_by_rushcut,
            project_id=self.project_id,
            discard_unsaved=bool(p.get("discard_unsaved")),
            save_current_to=p.get("save_current_to"),
            requested_project_id=p.get("project_id"),
            host_dirty=self.host_dirty,
        )
        if error:
            return fail(
                error,
                editor=self.name,
                track_count=tracks,
                event_count=events,
                project_id=self.project_id,
            )
        return None

    def _new(self, p: dict[str, Any]) -> dict[str, Any]:
        blocked = self._guard_discard(p)
        if blocked:
            return blocked
        if p.get("save_current_to"):
            saved = self._save({"path": p["save_current_to"]})
            if not saved.get("ok"):
                return saved
        self._reset()
        self.created_by_rushcut = True
        self.host_dirty = False
        self.host_edit_count = 0
        self.project_name = p.get("name") or "Untitled"
        return ok(project_name=self.project_name, project_id=self.project_id, created_by_rushcut=True)

    def _open(self, p: dict[str, Any]) -> dict[str, Any]:
        path = p.get("path")
        if not path:
            return fail("path is required", editor=self.name)
        if not os.path.isfile(path):
            return fail(f"file not found: {path}", editor=self.name)
        blocked = self._guard_discard(p)
        if blocked:
            return blocked
        if os.path.splitext(path)[1].lower() != ".otio":
            return fail("otio backend can only re-open .otio files it wrote", editor=self.name)
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        imported = data.get("metadata", {}).get("director")
        if not imported:
            return fail("otio file has no director metadata; open a file written by this backend", editor=self.name)
        self.width = int(imported.get("width") or 1920)
        self.height = int(imported.get("height") or 1080)
        self.fps = float(imported.get("fps") or 24)
        self.project_name = imported.get("project_name") or "Untitled"
        self.tracks = imported.get("tracks") or []
        self.media = imported.get("media") or []
        self.markers = imported.get("markers") or []
        self.path = path
        self.created_by_rushcut = False
        self.host_dirty = False
        self.host_edit_count = 0
        self.project_id = os.path.abspath(path)
        return ok(path=path, project_name=self.project_name, project_id=self.project_id)

    def _set_video(self, p: dict[str, Any]) -> dict[str, Any]:
        if p.get("width") is not None:
            self.width = int(p["width"])
        if p.get("height") is not None:
            self.height = int(p["height"])
        if p.get("frame_rate") is not None:
            self.fps = float(p["frame_rate"])
        return ok(width=self.width, height=self.height, frame_rate=self.fps)

    def _save(self, p: dict[str, Any]) -> dict[str, Any]:
        path = p.get("path") or self.path
        if not path:
            return fail("path is required (use .otio, .fcpxml, .mlt, or .kdenlive)", editor=self.name)
        ext = os.path.splitext(path)[1].lower()
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        lower = path.lower()
        if lower.endswith(".chapters.txt"):
            text = to_chapters_txt(self)
            ext = ".chapters.txt"
        elif lower.endswith(".chapters.srt"):
            text = to_chapters_srt(self)
            ext = ".chapters.srt"
        elif ext in {".mlt", ".kdenlive"}:
            text = to_mlt(self)
        elif ext == ".xmeml":
            text = to_xmeml(self)
        elif ext in {".fcpxml", ".xml"}:
            text = to_fcpxml(self)
        else:
            text = json.dumps(to_otio(self), indent=2)
            if not text.endswith("\n"):
                text += "\n"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        self.path = path
        return ok(path=path, format=ext.lstrip(".") or "otio", markers=len(self.markers))

    # ----- tracks / media / events ------------------------------------
    def _add_track(self, p: dict[str, Any]) -> dict[str, Any]:
        kind = (p.get("type") or "video").lower()
        if kind not in {"video", "audio", "subtitle"}:
            return fail("type must be video, audio, or subtitle", editor=self.name)
        track = {
            "type": kind,
            "name": p.get("name") or f"{kind[0].upper()}{len(self.tracks) + 1}",
            "mute": False,
            "solo": False,
            "volume": 1.0,
            "composite_level": 1.0,
            "envelope": [],
            "events": [],
        }
        index = p.get("index")
        if index is None or int(index) >= len(self.tracks):
            self.tracks.append(track)
            idx = len(self.tracks) - 1
        else:
            idx = max(0, int(index))
            self.tracks.insert(idx, track)
        return ok(track_index=idx, type=kind, name=track["name"])

    def _track(self, index: int) -> dict[str, Any] | None:
        if 0 <= index < len(self.tracks):
            return self.tracks[index]
        return None

    def _set_composite(self, p: dict[str, Any]) -> dict[str, Any]:
        tr = self._track(int(p["track_index"]))
        if tr is None:
            return fail("track not found", editor=self.name)
        tr["composite_level"] = float(p["level"])
        return ok(track_index=p["track_index"], level=tr["composite_level"])

    def _set_track_props(self, p: dict[str, Any]) -> dict[str, Any]:
        tr = self._track(int(p["track_index"]))
        if tr is None:
            return fail("track not found", editor=self.name)
        for key in ("name", "mute", "solo", "volume"):
            if p.get(key) is not None:
                tr[key] = p[key]
        return ok(track_index=p["track_index"])

    def _clear_track(self, p: dict[str, Any]) -> dict[str, Any]:
        tr = self._track(int(p["track_index"]))
        if tr is None:
            return fail("track not found", editor=self.name)
        n = len(tr["events"])
        tr["events"] = []
        return ok(track_index=p["track_index"], deleted=n)

    def _media_import(self, p: dict[str, Any]) -> dict[str, Any]:
        path = p.get("path")
        if not path:
            return fail("path is required", editor=self.name)
        rec = {"path": path, "name": os.path.basename(path)}
        self.media.append(rec)
        return ok(**rec)

    def _media_replace(self, p: dict[str, Any]) -> dict[str, Any]:
        old, new = p.get("old_path"), p.get("new_path")
        n = 0
        for m in self.media:
            if m.get("path") == old:
                m["path"] = new
                n += 1
        for tr in self.tracks:
            for ev in tr["events"]:
                if ev.get("media_path") == old:
                    ev["media_path"] = new
                    n += 1
        return ok(replaced=n, old_path=old, new_path=new)

    def _place(self, p: dict[str, Any]) -> dict[str, Any]:
        path = p.get("path") or p.get("media_path")
        if not path:
            return fail("path / media_path is required", editor=self.name)
        idx = int(p.get("track_index", 0))
        tr = self._track(idx)
        if tr is None:
            return fail("track not found", editor=self.name)
        want = p.get("type")
        if want and tr["type"] != want:
            return fail(f"track {idx} is {tr['type']}, expected {want}", editor=self.name)
        if path not in {m.get("path") for m in self.media}:
            self.media.append({"path": path, "name": os.path.basename(path)})
        ev = {
            "name": p.get("text") or os.path.basename(path),
            "media_path": path,
            "start_seconds": float(p.get("start_seconds") or 0.0),
            "length_seconds": float(p["length_seconds"]) if p.get("length_seconds") not in (None, -1) else 0.0,
            "take_offset_seconds": float(p.get("take_offset_seconds") or 0.0),
            "opacity": 1.0,
            "gain": 1.0,
            "motion": [],
            "fades": {},
            "kind": "title" if p.get("text") else "clip",
        }
        if ev["length_seconds"] < 0:
            ev["length_seconds"] = 0.0
        tr["events"].append(ev)
        tr["events"].sort(key=lambda e: e["start_seconds"])
        event_index = tr["events"].index(ev)
        self._selected = [{"track_index": idx, "event_index": event_index}]
        return ok(track_index=idx, event_index=event_index, start_seconds=ev["start_seconds"], length_seconds=ev["length_seconds"], media_path=path)

    def _add_title(self, p: dict[str, Any]) -> dict[str, Any]:
        return self._place({**p, "type": "video", "path": p.get("path") or f"title:{p.get('text') or 'Title'}", "text": p.get("text")})

    def _event(self, p: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        tr = self._track(int(p["track_index"]))
        if tr is None:
            return None, None
        ei = int(p["event_index"])
        if 0 <= ei < len(tr["events"]):
            return tr, tr["events"][ei]
        return tr, None

    def _trim(self, p: dict[str, Any]) -> dict[str, Any]:
        tr, ev = self._event(p)
        if ev is None:
            return fail("event not found", editor=self.name)
        if p.get("start_seconds") is not None:
            ev["start_seconds"] = float(p["start_seconds"])
        if p.get("length_seconds") is not None:
            ev["length_seconds"] = float(p["length_seconds"])
        if p.get("take_offset_seconds") is not None:
            ev["take_offset_seconds"] = float(p["take_offset_seconds"])
        return ok(track_index=p["track_index"], event_index=p["event_index"], **{k: ev[k] for k in ("start_seconds", "length_seconds", "take_offset_seconds")})

    def _move(self, p: dict[str, Any]) -> dict[str, Any]:
        return self._trim(p)

    def _delete(self, p: dict[str, Any]) -> dict[str, Any]:
        tr, ev = self._event(p)
        if ev is None or tr is None:
            return fail("event not found", editor=self.name)
        tr["events"].remove(ev)
        return ok(track_index=p["track_index"], event_index=p["event_index"])

    def _set_motion(self, p: dict[str, Any]) -> dict[str, Any]:
        _tr, ev = self._event(p)
        if ev is None:
            return fail("event not found", editor=self.name)
        ev["motion"] = list(p.get("keyframes") or [])
        return ok(keyframes=len(ev["motion"]), note="stored as clip metadata for OTIO/FCPXML export")

    def _set_fades(self, p: dict[str, Any]) -> dict[str, Any]:
        _tr, ev = self._event(p)
        if ev is None:
            return fail("event not found", editor=self.name)
        ev["fades"] = {
            "fade_in_seconds": p.get("fade_in_seconds"),
            "fade_out_seconds": p.get("fade_out_seconds"),
            "dissolve": p.get("dissolve"),
        }
        return ok(**ev["fades"])

    def _set_opacity(self, p: dict[str, Any]) -> dict[str, Any]:
        _tr, ev = self._event(p)
        if ev is None:
            return fail("event not found", editor=self.name)
        ev["opacity"] = float(p["opacity"])
        return ok(opacity=ev["opacity"])

    def _set_gain(self, p: dict[str, Any]) -> dict[str, Any]:
        _tr, ev = self._event(p)
        if ev is None:
            return fail("event not found", editor=self.name)
        if p.get("gain") is not None:
            ev["gain"] = float(p["gain"])
        elif p.get("gain_db") is not None:
            ev["gain"] = 10 ** (float(p["gain_db"]) / 20.0)
        ev["normalize"] = p.get("normalize")
        return ok(gain=ev["gain"])

    def _envelope_points(self, p: dict[str, Any]) -> dict[str, Any]:
        tr = self._track(int(p["track_index"]))
        if tr is None:
            return fail("track not found", editor=self.name)
        tr["envelope"] = list(p.get("points") or [])
        return ok(points_applied=len(tr["envelope"]), envelope_type=p.get("envelope_type"))

    def _envelope_clear(self, p: dict[str, Any]) -> dict[str, Any]:
        tr = self._track(int(p["track_index"]))
        if tr is None:
            return fail("track not found", editor=self.name)
        tr["envelope"] = []
        return ok(envelope_type=p.get("envelope_type"))

    def _marker_add(self, p: dict[str, Any]) -> dict[str, Any]:
        rec = {
            "at_seconds": float(p.get("at_seconds") or 0),
            "name": p.get("name") or "marker",
            "note": p.get("note") or "",
            "color": p.get("color") or "Green",
        }
        self.markers.append(rec)
        return ok(**rec)

    def _marker_delete(self, p: dict[str, Any]) -> dict[str, Any]:
        if p.get("at_seconds") is not None:
            at = float(p["at_seconds"])
            self.markers = [m for m in self.markers if abs(m["at_seconds"] - at) > 1e-6]
        elif p.get("name"):
            self.markers = [m for m in self.markers if m.get("name") != p["name"]]
        else:
            self.markers = []
        return ok(count=len(self.markers))


def _rational(seconds: float, fps: float) -> dict[str, Any]:
    return {"OTIO_SCHEMA": "RationalTime.1", "value": float(seconds) * float(fps), "rate": float(fps)}


def to_otio(state: OtioBackend) -> dict[str, Any]:
    """OTIO JSON 1.x. Includes director metadata so we can re-open it."""
    tracks = []
    for tr in state.tracks:
        children: list[dict[str, Any]] = []
        cursor = 0.0
        for ev in sorted(tr["events"], key=lambda e: e["start_seconds"]):
            if ev["start_seconds"] > cursor + 1e-6:
                gap = ev["start_seconds"] - cursor
                children.append(
                    {
                        "OTIO_SCHEMA": "Gap.1",
                        "name": "gap",
                        "source_range": {
                            "OTIO_SCHEMA": "TimeRange.1",
                            "start_time": _rational(0, state.fps),
                            "duration": _rational(gap, state.fps),
                        },
                    }
                )
            media_ref: dict[str, Any]
            if str(ev.get("media_path") or "").startswith("title:"):
                media_ref = {"OTIO_SCHEMA": "GeneratorReference.1", "name": ev.get("name") or "Title", "generator_kind": "title"}
            else:
                url = ev.get("media_path") or ""
                if url:
                    url = _file_url(url)
                media_ref = {"OTIO_SCHEMA": "ExternalReference.1", "target_url": url, "name": ev.get("name")}
            children.append(
                {
                    "OTIO_SCHEMA": "Clip.1",
                    "name": ev.get("name"),
                    "source_range": {
                        "OTIO_SCHEMA": "TimeRange.1",
                        "start_time": _rational(ev.get("take_offset_seconds") or 0, state.fps),
                        "duration": _rational(ev["length_seconds"], state.fps),
                    },
                    "media_reference": media_ref,
                    "metadata": {"director": {"motion": ev.get("motion"), "fades": ev.get("fades"), "opacity": ev.get("opacity")}},
                }
            )
            cursor = ev["start_seconds"] + ev["length_seconds"]
        tracks.append(
            {
                "OTIO_SCHEMA": "Track.1",
                "name": tr["name"],
                "kind": "Video" if tr["type"] == "video" else "Audio" if tr["type"] == "audio" else "Subtitle",
                "children": children,
            }
        )
    return {
        "OTIO_SCHEMA": "Timeline.1",
        "name": state.project_name,
        "global_start_time": _rational(0, state.fps),
        "tracks": {
            "OTIO_SCHEMA": "Stack.1",
            "name": "tracks",
            "children": tracks,
            "markers": [_otio_marker(m, state.fps) for m in _sorted_markers(state)],
        },
        "metadata": {
            "director": {
                "width": state.width,
                "height": state.height,
                "fps": state.fps,
                "project_name": state.project_name,
                "tracks": state.tracks,
                "media": state.media,
                "markers": state.markers,
            }
        },
    }


def to_mlt(state: OtioBackend) -> str:
    root = ET.Element("mlt", {"LC_NUMERIC": "C", "version": "7.0.0", "title": state.project_name})
    ET.SubElement(
        root,
        "profile",
        {
            "width": str(state.width),
            "height": str(state.height),
            "frame_rate_num": str(int(round(state.fps))),
            "frame_rate_den": "1",
            "sample_aspect_num": "1",
            "sample_aspect_den": "1",
            "display_aspect_num": str(state.width),
            "display_aspect_den": str(state.height),
        },
    )
    producers: dict[str, str] = {}
    n = 0
    playlists = []
    for tr in state.tracks:
        pl_id = f"playlist_{len(playlists)}"
        playlist = ET.SubElement(root, "playlist", {"id": pl_id})
        cursor = 0.0
        for ev in sorted(tr["events"], key=lambda e: e["start_seconds"]):
            if ev["start_seconds"] > cursor + 1e-6:
                blank = int(round((ev["start_seconds"] - cursor) * state.fps))
                ET.SubElement(playlist, "blank", {"length": str(max(0, blank))})
            path = ev.get("media_path") or ""
            if path not in producers:
                pid = f"producer{n}"
                n += 1
                producers[path] = pid
                prod = ET.SubElement(root, "producer", {"id": pid})
                ET.SubElement(prod, "property", {"name": "resource"}).text = path
            in_f = int(round((ev.get("take_offset_seconds") or 0) * state.fps))
            out_f = in_f + max(0, int(round(ev["length_seconds"] * state.fps)) - 1)
            ET.SubElement(playlist, "entry", {"producer": producers[path], "in": str(in_f), "out": str(out_f)})
            cursor = ev["start_seconds"] + ev["length_seconds"]
        playlists.append(pl_id)
    tractor = ET.SubElement(root, "tractor", {"id": "tractor0"})
    markers = _sorted_markers(state)
    if markers:
        block = ET.SubElement(tractor, "properties", {"name": "shotcut:markers"})
        for i, m in enumerate(markers):
            entry = ET.SubElement(block, "properties", {"name": str(i)})
            at = _clock(float(m.get("at_seconds") or 0), millis=True)
            ET.SubElement(entry, "property", {"name": "text"}).text = str(m.get("name") or "marker")
            ET.SubElement(entry, "property", {"name": "start"}).text = at
            ET.SubElement(entry, "property", {"name": "end"}).text = at
            color = _HEX_COLORS.get(str(m.get("color") or "green").lower(), "#00c000")
            ET.SubElement(entry, "property", {"name": "color"}).text = color
    for pl in playlists:
        ET.SubElement(tractor, "track", {"producer": pl})
    return _xml(root)


def to_fcpxml(state: OtioBackend) -> str:
    """Minimal FCPXML 1.9. Free Resolve's ImportTimelineFromFile accepts this family."""
    fcpxml = ET.Element("fcpxml", {"version": "1.9"})
    resources = ET.SubElement(fcpxml, "resources")
    frame_den = int(round(state.fps))
    ET.SubElement(
        resources,
        "format",
        {
            "id": "r1",
            "name": f"FFVideoFormat{state.height}p{frame_den}",
            "frameDuration": f"{1}/{frame_den}s",
            "width": str(state.width),
            "height": str(state.height),
        },
    )
    assets: dict[str, str] = {}
    n = 2
    for m in state.media:
        path = m.get("path") or ""
        if not path or path in assets or path.startswith("title:"):
            continue
        aid = f"r{n}"
        n += 1
        assets[path] = aid
        src = _file_url(path)
        ET.SubElement(
            resources,
            "asset",
            {"id": aid, "name": m.get("name") or os.path.basename(path), "src": src, "hasVideo": "1", "hasAudio": "1"},
        )
    library = ET.SubElement(fcpxml, "library")
    event = ET.SubElement(library, "event", {"name": "director"})
    project = ET.SubElement(event, "project", {"name": state.project_name})
    seq = ET.SubElement(project, "sequence", {"format": "r1"})
    spine = ET.SubElement(seq, "spine")
    # FCPXML spine is a single storyline; flatten video track 0 then the rest as connected clips.
    video_tracks = [tr for tr in state.tracks if tr["type"] == "video"]
    primary = video_tracks[0]["events"] if video_tracks else []
    pending = _sorted_markers(state)
    frame = 1.0 / state.fps

    def _at(m: dict[str, Any]) -> float:
        return max(0.0, float(m.get("at_seconds") or 0))

    def _place(parent: ET.Element, lo: float, hi: float, origin: float, source_start: float) -> None:
        """Write the pending markers in [lo, hi) onto *parent* (a clip or gap), in its own time.

        *origin* is the timeline time of the element's first frame, which
        sits at *source_start* in the element's own time.
        """
        nonlocal pending
        inside = [m for m in pending if lo <= _at(m) < hi]
        for m in inside:
            local = source_start + max(0.0, _at(m) - origin)
            ET.SubElement(
                parent,
                "chapter-marker",
                {
                    "start": _fcp_time(local, state.fps),
                    "duration": _fcp_time(frame, state.fps),
                    "value": str(m.get("name") or "marker"),
                },
            )
        pending = [m for m in pending if m not in inside]

    def _gap(lo: float, hi: float) -> None:
        gap = ET.SubElement(
            spine,
            "gap",
            {
                "name": "Gap",
                "offset": _fcp_time(lo, state.fps),
                "duration": _fcp_time(hi - lo, state.fps),
                "start": "0s",
            },
        )
        _place(gap, lo, hi, lo, 0.0)

    cursor = 0.0
    for ev in sorted(primary, key=lambda e: e["start_seconds"]):
        clip_start = float(ev["start_seconds"])
        clip_end = clip_start + float(ev["length_seconds"])
        lo = clip_start
        if clip_start - cursor >= frame / 2:
            # Empty time before this clip: a gap holds it, and any markers in it.
            _gap(cursor, clip_start)
        elif clip_start > cursor:
            lo = cursor  # sub-frame hole: its markers go on this clip's first frame
        ref = assets.get(ev.get("media_path") or "")
        attrib = {
            "name": ev.get("name") or "clip",
            "offset": _fcp_time(ev["start_seconds"], state.fps),
            "duration": _fcp_time(ev["length_seconds"], state.fps),
            "start": _fcp_time(ev.get("take_offset_seconds") or 0, state.fps),
        }
        if ref:
            attrib["ref"] = ref
            clip = ET.SubElement(spine, "asset-clip", attrib)
        else:
            clip = ET.SubElement(spine, "title", attrib)
            ET.SubElement(clip, "text").text = ev.get("name") or ""
        # Marker start is in the clip's own source time.
        _place(clip, lo, clip_end, clip_start, float(ev.get("take_offset_seconds") or 0))
        cursor = max(cursor, clip_end)
    if pending:
        # Markers past the last primary clip (or on an empty spine) go on a trailing gap.
        _gap(cursor, max(_at(m) for m in pending) + frame)
    return _xml(fcpxml, doctype='<!DOCTYPE fcpxml>')


# ----- markers -----------------------------------------------------------
# OTIO marker colors are an upper-case enum; anything else maps to GREEN.
_OTIO_COLORS = {"PINK", "RED", "ORANGE", "YELLOW", "GREEN", "CYAN", "BLUE", "PURPLE", "MAGENTA", "BLACK", "WHITE"}
_HEX_COLORS = {
    "pink": "#ff66cc", "red": "#ff0000", "orange": "#ff8000", "yellow": "#ffff00", "green": "#00c000",
    "cyan": "#00ffff", "blue": "#0000ff", "purple": "#8000ff", "magenta": "#ff00ff", "black": "#000000",
    "white": "#ffffff",
}


def _sorted_markers(state: OtioBackend) -> list[dict[str, Any]]:
    return sorted(state.markers, key=lambda m: (float(m.get("at_seconds") or 0), str(m.get("name") or "")))


def _otio_marker(m: dict[str, Any], fps: float) -> dict[str, Any]:
    color = str(m.get("color") or "Green").upper()
    return {
        "OTIO_SCHEMA": "Marker.2",
        "name": m.get("name") or "marker",
        "comment": m.get("note") or "",
        "color": color if color in _OTIO_COLORS else "GREEN",
        "marked_range": {
            "OTIO_SCHEMA": "TimeRange.1",
            "start_time": _rational(float(m.get("at_seconds") or 0), fps),
            "duration": _rational(0, fps),
        },
        "metadata": {},
    }


def _clock(seconds: float, *, millis: bool, sep: str = ".") -> str:
    total_ms = int(round(max(0.0, float(seconds)) * 1000))
    h, rem = divmod(total_ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    if millis:
        return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def to_chapters_txt(state: OtioBackend) -> str:
    """One `MM:SS Name` line per marker (the YouTube description format)."""
    lines = [f"{_clock(float(m.get('at_seconds') or 0), millis=False)} {m.get('name') or 'marker'}" for m in _sorted_markers(state)]
    return "\n".join(lines) + ("\n" if lines else "")


def to_chapters_srt(state: OtioBackend) -> str:
    """Each marker as an SRT cue running to the next marker (or the timeline end)."""
    markers = _sorted_markers(state)
    end_of_timeline = 0.0
    for tr in state.tracks:
        for ev in tr["events"]:
            end_of_timeline = max(end_of_timeline, float(ev["start_seconds"]) + float(ev["length_seconds"]))
    blocks = []
    for i, m in enumerate(markers):
        start = float(m.get("at_seconds") or 0)
        if i + 1 < len(markers):
            end = float(markers[i + 1].get("at_seconds") or 0)
        else:
            end = max(end_of_timeline, start + 1.0)
        if end <= start:
            end = start + 1.0
        text = str(m.get("name") or "marker")
        if m.get("note"):
            text += "\n" + str(m["note"])
        blocks.append(f"{i + 1}\n{_clock(start, millis=True, sep=',')} --> {_clock(end, millis=True, sep=',')}\n{text}\n")
    return "\n".join(blocks)


def _xmeml_rate(parent: ET.Element, fps: float) -> None:
    rate = ET.SubElement(parent, "rate")
    timebase = int(round(fps))
    ET.SubElement(rate, "timebase").text = str(timebase)
    ntsc = abs(fps - timebase) > 1e-3
    ET.SubElement(rate, "ntsc").text = "TRUE" if ntsc else "FALSE"


def to_xmeml(state: OtioBackend) -> str:
    """Final Cut Pro 7 XML (xmeml v5). Premiere Pro and Resolve import it."""
    fps = state.fps
    root = ET.Element("xmeml", {"version": "5"})
    seq = ET.SubElement(root, "sequence", {"id": "sequence-1"})
    ET.SubElement(seq, "name").text = state.project_name
    end_frames = 0
    for tr in state.tracks:
        for ev in tr["events"]:
            end_frames = max(end_frames, int(round((ev["start_seconds"] + ev["length_seconds"]) * fps)))
    ET.SubElement(seq, "duration").text = str(end_frames)
    _xmeml_rate(seq, fps)
    media = ET.SubElement(seq, "media")
    video = ET.SubElement(media, "video")
    fmt = ET.SubElement(video, "format")
    chars = ET.SubElement(fmt, "samplecharacteristics")
    _xmeml_rate(chars, fps)
    ET.SubElement(chars, "width").text = str(state.width)
    ET.SubElement(chars, "height").text = str(state.height)
    audio = ET.SubElement(media, "audio")

    files: dict[str, str] = {}
    n_clip = 0
    for tr in state.tracks:
        if tr["type"] not in {"video", "audio"}:
            continue
        track = ET.SubElement(video if tr["type"] == "video" else audio, "track")
        for ev in sorted(tr["events"], key=lambda e: e["start_seconds"]):
            path = str(ev.get("media_path") or "")
            n_clip += 1
            start_f = int(round(ev["start_seconds"] * fps))
            length_f = max(1, int(round(ev["length_seconds"] * fps)))
            in_f = int(round(float(ev.get("take_offset_seconds") or 0) * fps))
            if path.startswith("title:"):
                item = ET.SubElement(track, "generatoritem", {"id": f"generatoritem-{n_clip}"})
                ET.SubElement(item, "name").text = ev.get("name") or "Title"
            else:
                item = ET.SubElement(track, "clipitem", {"id": f"clipitem-{n_clip}"})
                ET.SubElement(item, "name").text = ev.get("name") or os.path.basename(path) or "clip"
            ET.SubElement(item, "duration").text = str(in_f + length_f)
            _xmeml_rate(item, fps)
            ET.SubElement(item, "start").text = str(start_f)
            ET.SubElement(item, "end").text = str(start_f + length_f)
            ET.SubElement(item, "in").text = str(in_f)
            ET.SubElement(item, "out").text = str(in_f + length_f)
            if path.startswith("title:"):
                continue
            if path in files:
                ET.SubElement(item, "file", {"id": files[path]})
                continue
            fid = f"file-{len(files) + 1}"
            files[path] = fid
            file_el = ET.SubElement(item, "file", {"id": fid})
            ET.SubElement(file_el, "name").text = os.path.basename(path) or "media"
            url = _file_url(path)
            ET.SubElement(file_el, "pathurl").text = url
            _xmeml_rate(file_el, fps)
            file_media = ET.SubElement(file_el, "media")
            ET.SubElement(file_media, "video")
            ET.SubElement(file_media, "audio")

    for m in _sorted_markers(state):
        marker = ET.SubElement(seq, "marker")
        ET.SubElement(marker, "name").text = m.get("name") or "marker"
        ET.SubElement(marker, "comment").text = m.get("note") or ""
        ET.SubElement(marker, "in").text = str(int(round(float(m.get("at_seconds") or 0) * fps)))
        ET.SubElement(marker, "out").text = "-1"
    return _xml(root, doctype="<!DOCTYPE xmeml>")


_WIN_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")


def _file_url(path: str) -> str:
    """A ``file://`` URL for a media path: ``/a/b`` -> ``file:///a/b``, ``C:\\a`` -> ``file:///C:/a``.

    Windows paths are kept as written even when running on POSIX. UNC paths
    (``\\\\host\\share\\a``) become ``file://host/share/a``.
    """
    if path.startswith("file:"):
        return path
    unc = path.startswith("\\\\") or (os.name == "nt" and path.startswith("//"))
    p = path if unc or _WIN_DRIVE.match(path) else os.path.abspath(path)
    p = p.replace("\\", "/")
    if unc:
        return "file:" + quote(p, safe="/:")
    if not p.startswith("/"):
        p = "/" + p
    return "file://" + quote(p, safe="/:")


def _fcp_time(seconds: float, fps: float) -> str:
    frames = int(round(float(seconds) * float(fps)))
    den = int(round(float(fps)))
    return f"{frames}/{den}s"


def _xml(root: ET.Element, doctype: str | None = None) -> str:
    raw = ET.tostring(root, encoding="utf-8")
    pretty = minidom.parseString(raw).toprettyxml(indent="  ", encoding="utf-8").decode("utf-8")
    if doctype and "<!DOCTYPE" not in pretty:
        pretty = pretty.replace("<?xml version=\"1.0\" encoding=\"utf-8\"?>", "<?xml version=\"1.0\" encoding=\"utf-8\"?>\n" + doctype, 1)
    return pretty
