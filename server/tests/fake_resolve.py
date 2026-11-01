"""In-memory stand-in for DaVinciResolveScript objects. No Resolve install."""
from __future__ import annotations

from typing import Any


class FakeTool:
    def __init__(self) -> None:
        self.inputs: dict[str, Any] = {}

    def SetInput(self, key: str, value: Any) -> None:
        self.inputs[key] = value


class FakeComp:
    def GetToolList(self) -> dict[str, FakeTool]:
        return {"Text1": FakeTool()}


class FakePoolItem:
    def __init__(self, path: str, name: str | None = None, duration: float = 10.0):
        self.path = path
        self._name = name or path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        self.props = {
            "File Path": path,
            "Duration": str(duration),
            "Online": "Online",
        }
        self.replaced: str | None = None
        self.voice = 0

    def GetName(self) -> str:
        return self._name

    def GetClipProperty(self, key: str | None = None):
        if key:
            return self.props.get(key)
        return dict(self.props)

    def SetClipProperty(self, key: str, value: Any) -> bool:
        self.props[key] = value
        return True

    def ReplaceClip(self, path: str) -> bool:
        self.replaced = path
        self.path = path
        self.props["File Path"] = path
        return True

    def SetVoiceIsolationState(self, state: int) -> bool:
        self.voice = state
        return True


class FakeItem:
    def __init__(self, *, start: int, duration: int, mpi: FakePoolItem | None = None, name: str = "clip"):
        self._start = start
        self._duration = duration
        self._mpi = mpi
        self._name = name
        self.props: dict[str, Any] = {"Opacity": 100.0, "ZoomX": 1.0, "ZoomY": 1.0, "Pan": 0.0, "Tilt": 0.0}
        self.comps = [FakeComp()]
        self.smart_reframe_calls = 0

    def GetName(self) -> str:
        return self._name

    def GetStart(self) -> int:
        return self._start

    def GetDuration(self) -> int:
        return self._duration

    def GetEnd(self) -> int:
        return self._start + self._duration

    def GetLeftOffset(self) -> int:
        return 0

    def GetMediaPoolItem(self) -> FakePoolItem | None:
        return self._mpi

    def SetProperty(self, key: Any, value: Any = None) -> bool:
        if isinstance(key, dict):
            self.props.update(key)
            return True
        self.props[key] = value
        return True

    def GetProperty(self, key: str | None = None):
        if key is None:
            return dict(self.props)
        return self.props.get(key)

    def GetFusionCompCount(self) -> int:
        return len(self.comps)

    def GetFusionCompByIndex(self, idx: int) -> FakeComp:
        return self.comps[idx - 1]

    def SmartReframe(self) -> bool:
        self.smart_reframe_calls += 1
        return True

    def SetVoiceIsolationState(self, state: int) -> bool:
        if self._mpi:
            return self._mpi.SetVoiceIsolationState(state)
        return True


class FakeFolder:
    def __init__(self) -> None:
        self.clips: list[FakePoolItem] = []

    def GetClipList(self) -> list[FakePoolItem]:
        return list(self.clips)

    def GetSubFolderList(self) -> list:
        return []


class FakeTimeline:
    def __init__(self, name: str = "Timeline 1", start: int = 86400, fps: float = 24.0):
        self._name = name
        self._start = start
        self.settings = {
            "timelineFrameRate": str(fps),
            "timelineResolutionWidth": "1920",
            "timelineResolutionHeight": "1080",
        }
        self.video: list[list[FakeItem]] = [[]]
        self.audio: list[list[FakeItem]] = [[]]
        self.subtitle: list[list[FakeItem]] = []
        self.track_names = {"video": {1: "V1"}, "audio": {1: "A1"}, "subtitle": {}}
        self.enabled = {"video": {1: True}, "audio": {1: True}}
        self.markers: dict[float, dict[str, Any]] = {}
        self.tc = "01:00:00:00"
        self.subtitles_called = False

    def GetName(self) -> str:
        return self._name

    def GetStartFrame(self) -> int:
        return self._start

    def GetEndFrame(self) -> int:
        end = self._start
        for items in self.video + self.audio:
            for it in items:
                end = max(end, it.GetEnd())
        return end

    def GetSetting(self, key: str) -> str:
        return self.settings.get(key, "")

    def SetSetting(self, key: str, value: str) -> bool:
        self.settings[key] = str(value)
        return True

    def GetTrackCount(self, kind: str) -> int:
        return len(getattr(self, kind))

    def GetTrackName(self, kind: str, index: int) -> str:
        return self.track_names.get(kind, {}).get(index, f"{kind[0].upper()}{index}")

    def SetTrackName(self, kind: str, index: int, name: str) -> bool:
        self.track_names.setdefault(kind, {})[index] = name
        return True

    def GetIsTrackEnabled(self, kind: str, index: int) -> bool:
        return self.enabled.get(kind, {}).get(index, True)

    def SetTrackEnable(self, kind: str, index: int, enabled: bool) -> bool:
        self.enabled.setdefault(kind, {})[index] = bool(enabled)
        return True

    def AddTrack(self, kind: str, _sub: str | None = None) -> bool:
        bucket = getattr(self, kind)
        bucket.append([])
        idx = len(bucket)
        self.track_names.setdefault(kind, {})[idx] = f"{kind[0].upper()}{idx}"
        self.enabled.setdefault(kind, {})[idx] = True
        return True

    def GetItemListInTrack(self, kind: str, index: int) -> list[FakeItem]:
        bucket = getattr(self, kind)
        if 1 <= index <= len(bucket):
            return list(bucket[index - 1])
        return []

    def DeleteClips(self, items: list[FakeItem], _ripple: bool = False) -> bool:
        for bucket in self.video + self.audio + self.subtitle:
            for it in items:
                if it in bucket:
                    bucket.remove(it)
        return True

    def SetCurrentTimecode(self, tc: str) -> bool:
        self.tc = tc
        return True

    def GetCurrentTimecode(self) -> str:
        return self.tc

    def GetCurrentVideoItem(self) -> FakeItem | None:
        if self.video and self.video[0]:
            return self.video[0][0]
        return None

    def AddMarker(self, frame, color, name, note, duration, custom=""):
        self.markers[float(frame)] = {
            "color": color,
            "name": name,
            "note": note,
            "duration": duration,
            "customData": custom,
        }
        return True

    def GetMarkers(self) -> dict:
        return dict(self.markers)

    def DeleteMarkerByCustomData(self, custom: str) -> bool:
        self.markers = {k: v for k, v in self.markers.items() if v.get("customData") != custom}
        return True

    def DeleteMarkersByColor(self, color: str) -> bool:
        if color == "All":
            self.markers = {}
        else:
            self.markers = {k: v for k, v in self.markers.items() if v.get("color") != color}
        return True

    def DeleteMarkerAtFrame(self, frame: int) -> bool:
        self.markers.pop(float(frame), None)
        return True

    def InsertFusionTitleIntoTimeline(self, _name: str) -> FakeItem:
        item = FakeItem(start=self._start, duration=72, name="title")
        if not self.video:
            self.video.append([])
        self.video[0].append(item)
        return item

    def InsertTitleIntoTimeline(self, name: str) -> FakeItem:
        return self.InsertFusionTitleIntoTimeline(name)

    def CreateSubtitlesFromAudio(self) -> bool:
        self.subtitles_called = True
        return True


class FakePool:
    def __init__(self, project: FakeProject):
        self.project = project
        self.root = FakeFolder()

    def GetRootFolder(self) -> FakeFolder:
        return self.root

    def ImportMedia(self, paths: list[str]) -> list[FakePoolItem]:
        items = []
        for path in paths:
            clip = FakePoolItem(path)
            self.root.clips.append(clip)
            items.append(clip)
        return items

    def CreateEmptyTimeline(self, name: str) -> FakeTimeline:
        tl = FakeTimeline(name)
        self.project.timelines.append(tl)
        self.project.current_timeline = tl
        return tl

    def AppendToTimeline(self, infos: list[Any]) -> list[FakeItem]:
        placed = []
        tl = self.project.current_timeline
        if tl is None:
            return []
        for info in infos:
            if not isinstance(info, dict):
                mpi = info
                info = {"mediaPoolItem": mpi, "trackIndex": 1, "mediaType": 1}
            mpi = info["mediaPoolItem"]
            start = int(info.get("recordFrame") or tl.GetStartFrame())
            src0 = int(info.get("startFrame") or 0)
            src1 = int(info.get("endFrame") or src0 + 24)
            duration = max(1, src1 - src0)
            item = FakeItem(start=start, duration=duration, mpi=mpi, name=mpi.GetName())
            media_type = int(info.get("mediaType") or 1)
            track_i = int(info.get("trackIndex") or 1)
            bucket = tl.video if media_type == 1 else tl.audio
            while len(bucket) < track_i:
                bucket.append([])
            bucket[track_i - 1].append(item)
            placed.append(item)
        return placed


class FakeProject:
    def __init__(self, name: str = "Proj"):
        self._name = name
        self.settings = {
            "timelineFrameRate": "24",
            "timelineResolutionWidth": "1920",
            "timelineResolutionHeight": "1080",
        }
        self.timelines: list[FakeTimeline] = []
        self.current_timeline: FakeTimeline | None = None
        self.pool = FakePool(self)
        self.render_presets = ["H.264 Master", "YouTube 1080p"]
        self.render_settings: dict[str, Any] = {}
        self.jobs: dict[str, dict[str, Any]] = {}
        self.rendering = False
        self.format_codec = ("mp4", "H264")
        self._job_n = 0

    def GetName(self) -> str:
        return self._name

    def GetMediaPool(self) -> FakePool:
        return self.pool

    def GetCurrentTimeline(self) -> FakeTimeline | None:
        return self.current_timeline

    def SetCurrentTimeline(self, tl: FakeTimeline) -> bool:
        self.current_timeline = tl
        return True

    def GetSetting(self, key: str) -> str:
        return self.settings.get(key, "")

    def SetSetting(self, key: str, value: str) -> bool:
        self.settings[key] = str(value)
        return True

    def GetRenderPresetList(self) -> list[str]:
        return list(self.render_presets)

    def LoadRenderPreset(self, name: str) -> bool:
        return name in self.render_presets

    def SetCurrentRenderFormatAndCodec(self, fmt: str, codec: str) -> bool:
        self.format_codec = (fmt, codec)
        return True

    def SetRenderSettings(self, settings: dict) -> bool:
        self.render_settings = dict(settings)
        return True

    def AddRenderJob(self) -> str:
        self._job_n += 1
        job_id = f"job-{self._job_n}"
        self.jobs[job_id] = {"JobStatus": "Ready", "CompletionPercentage": 0}
        return job_id

    def StartRendering(self, job_id=None) -> bool:
        ids = job_id if isinstance(job_id, list) else [job_id] if job_id else list(self.jobs)
        for jid in ids:
            if jid in self.jobs:
                self.jobs[jid] = {"JobStatus": "Complete", "CompletionPercentage": 100}
        self.rendering = False
        return True

    def IsRenderingInProgress(self) -> bool:
        return self.rendering

    def GetRenderJobStatus(self, job_id: str) -> dict:
        return dict(self.jobs.get(job_id) or {})


class FakeProjectManager:
    def __init__(self, project: FakeProject | None):
        self.project = project
        self.exported: list[tuple[str, str]] = []
        self.saved = False
        self.imported: list[str] = []

    def GetCurrentProject(self) -> FakeProject | None:
        return self.project

    def SaveProject(self) -> bool:
        self.saved = True
        return True

    def ExportProject(self, name: str, path: str, _stills: bool = True) -> bool:
        self.exported.append((name, path))
        return True

    def CloseProject(self, _project) -> bool:
        self.project = None
        return True

    def CreateProject(self, name: str) -> FakeProject:
        self.project = FakeProject(name)
        return self.project

    def LoadProject(self, name: str) -> FakeProject:
        self.project = FakeProject(name)
        return self.project

    def ImportProject(self, path: str) -> bool:
        self.imported.append(path)
        self.project = FakeProject(path)
        return True


class FakeResolve:
    def __init__(self, product: str = "DaVinci Resolve Studio", version: str = "21.1.0"):
        self.product = product
        self.version = version
        self.project = FakeProject("Demo")
        self.pm = FakeProjectManager(self.project)
        self.page = "edit"

    def GetProductName(self) -> str:
        return self.product

    def GetVersionString(self) -> str:
        return self.version

    def GetVersion(self) -> list[int]:
        return [21, 1, 0, 0, ""]

    def GetProjectManager(self) -> FakeProjectManager:
        return self.pm

    def OpenPage(self, page: str) -> bool:
        self.page = page
        return True
