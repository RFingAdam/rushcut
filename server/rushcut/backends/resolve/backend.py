"""Resolve backend: same RPC names as the VEGAS host, via DaVinciResolveScript."""
from __future__ import annotations

import os
import time
from typing import Any, Callable

from ..protocol import capability_sheet, fail, ok, unsupported
from . import mapping as M
from .connect import (
    FREE_FRIENDLY_HINT,
    STUDIO_ONLY_FEATURES,
    connect_resolve,
    free_edition_error,
    launch_resolve,
    product_is_studio,
    read_product,
    read_version,
    scripting_unavailable_error,
)

Handler = Callable[[dict[str, Any]], dict[str, Any]]


class ResolveBackend:
    name = "resolve"

    def __init__(
        self,
        app: Any | None = None,
        connector: Callable[[], Any] | None = None,
        launcher: Callable[..., dict[str, Any]] | None = None,
        auto_launch: bool = True,
    ):
        self._injected = app
        self._app = app
        self._connector = connector or connect_resolve
        self._launcher = launcher or launch_resolve
        self._auto_launch = auto_launch
        self.last_mode = "live" if app is not None else "none"
        self._render_jobs: dict[str, dict[str, Any]] = {}

    # ----- connection -------------------------------------------------
    def _resolve(self) -> Any | None:
        if self._injected is not None:
            return self._injected
        if self._app is not None:
            return self._app
        self._app = self._connector()
        return self._app

    def _gate(self, app: Any | None) -> dict[str, Any] | None:
        """Refuse free Resolve and a missing scripting attach."""
        if app is None:
            return scripting_unavailable_error(used_mode=self.last_mode or "none")
        product = read_product(app)
        version = read_version(app)
        if not product_is_studio(product):
            return free_edition_error(product, version, used_mode=self.last_mode or "live")
        return None

    def ensure(self, project_path: str | None = None) -> dict[str, Any]:
        app = self._resolve()
        blocked = self._gate(app)
        if app is not None:
            if blocked:
                self.last_mode = "none"
                return blocked
            self.last_mode = "live"
            return ok(
                editor=self.name,
                used_mode="live",
                product=read_product(app),
                studio=True,
                version=read_version(app),
            )
        if blocked and not self._auto_launch:
            self.last_mode = "none"
            return blocked
        if not self._auto_launch:
            self.last_mode = "none"
            return scripting_unavailable_error(used_mode="none")
        launched = self._launcher()
        if not launched.get("ok"):
            self.last_mode = "none"
            err = scripting_unavailable_error(used_mode="none")
            err["launch"] = launched
            return err
        wait = float(os.environ.get("RESOLVE_LAUNCH_WAIT", "60"))
        deadline = time.time() + wait
        while time.time() < deadline:
            self._app = None
            app = self._resolve()
            blocked = self._gate(app)
            if app is not None:
                if blocked:
                    self.last_mode = "none"
                    return blocked
                self.last_mode = "launched_resolve"
                out = ok(
                    editor=self.name,
                    used_mode="launched_resolve",
                    launched=True,
                    product=read_product(app),
                    studio=True,
                    version=read_version(app),
                )
                if project_path:
                    opened = self._open_project(project_path)
                    out["opened"] = opened
                return out
            time.sleep(0.5)
        self.last_mode = "none"
        return scripting_unavailable_error(used_mode="none", launch=launched)

    def _need_app(self) -> tuple[Any | None, dict[str, Any] | None]:
        app = self._resolve()
        blocked = self._gate(app)
        if blocked:
            if app is None and self._auto_launch:
                ensured = self.ensure()
                if not ensured.get("ok"):
                    return None, ensured
                app = self._resolve()
                blocked = self._gate(app)
                if blocked:
                    return None, blocked
            else:
                return None, blocked
        if app is None:
            return None, scripting_unavailable_error(used_mode=self.last_mode or "none")
        return app, None

    def _ctx(self) -> tuple[Any, Any, Any, Any] | dict[str, Any]:
        app, err = self._need_app()
        if err:
            return err
        try:
            pm = app.GetProjectManager()
            project = pm.GetCurrentProject() if pm else None
        except Exception as exc:
            return fail(f"Resolve project manager: {exc}", editor=self.name)
        if project is None:
            return fail("No active Resolve project", editor=self.name)
        try:
            pool = project.GetMediaPool()
            timeline = project.GetCurrentTimeline()
        except Exception as exc:
            return fail(f"Resolve project: {exc}", editor=self.name)
        return app, pm, project, pool, timeline  # type: ignore[return-value]

    # ----- dispatch ---------------------------------------------------
    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = params or {}
        handler = self._handlers().get(method)
        if handler is None:
            return fail(f"Method not found: {method}", code=-32601, editor=self.name)
        try:
            result = handler(params)
        except Exception as exc:
            return fail(str(exc), editor=self.name, method=method)
        if isinstance(result, dict):
            result = dict(result)
            result.setdefault("editor", self.name)
            result.setdefault("used_mode", self.last_mode if self.last_mode != "none" else "live")
        return result

    def capabilities(self) -> dict[str, Any]:
        sheet = capability_sheet(
            self.name,
            live=True,
            notes=(
                "DaVinciResolveScript / fusionscript. Live control needs Resolve "
                "Studio 21.1+ and External scripting = Local. Free Resolve has had "
                "no Scripting API since 21.1. "
                + FREE_FRIENDLY_HINT
            ),
            transport_play=False,
            transport_seek=True,
            motion_keyframes="partial",
            fades="partial",
            envelopes="partial",
            titles=True,
            gain=False,
            alpha="partial",
            render=True,
            markers=True,
        )
        sheet["studio_required"] = True
        sheet["min_studio_version"] = "21.1+"
        sheet["studio_only_features"] = list(STUDIO_ONLY_FEATURES)
        sheet["platforms"] = ["windows", "macos", "linux"]
        app = self._resolve()
        if app is not None:
            sheet["product"] = read_product(app)
            sheet["studio"] = product_is_studio(sheet["product"])
            sheet["version"] = read_version(app)
        return sheet

    def info(self) -> dict[str, Any]:
        return self._host_info({})

    def _handlers(self) -> dict[str, Handler]:
        return {
            "ping": self._ping,
            "host.info": self._host_info,
            "editor.info": self._host_info,
            "editor.capabilities": lambda _p: self.capabilities(),
            "project.get_state": self._get_state,
            "project.get_selected_events": self._selected,
            "project.save": self._save,
            "project.new": self._new_project,
            "project.open": self._open,
            "project.set_video": self._set_video,
            "track.add": self._add_track,
            "track.set_composite_level": self._set_composite,
            "track.set_props": self._set_track_props,
            "track.clear_events": self._clear_track,
            "media.import": self._media_import,
            "media.place": self._media_place,
            "media.list": self._media_list,
            "media.replace": self._media_replace,
            "media.set_alpha": self._media_alpha,
            "event.add_video": self._add_video,
            "event.add_audio": self._add_audio,
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
            "transport.play": self._play,
            "transport.stop": self._stop,
            "transport.seek": self._seek,
            "render.list_templates": self._list_templates,
            "render.start": self._render_start,
            "render.status": self._render_status,
            "marker.add": self._marker_add,
            "marker.list": self._marker_list,
            "marker.delete": self._marker_delete,
            "resolve.create_subtitles": self._create_subtitles,
            "resolve.smart_reframe": self._smart_reframe,
            "resolve.set_voice_isolation": self._voice_isolation,
        }

    # ----- helpers ----------------------------------------------------
    def _unpack(self) -> tuple[Any, Any, Any, Any, Any] | dict[str, Any]:
        ctx = self._ctx()
        if isinstance(ctx, dict):
            return ctx
        return ctx  # app, pm, project, pool, timeline

    def _require_timeline(self, p: dict[str, Any]) -> tuple[Any, Any, Any, Any, Any] | dict[str, Any]:
        ctx = self._unpack()
        if isinstance(ctx, dict):
            return ctx
        app, pm, project, pool, timeline = ctx
        if timeline is None:
            try:
                timeline = pool.CreateEmptyTimeline(p.get("timeline_name") or "Timeline 1")
                if timeline:
                    project.SetCurrentTimeline(timeline)
            except Exception:
                timeline = None
        if timeline is None:
            return fail("No current Resolve timeline. Create one in the Edit page.", editor=self.name)
        return app, pm, project, pool, timeline

    # ----- health -----------------------------------------------------
    def _ping(self, _p: dict[str, Any]) -> dict[str, Any]:
        app, err = self._need_app()
        if err:
            return err
        return ok(
            host="resolve-director",
            mode="external",
            editor=self.name,
            product=read_product(app),
            studio=True,
        )

    def _host_info(self, _p: dict[str, Any]) -> dict[str, Any]:
        app, err = self._need_app()
        if err:
            return err
        product = read_product(app)
        return ok(
            host="resolve-director",
            mode="external",
            editor=self.name,
            product=product,
            studio=True,
            version=read_version(app),
        )

    # ----- project ----------------------------------------------------
    def _get_state(self, _p: dict[str, Any]) -> dict[str, Any]:
        ctx = self._require_timeline({})
        if isinstance(ctx, dict):
            # Empty project with no timeline is still a valid "empty" state.
            if ctx.get("error") == "No active Resolve project":
                return ctx
            app, err = self._need_app()
            if err:
                return err
            pm = app.GetProjectManager()
            project = pm.GetCurrentProject() if pm else None
            if project is None:
                return fail("No active Resolve project", editor=self.name)
            return ok(
                length_seconds=0,
                video_track_count=0,
                audio_track_count=0,
                tracks=[],
                events=[],
                project_name=project.GetName() if project else None,
            )
        _app, _pm, project, _pool, timeline = ctx
        fps = M.timeline_fps(timeline, project)
        origin = M.start_frame(timeline)
        try:
            end = int(timeline.GetEndFrame() or origin)
        except Exception:
            end = origin
        refs = M.list_tracks(timeline)
        tracks = []
        events = []
        vcount = acount = 0
        for ref in refs:
            if ref.kind == "video":
                vcount += 1
            elif ref.kind == "audio":
                acount += 1
            muted = False
            try:
                muted = not bool(timeline.GetIsTrackEnabled(ref.kind, ref.resolve_index))
            except Exception:
                pass
            tracks.append(
                {
                    "index": ref.flat_index,
                    "track_index": ref.flat_index,
                    "type": ref.kind,
                    "name": ref.name,
                    "mute": muted,
                    "resolve_index": ref.resolve_index,
                }
            )
            for ei, item in enumerate(sorted(M.items_on(timeline, ref), key=lambda it: _start(it))):
                start = _start(item)
                duration = _duration(item)
                events.append(
                    {
                        "track_index": ref.flat_index,
                        "event_index": ei,
                        "start_seconds": M.frame_to_seconds(start, fps, origin),
                        "length_seconds": duration / fps if fps else 0.0,
                        "media_path": M.clip_file_path(item),
                        "media_name": _name(item),
                        "take_offset_seconds": _take_offset(item, fps),
                    }
                )
        return ok(
            length_seconds=M.frame_to_seconds(end, fps, origin),
            video_track_count=vcount,
            audio_track_count=acount,
            tracks=tracks,
            events=events,
            project_name=project.GetName() if project else None,
            timeline_name=timeline.GetName() if timeline else None,
            fps=fps,
        )

    def _selected(self, _p: dict[str, Any]) -> dict[str, Any]:
        ctx = self._require_timeline({})
        if isinstance(ctx, dict):
            return ctx
        _app, _pm, _project, _pool, timeline = ctx
        try:
            current = timeline.GetCurrentVideoItem()
        except Exception:
            current = None
        if current is None:
            return ok(events=[])
        fps = M.timeline_fps(timeline, _project)
        origin = M.start_frame(timeline)
        for ref in M.list_tracks(timeline):
            for ei, item in enumerate(sorted(M.items_on(timeline, ref), key=lambda it: _start(it))):
                if item is current or _name(item) == _name(current) and _start(item) == _start(current):
                    return ok(
                        events=[
                            {
                                "track_index": ref.flat_index,
                                "event_index": ei,
                                "start_seconds": M.frame_to_seconds(_start(item), fps, origin),
                                "length_seconds": _duration(item) / fps if fps else 0.0,
                            }
                        ]
                    )
        return ok(events=[])

    def _save(self, p: dict[str, Any]) -> dict[str, Any]:
        app, err = self._need_app()
        if err:
            return err
        pm = app.GetProjectManager()
        project = pm.GetCurrentProject() if pm else None
        if project is None:
            return fail("No active Resolve project", editor=self.name)
        path = p.get("path")
        if path:
            try:
                exported = pm.ExportProject(project.GetName(), path, True)
            except Exception as exc:
                return fail(f"ExportProject: {exc}", editor=self.name)
            if not exported:
                return fail("ExportProject returned false", editor=self.name, path=path)
            return ok(path=path, exported=True)
        try:
            saved = pm.SaveProject()
        except Exception as exc:
            return fail(str(exc), editor=self.name)
        if not saved:
            return fail("SaveProject returned false", editor=self.name)
        return ok()

    def _new_project(self, p: dict[str, Any]) -> dict[str, Any]:
        app, err = self._need_app()
        if err:
            return err
        pm = app.GetProjectManager()
        current = pm.GetCurrentProject() if pm else None
        if current is not None:
            if p.get("save_current_to"):
                try:
                    pm.ExportProject(current.GetName(), p["save_current_to"], True)
                except Exception as exc:
                    return fail(f"could not save current project: {exc}", editor=self.name)
            elif not p.get("discard_unsaved"):
                return fail(
                    "Resolve already has a project open. Pass discard_unsaved=True or save_current_to.",
                    editor=self.name,
                )
            try:
                pm.CloseProject(current)
            except Exception:
                pass
        name = p.get("name") or f"director-{int(time.time())}"
        try:
            project = pm.CreateProject(name)
        except Exception as exc:
            return fail(str(exc), editor=self.name)
        if project is None:
            return fail(f"CreateProject({name!r}) failed — name may already exist", editor=self.name)
        pool = project.GetMediaPool()
        try:
            timeline = pool.CreateEmptyTimeline("Timeline 1")
            if timeline:
                project.SetCurrentTimeline(timeline)
        except Exception:
            timeline = None
        return ok(project_name=name, timeline=bool(timeline))

    def _open(self, p: dict[str, Any]) -> dict[str, Any]:
        path = p.get("path")
        if not path:
            return fail("path is required", editor=self.name)
        return self._open_project(path, save_current_to=p.get("save_current_to"), discard=p.get("discard_unsaved"))

    def _open_project(
        self,
        path: str,
        save_current_to: str | None = None,
        discard: bool | None = None,
    ) -> dict[str, Any]:
        app, err = self._need_app()
        if err:
            return err
        pm = app.GetProjectManager()
        current = pm.GetCurrentProject() if pm else None
        if current is not None:
            if save_current_to:
                pm.ExportProject(current.GetName(), save_current_to, True)
            elif not discard:
                return fail(
                    "Resolve already has a project open. Pass discard_unsaved=True or save_current_to.",
                    editor=self.name,
                )
            try:
                pm.CloseProject(current)
            except Exception:
                pass
        ext = os.path.splitext(path)[1].lower()
        if ext in {".drp", ".dra"}:
            imported = pm.ImportProject(path)
            if not imported:
                return fail(f"ImportProject failed: {path}", editor=self.name)
            # Import loads it in some versions; try LoadProject with basename too.
            name = os.path.splitext(os.path.basename(path))[0]
            loaded = pm.GetCurrentProject() or pm.LoadProject(name)
            return ok(path=path, project_name=loaded.GetName() if loaded else name)
        loaded = pm.LoadProject(path)
        if loaded is None:
            return fail(f"LoadProject failed: {path}", editor=self.name)
        return ok(path=path, project_name=loaded.GetName())

    def _set_video(self, p: dict[str, Any]) -> dict[str, Any]:
        ctx = self._unpack()
        if isinstance(ctx, dict):
            return ctx
        _app, _pm, project, pool, timeline = ctx
        applied: dict[str, Any] = {}
        if p.get("width") is not None:
            project.SetSetting("timelineResolutionWidth", str(int(p["width"])))
            applied["width"] = int(p["width"])
        if p.get("height") is not None:
            project.SetSetting("timelineResolutionHeight", str(int(p["height"])))
            applied["height"] = int(p["height"])
        if p.get("frame_rate") is not None:
            project.SetSetting("timelineFrameRate", str(p["frame_rate"]))
            applied["frame_rate"] = p["frame_rate"]
        if timeline is None:
            try:
                timeline = pool.CreateEmptyTimeline("Timeline 1")
                if timeline:
                    project.SetCurrentTimeline(timeline)
            except Exception:
                pass
        if timeline is not None:
            if p.get("width") is not None:
                try:
                    timeline.SetSetting("timelineResolutionWidth", str(int(p["width"])))
                except Exception:
                    pass
            if p.get("height") is not None:
                try:
                    timeline.SetSetting("timelineResolutionHeight", str(int(p["height"])))
                except Exception:
                    pass
        return ok(**applied)

    # ----- tracks -----------------------------------------------------
    def _add_track(self, p: dict[str, Any]) -> dict[str, Any]:
        ctx = self._require_timeline(p)
        if isinstance(ctx, dict):
            return ctx
        _app, _pm, _project, _pool, timeline = ctx
        kind = (p.get("type") or "video").lower()
        if kind not in {"video", "audio", "subtitle"}:
            return fail("type must be video, audio, or subtitle", editor=self.name)
        before = M.list_tracks(timeline)
        try:
            if kind == "audio":
                added = timeline.AddTrack("audio", p.get("sub_type") or "stereo")
            else:
                added = timeline.AddTrack(kind)
        except TypeError:
            added = timeline.AddTrack(kind)
        if not added:
            return fail("AddTrack returned false", editor=self.name)
        after = M.list_tracks(timeline)
        new_refs = [r for r in after if (r.kind, r.resolve_index) not in {(x.kind, x.resolve_index) for x in before}]
        ref = new_refs[-1] if new_refs else (after[-1] if after else None)
        name = p.get("name") or ""
        if ref and name:
            try:
                timeline.SetTrackName(ref.kind, ref.resolve_index, name)
                ref.name = name
            except Exception:
                pass
        # Resolve appends tracks; optional insert index is not in the public API.
        note = None
        if p.get("index") is not None and ref and int(p["index"]) != ref.flat_index:
            note = "Resolve AddTrack always appends; index was not honored."
        return ok(
            track_index=ref.flat_index if ref else len(after) - 1,
            type=kind,
            name=ref.name if ref else name,
            note=note,
        )

    def _set_composite(self, p: dict[str, Any]) -> dict[str, Any]:
        ctx = self._require_timeline(p)
        if isinstance(ctx, dict):
            return ctx
        _app, _pm, _project, _pool, timeline = ctx
        ref = M.track_at(timeline, int(p["track_index"]))
        if ref is None:
            return fail("track not found", editor=self.name)
        level = float(p["level"])
        opacity = max(0.0, min(100.0, level * 100.0))
        n = 0
        for item in M.items_on(timeline, ref):
            try:
                item.SetProperty("Opacity", opacity)
                n += 1
            except Exception:
                pass
        return ok(
            track_index=ref.flat_index,
            level=level,
            clips=n,
            note="Resolve has no track CompositeLevel; opacity was applied to each clip.",
            partial=True,
        )

    def _set_track_props(self, p: dict[str, Any]) -> dict[str, Any]:
        ctx = self._require_timeline(p)
        if isinstance(ctx, dict):
            return ctx
        _app, _pm, _project, _pool, timeline = ctx
        ref = M.track_at(timeline, int(p["track_index"]))
        if ref is None:
            return fail("track not found", editor=self.name)
        if p.get("name") is not None:
            timeline.SetTrackName(ref.kind, ref.resolve_index, str(p["name"]))
        if p.get("mute") is not None:
            timeline.SetTrackEnable(ref.kind, ref.resolve_index, not bool(p["mute"]))
        note = None
        if p.get("solo") is not None:
            note = "Resolve scripting has no track solo."
        if p.get("volume") is not None:
            note = (note + " " if note else "") + "Track volume is Fairlight-only; not set."
        return ok(track_index=ref.flat_index, note=note, partial=bool(note))

    def _clear_track(self, p: dict[str, Any]) -> dict[str, Any]:
        ctx = self._require_timeline(p)
        if isinstance(ctx, dict):
            return ctx
        _app, _pm, _project, _pool, timeline = ctx
        ref = M.track_at(timeline, int(p["track_index"]))
        if ref is None:
            return fail("track not found", editor=self.name)
        items = M.items_on(timeline, ref)
        if items:
            timeline.DeleteClips(items, False)
        return ok(track_index=ref.flat_index, deleted=len(items))

    # ----- media / events ---------------------------------------------
    def _import_clip(self, pool: Any, path: str) -> Any | None:
        imported = pool.ImportMedia([path])
        if not imported:
            return None
        if isinstance(imported, dict):
            imported = list(imported.values())
        return imported[0] if imported else None

    def _media_import(self, p: dict[str, Any]) -> dict[str, Any]:
        ctx = self._unpack()
        if isinstance(ctx, dict):
            return ctx
        _app, _pm, _project, pool, _timeline = ctx
        path = p.get("path")
        if not path:
            return fail("path is required", editor=self.name)
        clip = self._import_clip(pool, path)
        if clip is None:
            return fail(f"ImportMedia failed: {path}", editor=self.name)
        return ok(path=path, name=_name(clip))

    def _media_list(self, _p: dict[str, Any]) -> dict[str, Any]:
        ctx = self._unpack()
        if isinstance(ctx, dict):
            return ctx
        _app, _pm, _project, pool, _timeline = ctx
        root = pool.GetRootFolder() if pool else None
        items = []
        for clip in M.iter_folder_clips(root):
            path = None
            try:
                props = clip.GetClipProperty() or {}
                if isinstance(props, dict):
                    path = props.get("File Path") or props.get("Clip Path")
            except Exception:
                props = {}
            items.append(
                {
                    "name": _name(clip),
                    "path": path,
                    "offline": bool(isinstance(props, dict) and str(props.get("Online", "Online")).lower() == "offline"),
                    "length_seconds": _clip_length_seconds(clip),
                }
            )
        return ok(media=items, count=len(items))

    def _media_replace(self, p: dict[str, Any]) -> dict[str, Any]:
        ctx = self._unpack()
        if isinstance(ctx, dict):
            return ctx
        _app, _pm, _project, pool, _timeline = ctx
        old = p.get("old_path")
        new = p.get("new_path")
        if not old or not new:
            return fail("old_path and new_path are required", editor=self.name)
        n = 0
        for clip in M.iter_folder_clips(pool.GetRootFolder()):
            path = None
            try:
                props = clip.GetClipProperty() or {}
                path = props.get("File Path") if isinstance(props, dict) else None
            except Exception:
                path = None
            if path and os.path.normcase(str(path)) == os.path.normcase(str(old)):
                if clip.ReplaceClip(new):
                    n += 1
        if n == 0:
            return fail(f"no media pool item matched {old}", editor=self.name)
        return ok(replaced=n, old_path=old, new_path=new)

    def _media_alpha(self, p: dict[str, Any]) -> dict[str, Any]:
        ctx = self._unpack()
        if isinstance(ctx, dict):
            return ctx
        _app, _pm, _project, pool, _timeline = ctx
        alpha = p.get("alpha") or "Straight"
        mapping = {
            "straight": "Straight",
            "premultiplied": "Premultiplied",
            "none": "None",
            "undefined": "None",
        }
        value = mapping.get(str(alpha).lower(), str(alpha))
        n = 0
        needle = (p.get("path") or p.get("path_contains") or "").lower()
        for clip in M.iter_folder_clips(pool.GetRootFolder()):
            path = ""
            try:
                props = clip.GetClipProperty() or {}
                path = str((props or {}).get("File Path") or "")
            except Exception:
                pass
            if needle and needle not in path.lower() and needle not in _name(clip).lower():
                continue
            try:
                if clip.SetClipProperty("Alpha mode", value):
                    n += 1
            except Exception:
                continue
        if n == 0:
            return fail(
                "Could not set Alpha mode (clip attribute name varies by Resolve version).",
                editor=self.name,
                partial=True,
            )
        return ok(updated=n, alpha=value)

    def _place_on_track(
        self,
        p: dict[str, Any],
        *,
        media_type: int | None,
        require_kind: str | None,
    ) -> dict[str, Any]:
        ctx = self._require_timeline(p)
        if isinstance(ctx, dict):
            return ctx
        _app, _pm, project, pool, timeline = ctx
        path = p.get("path") or p.get("media_path")
        if not path:
            return fail("path / media_path is required", editor=self.name)
        ref = M.track_at(timeline, int(p.get("track_index", 0)))
        if ref is None:
            return fail("track not found", editor=self.name)
        if require_kind and ref.kind != require_kind:
            return fail(f"track {ref.flat_index} is {ref.kind}, expected {require_kind}", editor=self.name)
        clip = self._import_clip(pool, path)
        if clip is None:
            return fail(f"ImportMedia failed: {path}", editor=self.name)
        fps = M.timeline_fps(timeline, project)
        origin = M.start_frame(timeline)
        start_s = float(p.get("start_seconds") or 0.0)
        length_s = float(p.get("length_seconds") if p.get("length_seconds") is not None else -1)
        take = float(p.get("take_offset_seconds") or 0.0)
        src_len = _clip_length_seconds(clip)
        if length_s < 0:
            length_s = max(0.0, src_len - take) if src_len else 0.0
        info: dict[str, Any] = {
            "mediaPoolItem": clip,
            "trackIndex": ref.resolve_index,
            "recordFrame": M.seconds_to_frame(start_s, fps, origin),
        }
        if media_type is not None:
            info["mediaType"] = media_type
        elif ref.kind == "audio":
            info["mediaType"] = 2
        elif ref.kind == "video":
            info["mediaType"] = 1
        src_fps = fps
        info["startFrame"] = M.seconds_to_frame(take, src_fps, 0)
        if length_s > 0:
            info["endFrame"] = M.seconds_to_frame(take + length_s, src_fps, 0)
        try:
            placed = pool.AppendToTimeline([info])
        except Exception as exc:
            return fail(f"AppendToTimeline: {exc}", editor=self.name)
        if not placed:
            return fail("AppendToTimeline returned nothing", editor=self.name)
        items = M.items_on(timeline, ref)
        items.sort(key=lambda it: _start(it))
        return ok(
            track_index=ref.flat_index,
            event_index=max(0, len(items) - 1),
            start_seconds=start_s,
            length_seconds=length_s,
            media_path=path,
        )

    def _media_place(self, p: dict[str, Any]) -> dict[str, Any]:
        return self._place_on_track(p, media_type=None, require_kind=None)

    def _add_video(self, p: dict[str, Any]) -> dict[str, Any]:
        return self._place_on_track(p, media_type=1, require_kind="video")

    def _add_audio(self, p: dict[str, Any]) -> dict[str, Any]:
        return self._place_on_track(p, media_type=2, require_kind="audio")

    def _add_title(self, p: dict[str, Any]) -> dict[str, Any]:
        ctx = self._require_timeline(p)
        if isinstance(ctx, dict):
            return ctx
        app, _pm, project, _pool, timeline = ctx
        fps = M.timeline_fps(timeline, project)
        origin = M.start_frame(timeline)
        start_s = float(p.get("start_seconds") or 0.0)
        length_s = float(p.get("length_seconds") or 3.0)
        text = p.get("text") or ""
        try:
            app.OpenPage("edit")
        except Exception:
            pass
        try:
            timeline.SetCurrentTimecode(M.frames_to_timecode(M.seconds_to_frame(start_s, fps, origin), fps))
        except Exception:
            pass
        item = None
        preset = p.get("preset") or "Text+"
        for name in (preset, "Text+", "Text", "Titles"):
            try:
                item = timeline.InsertFusionTitleIntoTimeline(name)
            except Exception:
                item = None
            if item:
                break
            try:
                item = timeline.InsertTitleIntoTimeline(name)
            except Exception:
                item = None
            if item:
                break
        if item is None:
            return fail(
                "InsertFusionTitleIntoTimeline failed. Open the Edit page and confirm Titles are installed.",
                editor=self.name,
            )
        _set_title_text(item, text)
        return ok(
            track_index=int(p.get("track_index", 0)),
            start_seconds=start_s,
            length_seconds=length_s,
            text=text,
            actual_start_seconds=M.frame_to_seconds(_start(item), fps, origin),
            actual_length_seconds=_duration(item) / fps if fps else None,
            note="Title is inserted at the playhead; trim/move if Resolve used a default duration.",
        )

    def _trim(self, p: dict[str, Any]) -> dict[str, Any]:
        return self._replace_item(p, move_start=p.get("start_seconds"), new_length=p.get("length_seconds"))

    def _move(self, p: dict[str, Any]) -> dict[str, Any]:
        if p.get("start_seconds") is None:
            return fail("start_seconds is required", editor=self.name)
        return self._replace_item(p, move_start=p.get("start_seconds"), new_length=None)

    def _delete(self, p: dict[str, Any]) -> dict[str, Any]:
        ctx = self._require_timeline(p)
        if isinstance(ctx, dict):
            return ctx
        _app, _pm, _project, _pool, timeline = ctx
        ref, item = M.item_at(timeline, int(p["track_index"]), int(p["event_index"]))
        if item is None:
            return fail("event not found", editor=self.name)
        timeline.DeleteClips([item], False)
        return ok(track_index=ref.flat_index if ref else p["track_index"], event_index=p["event_index"])

    def _replace_item(
        self,
        p: dict[str, Any],
        *,
        move_start: Any,
        new_length: Any,
    ) -> dict[str, Any]:
        ctx = self._require_timeline(p)
        if isinstance(ctx, dict):
            return ctx
        _app, _pm, project, pool, timeline = ctx
        ref, item = M.item_at(timeline, int(p["track_index"]), int(p["event_index"]))
        if item is None or ref is None:
            return fail("event not found", editor=self.name)
        fps = M.timeline_fps(timeline, project)
        origin = M.start_frame(timeline)
        old_start_s = M.frame_to_seconds(_start(item), fps, origin)
        old_len_s = _duration(item) / fps if fps else 0.0
        start_s = float(move_start) if move_start is not None else old_start_s
        length_s = float(new_length) if new_length is not None else old_len_s
        take = p.get("take_offset_seconds")
        if take is None:
            take = _take_offset(item, fps)
        mpi = None
        try:
            mpi = item.GetMediaPoolItem()
        except Exception:
            mpi = None
        if mpi is None:
            return fail("clip has no media pool item (generator/title?); cannot retime via API", editor=self.name)
        media_type = 2 if ref.kind == "audio" else 1
        timeline.DeleteClips([item], False)
        info = {
            "mediaPoolItem": mpi,
            "trackIndex": ref.resolve_index,
            "mediaType": media_type,
            "recordFrame": M.seconds_to_frame(start_s, fps, origin),
            "startFrame": M.seconds_to_frame(float(take), fps, 0),
            "endFrame": M.seconds_to_frame(float(take) + length_s, fps, 0),
        }
        placed = pool.AppendToTimeline([info])
        if not placed:
            return fail("re-append after trim/move failed", editor=self.name)
        return ok(
            track_index=ref.flat_index,
            event_index=int(p["event_index"]),
            start_seconds=start_s,
            length_seconds=length_s,
            take_offset_seconds=float(take),
        )

    def _set_motion(self, p: dict[str, Any]) -> dict[str, Any]:
        ctx = self._require_timeline(p)
        if isinstance(ctx, dict):
            return ctx
        _app, _pm, project, _pool, timeline = ctx
        ref, item = M.item_at(timeline, int(p["track_index"]), int(p["event_index"]))
        if item is None:
            return fail("event not found", editor=self.name)
        keys = list(p.get("keyframes") or [])
        if p.get("reset"):
            try:
                item.SetProperty({"ZoomX": 1.0, "ZoomY": 1.0, "Pan": 0.0, "Tilt": 0.0})
            except Exception:
                pass
        if not keys:
            return ok(keyframes=0, partial=True, note="No keyframes; inspector reset only.")
        # Public API has no inspector keyframe curve. Apply the last keyframe
        # as Zoom/Pan.
        last = keys[-1]
        scale = float(last.get("scale") or 1.0)
        pan_x = float(last.get("pan_x") or 0.0)
        pan_y = float(last.get("pan_y") or 0.0)
        width = _setting_int(project, "timelineResolutionWidth", 1920)
        height = _setting_int(project, "timelineResolutionHeight", 1080)
        try:
            item.SetProperty(
                {
                    "ZoomX": scale,
                    "ZoomY": scale,
                    "ZoomGang": True,
                    "Pan": pan_x * (width / 2.0),
                    "Tilt": pan_y * (height / 2.0),
                }
            )
        except Exception as exc:
            return fail(f"SetProperty motion: {exc}", editor=self.name)
        return ok(
            keyframes=len(keys),
            applied="last",
            scale=scale,
            partial=True,
            note="Resolve inspector Zoom/Pan is a single value; only the last keyframe is applied.",
        )

    def _set_fades(self, p: dict[str, Any]) -> dict[str, Any]:
        # No clip fade-handle API. Overlap (apply_crossfade) still works via move.
        return ok(
            track_index=p.get("track_index"),
            event_index=p.get("event_index"),
            fade_in_seconds=p.get("fade_in_seconds"),
            fade_out_seconds=p.get("fade_out_seconds"),
            dissolve=p.get("dissolve"),
            partial=True,
            note=(
                "Resolve has no clip fade-handle scripting API. "
                "Overlap clips with apply_crossfade / event.move and add a "
                "Cross Dissolve on the Edit page."
            ),
        )

    def _set_opacity(self, p: dict[str, Any]) -> dict[str, Any]:
        ctx = self._require_timeline(p)
        if isinstance(ctx, dict):
            return ctx
        _app, _pm, _project, _pool, timeline = ctx
        _ref, item = M.item_at(timeline, int(p["track_index"]), int(p["event_index"]))
        if item is None:
            return fail("event not found", editor=self.name)
        opacity = max(0.0, min(1.0, float(p["opacity"])))
        item.SetProperty("Opacity", opacity * 100.0)
        return ok(opacity=opacity)

    def _set_gain(self, p: dict[str, Any]) -> dict[str, Any]:
        return unsupported(
            "event.set_gain",
            self.name,
            "Fairlight clip gain is not in the public Python API.",
        )

    def _envelope_points(self, p: dict[str, Any]) -> dict[str, Any]:
        ctx = self._require_timeline(p)
        if isinstance(ctx, dict):
            return ctx
        _app, _pm, project, _pool, timeline = ctx
        fps = M.timeline_fps(timeline, project)
        origin = M.start_frame(timeline)
        points = list(p.get("points") or [])
        kind = p.get("envelope_type") or "Volume"
        n = 0
        for pt in points:
            frame = M.seconds_to_frame(float(pt.get("at_seconds") or 0), fps, origin)
            try:
                timeline.AddMarker(
                    frame,
                    "Blue",
                    f"{kind}={pt.get('value')}",
                    f"director envelope {kind}",
                    1,
                    "director-envelope",
                )
                n += 1
            except Exception:
                continue
        return ok(
            points_applied=n,
            envelope_type=kind,
            partial=True,
            note="Resolve has no track envelope API; points were stored as Blue timeline markers.",
        )

    def _envelope_clear(self, p: dict[str, Any]) -> dict[str, Any]:
        ctx = self._require_timeline(p)
        if isinstance(ctx, dict):
            return ctx
        _app, _pm, _project, _pool, timeline = ctx
        try:
            timeline.DeleteMarkerByCustomData("director-envelope")
        except Exception:
            pass
        return ok(envelope_type=p.get("envelope_type") or "Volume", partial=True)

    # ----- transport / render / markers -------------------------------
    def _play(self, _p: dict[str, Any]) -> dict[str, Any]:
        return unsupported("transport.play", self.name, "Resolve's public API has no Play().")

    def _stop(self, _p: dict[str, Any]) -> dict[str, Any]:
        return unsupported("transport.stop", self.name, "Resolve's public API has no Stop().")

    def _seek(self, p: dict[str, Any]) -> dict[str, Any]:
        ctx = self._require_timeline(p)
        if isinstance(ctx, dict):
            return ctx
        app, _pm, project, _pool, timeline = ctx
        fps = M.timeline_fps(timeline, project)
        origin = M.start_frame(timeline)
        seconds = float(p.get("seconds") or 0)
        tc = M.frames_to_timecode(M.seconds_to_frame(seconds, fps, origin), fps)
        try:
            app.OpenPage("edit")
        except Exception:
            pass
        ok_set = timeline.SetCurrentTimecode(tc)
        if not ok_set:
            return fail(f"SetCurrentTimecode({tc}) failed", editor=self.name)
        return ok(seconds=seconds, timecode=tc)

    def _list_templates(self, p: dict[str, Any]) -> dict[str, Any]:
        ctx = self._unpack()
        if isinstance(ctx, dict):
            return ctx
        _app, _pm, project, _pool, _timeline = ctx
        templates: list[dict[str, Any]] = [
            {"renderer": "mp4", "renderer_name": "mp4", "template": "H.264 Vertical 1080x1920", "width": 1080, "height": 1920},
            {"renderer": "mp4", "renderer_name": "mp4", "template": "H.264 1080p", "width": 1920, "height": 1080},
            {"renderer": "mp4", "renderer_name": "mp4", "template": "H.264 4K", "width": 3840, "height": 2160},
        ]
        try:
            presets = project.GetRenderPresetList() or []
        except Exception:
            presets = []
        if isinstance(presets, dict):
            presets = list(presets.values())
        for name in presets:
            if isinstance(name, dict):
                name = name.get("name") or name.get("Name") or str(name)
            templates.append({"renderer": "preset", "renderer_name": "preset", "template": str(name)})
        rf = (p.get("renderer_contains") or "").lower()
        tf = (p.get("template_contains") or "").lower()
        filtered = []
        for t in templates:
            blob = f"{t.get('renderer')} {t.get('template')}".lower()
            if rf and rf not in blob:
                continue
            if tf and tf not in (t.get("template") or "").lower():
                continue
            filtered.append(t)
        limit = int(p.get("limit") or 0)
        offset = int(p.get("offset") or 0)
        page = filtered[offset:] if limit <= 0 else filtered[offset : offset + limit]
        truncated = bool(limit > 0 and offset + limit < len(filtered))
        return ok(templates=page, count=len(page), offset=offset, limit=limit, truncated=truncated)

    def _render_start(self, p: dict[str, Any]) -> dict[str, Any]:
        ctx = self._require_timeline(p)
        if isinstance(ctx, dict):
            return ctx
        _app, _pm, project, _pool, timeline = ctx
        output = p.get("output_path")
        if not output:
            return fail("output_path is required", editor=self.name)
        target_dir = os.path.dirname(output) or os.getcwd()
        custom = os.path.splitext(os.path.basename(output))[0]
        fmt, codec = "mp4", "H264"
        width = height = None
        template = (p.get("template_name") or "").lower()
        if "1920" in template and "1080" in template and template.find("1920") < template.find("1080"):
            width, height = 1920, 1080
        elif "1080" in template and "1920" in template:
            width, height = 1080, 1920
        elif "3840" in template or "4k" in template or "2160" in template:
            width, height = 3840, 2160
        renderer = (p.get("renderer_name") or "").lower()
        if renderer and renderer not in {"mp4", "preset", ""}:
            fmt = renderer
        preset_name = p.get("template_name")
        if preset_name:
            try:
                project.LoadRenderPreset(preset_name)
            except Exception:
                pass
        try:
            project.SetCurrentRenderFormatAndCodec(fmt, codec)
        except Exception:
            pass
        settings: dict[str, Any] = {
            "TargetDir": target_dir,
            "CustomName": custom,
            "ExportVideo": True,
            "ExportAudio": True,
        }
        if width and height:
            settings["FormatWidth"] = width
            settings["FormatHeight"] = height
        if p.get("start_seconds") is not None or p.get("length_seconds") is not None:
            fps = M.timeline_fps(timeline, project)
            origin = M.start_frame(timeline)
            start_s = float(p.get("start_seconds") or 0)
            length_s = p.get("length_seconds")
            settings["SelectAllFrames"] = False
            settings["MarkIn"] = M.seconds_to_frame(start_s, fps, origin)
            if length_s is not None:
                settings["MarkOut"] = M.seconds_to_frame(start_s + float(length_s), fps, origin)
        else:
            settings["SelectAllFrames"] = True
        try:
            project.SetRenderSettings(settings)
        except Exception as exc:
            return fail(f"SetRenderSettings: {exc}", editor=self.name)
        try:
            job_id = project.AddRenderJob()
        except Exception as exc:
            return fail(f"AddRenderJob: {exc}", editor=self.name)
        if not job_id:
            return fail("AddRenderJob returned empty id", editor=self.name)
        started = True
        if p.get("async", True):
            try:
                started = bool(project.StartRendering(job_id))
            except TypeError:
                started = bool(project.StartRendering([job_id]))
        else:
            try:
                started = bool(project.StartRendering(job_id))
            except TypeError:
                started = bool(project.StartRendering([job_id]))
            # Blocking: poll here.
            deadline = time.time() + float(os.environ.get("RESOLVE_RENDER_WAIT", "1800"))
            while time.time() < deadline and project.IsRenderingInProgress():
                time.sleep(0.5)
        self._render_jobs[str(job_id)] = {"output_path": output, "settings": settings}
        return ok(
            job_id=str(job_id),
            state="running" if started else "queued",
            async_render=bool(p.get("async", True)),
            output_path=output,
        )

    def _render_status(self, p: dict[str, Any]) -> dict[str, Any]:
        ctx = self._unpack()
        if isinstance(ctx, dict):
            return ctx
        _app, _pm, project, _pool, _timeline = ctx
        job_id = p.get("job_id")
        if not job_id and self._render_jobs:
            job_id = next(reversed(self._render_jobs))
        if not job_id:
            busy = False
            try:
                busy = bool(project.IsRenderingInProgress())
            except Exception:
                pass
            return ok(state="idle" if not busy else "running", percent=0)
        try:
            info = project.GetRenderJobStatus(job_id) or {}
        except Exception as exc:
            return fail(str(exc), editor=self.name, job_id=job_id)
        raw = str(info.get("JobStatus") or info.get("status") or info.get("state") or "")
        percent = info.get("CompletionPercentage") or info.get("percent") or 0
        state = _normalize_job_state(raw)
        remembered = self._render_jobs.get(str(job_id), {})
        return ok(
            job_id=str(job_id),
            state=state,
            percent=percent,
            output_path=remembered.get("output_path"),
            raw=info,
        )

    def _marker_add(self, p: dict[str, Any]) -> dict[str, Any]:
        ctx = self._require_timeline(p)
        if isinstance(ctx, dict):
            return ctx
        _app, _pm, project, _pool, timeline = ctx
        fps = M.timeline_fps(timeline, project)
        origin = M.start_frame(timeline)
        frame = M.seconds_to_frame(float(p.get("at_seconds") or 0), fps, origin)
        color = p.get("color") or "Green"
        name = p.get("name") or "marker"
        note = p.get("note") or ""
        duration = int(p.get("duration_frames") or 1)
        custom = p.get("custom_data") or ""
        added = timeline.AddMarker(frame, color, name, note, duration, custom)
        if not added:
            return fail("AddMarker returned false", editor=self.name)
        return ok(at_seconds=p.get("at_seconds") or 0, frame=frame, color=color, name=name)

    def _marker_list(self, _p: dict[str, Any]) -> dict[str, Any]:
        ctx = self._require_timeline(_p)
        if isinstance(ctx, dict):
            return ctx
        _app, _pm, project, _pool, timeline = ctx
        fps = M.timeline_fps(timeline, project)
        origin = M.start_frame(timeline)
        raw = {}
        try:
            raw = timeline.GetMarkers() or {}
        except Exception:
            raw = {}
        markers = []
        for frame, info in raw.items():
            info = info or {}
            markers.append(
                {
                    "frame": frame,
                    "at_seconds": M.frame_to_seconds(int(float(frame)), fps, origin),
                    "color": info.get("color"),
                    "name": info.get("name"),
                    "note": info.get("note"),
                    "custom_data": info.get("customData"),
                }
            )
        return ok(markers=markers, count=len(markers))

    def _marker_delete(self, p: dict[str, Any]) -> dict[str, Any]:
        ctx = self._require_timeline(p)
        if isinstance(ctx, dict):
            return ctx
        _app, _pm, project, _pool, timeline = ctx
        if p.get("custom_data"):
            timeline.DeleteMarkerByCustomData(p["custom_data"])
            return ok(deleted="custom_data")
        if p.get("color"):
            timeline.DeleteMarkersByColor(p["color"])
            return ok(deleted="color")
        if p.get("at_seconds") is not None:
            fps = M.timeline_fps(timeline, project)
            origin = M.start_frame(timeline)
            frame = M.seconds_to_frame(float(p["at_seconds"]), fps, origin)
            timeline.DeleteMarkerAtFrame(frame)
            return ok(deleted="frame", frame=frame)
        return fail("pass at_seconds, color, or custom_data", editor=self.name)

    def _create_subtitles(self, _p: dict[str, Any]) -> dict[str, Any]:
        ctx = self._require_timeline(_p)
        if isinstance(ctx, dict):
            return ctx
        _app, _pm, _project, _pool, timeline = ctx
        try:
            done = bool(timeline.CreateSubtitlesFromAudio())
        except Exception as exc:
            return fail(
                f"CreateSubtitlesFromAudio: {exc}",
                editor=self.name,
                studio_only=True,
                feature="CreateSubtitlesFromAudio",
            )
        if not done:
            return fail(
                "CreateSubtitlesFromAudio returned false (Studio-only; needs dialogue on the timeline).",
                editor=self.name,
                studio_only=True,
            )
        return ok(feature="CreateSubtitlesFromAudio", studio_only=True)

    def _smart_reframe(self, p: dict[str, Any]) -> dict[str, Any]:
        ctx = self._require_timeline(p)
        if isinstance(ctx, dict):
            return ctx
        _app, _pm, _project, _pool, timeline = ctx
        _ref, item = M.item_at(timeline, int(p["track_index"]), int(p["event_index"]))
        if item is None:
            return fail("event not found", editor=self.name)
        try:
            done = bool(item.SmartReframe())
        except Exception as exc:
            return fail(
                f"SmartReframe: {exc}",
                editor=self.name,
                studio_only=True,
                feature="SmartReframe",
            )
        if not done:
            return fail("SmartReframe returned false (Studio-only).", editor=self.name, studio_only=True)
        return ok(feature="SmartReframe", studio_only=True)

    def _voice_isolation(self, p: dict[str, Any]) -> dict[str, Any]:
        ctx = self._require_timeline(p)
        if isinstance(ctx, dict):
            return ctx
        _app, _pm, _project, _pool, timeline = ctx
        _ref, item = M.item_at(timeline, int(p.get("track_index", 0)), int(p.get("event_index", 0)))
        if item is None:
            return fail("event not found", editor=self.name)
        enabled = bool(p.get("enabled", True))
        target = item
        try:
            mpi = item.GetMediaPoolItem()
            if mpi is not None:
                target = mpi
        except Exception:
            pass
        try:
            done = bool(target.SetVoiceIsolationState(1 if enabled else 0))
        except Exception as exc:
            return fail(
                f"SetVoiceIsolationState: {exc}",
                editor=self.name,
                studio_only=True,
                feature="SetVoiceIsolationState",
            )
        if not done:
            return fail(
                "SetVoiceIsolationState returned false (Studio-only).",
                editor=self.name,
                studio_only=True,
            )
        return ok(feature="SetVoiceIsolationState", enabled=enabled, studio_only=True)


def _product(app: Any) -> str:
    try:
        return str(app.GetProductName() or "DaVinci Resolve")
    except Exception:
        return "DaVinci Resolve"


def _is_studio(product: str) -> bool:
    return "studio" in product.lower()


def _name(obj: Any) -> str:
    try:
        return str(obj.GetName() or "")
    except Exception:
        return ""


def _start(item: Any) -> int:
    try:
        return int(item.GetStart() or 0)
    except Exception:
        return 0


def _duration(item: Any) -> int:
    try:
        return int(item.GetDuration() or 0)
    except Exception:
        return 0


def _take_offset(item: Any, fps: float) -> float:
    try:
        left = item.GetLeftOffset()
        # GetLeftOffset is "maximum extension to the left", not the in-point.
    except Exception:
        left = None
    try:
        mpi = item.GetMediaPoolItem()
        props = mpi.GetClipProperty() if mpi else None
        if isinstance(props, dict) and props.get("Start"):
            # Not reliable across versions; prefer 0.
            pass
    except Exception:
        pass
    _ = left
    _ = fps
    return 0.0


def _clip_length_seconds(clip: Any) -> float:
    try:
        props = clip.GetClipProperty() or {}
    except Exception:
        return 0.0
    if not isinstance(props, dict):
        return 0.0
    for key in ("Duration", "Clip Duration"):
        raw = props.get(key)
        if raw is None:
            continue
        text = str(raw)
        if ":" in text:
            # HH:MM:SS:FF or HH:MM:SS.mmm — best-effort.
            parts = text.replace(";", ":").split(":")
            try:
                nums = [float(p) for p in parts]
            except ValueError:
                continue
            if len(nums) >= 3:
                return nums[0] * 3600 + nums[1] * 60 + nums[2]
        try:
            return float(text)
        except ValueError:
            continue
    return 0.0


def _setting_int(project: Any, key: str, default: int) -> int:
    try:
        raw = project.GetSetting(key)
        return int(float(raw))
    except Exception:
        return default


def _set_title_text(item: Any, text: str) -> None:
    try:
        count = int(item.GetFusionCompCount() or 0)
    except Exception:
        count = 0
    for i in range(1, count + 1):
        try:
            comp = item.GetFusionCompByIndex(i)
        except Exception:
            continue
        if comp is None:
            continue
        tools = {}
        try:
            tools = comp.GetToolList() or {}
        except Exception:
            tools = {}
        if isinstance(tools, dict):
            iterable = tools.values()
        else:
            iterable = tools
        for tool in iterable:
            try:
                tool.SetInput("StyledText", text)
                return
            except Exception:
                continue


def _normalize_job_state(raw: str) -> str:
    s = (raw or "").lower()
    if s in {"complete", "completed", "done", "success", "finished"}:
        return "complete"
    if s in {"failed", "fail", "error", "cancelled", "canceled"}:
        return "failed" if "cancel" not in s else "canceled"
    if s in {"rendering", "running", "in progress", "in_progress"}:
        return "running"
    if s in {"ready", "queued", "waiting"}:
        return "queued"
    return s or "unknown"
