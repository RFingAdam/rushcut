// VegasDirectorHost.cs — VEGAS Pro 22 TCP host (Phase 3 + v2 headless extension).
// Script Menu: Tools > Scripting > VegasDirectorHost — leave the dialog open.
// Extension: VegasDirectorExtension.dll auto-starts the host at VEGAS launch (no dialog).

using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using System.Windows.Forms;
using ScriptPortal.Vegas;

public partial class EntryPoint
{
    private const int TcpPort = 8752;
    private Vegas myVegas;
    private volatile bool running = true;
    private Control uiMarshal;
    private DateTime startedAt = DateTime.Now;
    private int requestCount = 0;
    private string mode = "script";
    private string authToken = null;                       // required in extension mode
    internal Func<RpcRequest, string> v2Dispatch = null;  // reserved for future host modules
    private readonly object renderGate = new object();
    private string renderJobId = "";
    private string renderState = "idle";
    private int renderPercent = 0;
    private string renderOutput = "";
    private string renderError = "";
    private bool renderHooksAttached = false;

    // Called by the VegasDirector Application Extension at VEGAS startup (no dialog).
    public void StartHeadless(Vegas vegas, Control marshal)
    {
        myVegas = vegas;
        uiMarshal = marshal;
        mode = "extension";
        running = true;
        authToken = LoadOrCreateToken();
        Log("VegasDirectorHost v2 starting headless (extension) tcp=127.0.0.1:" + TcpPort + " (token required)");
        Thread tcpThread = new Thread(RunTcpListener);
        tcpThread.IsBackground = true;
        tcpThread.Start();
    }

    private static string LoadOrCreateToken()
    {
        string dir = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "vegas-director-mcp");
        Directory.CreateDirectory(dir);
        string f = Path.Combine(dir, "token");
        if (File.Exists(f))
        {
            string t = File.ReadAllText(f).Trim();
            if (t.Length >= 32) return t;
        }
        byte[] b = new byte[32];
        using (System.Security.Cryptography.RandomNumberGenerator rng = System.Security.Cryptography.RandomNumberGenerator.Create()) rng.GetBytes(b);
        string tok = BitConverter.ToString(b).Replace("-", "").ToLowerInvariant();
        File.WriteAllText(f, tok);
        return tok;
    }

    public string StatusText()
    {
        return "Vegas Director MCP host v2 (" + mode + ")\r\nTCP 127.0.0.1:" + TcpPort +
               "\r\nRunning since " + startedAt.ToString("g") + "\r\nRequests served: " + requestCount;
    }

    public string HostMode()
    {
        return mode;
    }

    private static bool PortInUse()
    {
        try { using (TcpClient c = new TcpClient()) { c.Connect("127.0.0.1", TcpPort); return true; } }
        catch { return false; }
    }

    public void FromVegas(Vegas vegas)
    {
        if (PortInUse())
        {
            MessageBox.Show("The Vegas Director host is already running (the VegasDirector extension starts it with VEGAS). Nothing to do.",
                            "vegas-director-mcp");
            return;
        }
        myVegas = vegas;
        mode = "script";

        // Vegas has no Invoke. A visible status Form on this (UI) thread
        // marshals ScriptPortal calls and keeps the host alive.
        // MessageBox was easy to lose behind other windows and killed TCP when dismissed.
        Form status = new Form();
        status.Text = "vegas-director-mcp host";
        status.FormBorderStyle = FormBorderStyle.FixedDialog;
        status.MaximizeBox = false;
        status.MinimizeBox = true;
        status.ShowInTaskbar = true;
        status.StartPosition = FormStartPosition.CenterScreen;
        status.ClientSize = new System.Drawing.Size(440, 200);
        status.TopMost = false;

        Label lbl = new Label();
        lbl.AutoSize = false;
        lbl.Dock = DockStyle.Fill;
        lbl.Padding = new Padding(14);
        lbl.Text =
            StatusText() + "\r\n\r\n" +
            "Minimize this window while you scrub Vegas.\r\n" +
            "Close it or click Stop host to disconnect MCP.";

        Button stop = new Button();
        stop.Text = "Stop host";
        stop.Dock = DockStyle.Bottom;
        stop.Height = 40;
        stop.Click += delegate { status.Close(); };

        status.Controls.Add(lbl);
        status.Controls.Add(stop);
        status.FormClosed += delegate { running = false; };

        uiMarshal = status;

        Log("VegasDirectorHost starting tcp=127.0.0.1:" + TcpPort);

        Thread tcpThread = new Thread(RunTcpListener);
        tcpThread.IsBackground = true;
        tcpThread.Start();

        // Modal status form — survives focus changes; only exits when user stops it.
        status.ShowDialog();
        running = false;
        try { status.Dispose(); } catch { }
    }

    private void RunTcpListener()
    {
        TcpListener listener = null;
        try
        {
            listener = new TcpListener(IPAddress.Loopback, TcpPort);
            listener.Start();
            Log("TCP listening on 127.0.0.1:" + TcpPort);
            while (running)
            {
                if (!listener.Pending())
                {
                    Thread.Sleep(50);
                    continue;
                }
                using (TcpClient client = listener.AcceptTcpClient())
                using (NetworkStream stream = client.GetStream())
                {
                    Log("TCP client connected.");
                    HandleStream(stream);
                }
            }
        }
        catch (Exception ex)
        {
            Log("TCP listener error: " + ex.Message);
        }
        finally
        {
            if (listener != null) listener.Stop();
        }
    }

    private void HandleStream(Stream stream)
    {
        StreamReader reader = new StreamReader(stream, Encoding.UTF8);
        StreamWriter writer = new StreamWriter(stream, Encoding.UTF8);
        writer.AutoFlush = true;
        while (true)
        {
            string line;
            try { line = reader.ReadLine(); }
            catch { break; }
            if (line == null) break;
            if (line.Trim().Length == 0) continue;
            string response = Dispatch(line);
            try { writer.WriteLine(response); }
            catch { break; }
        }
    }

    private string Dispatch(string requestJson)
    {
        RpcRequest req;
        try { req = RpcRequest.Parse(requestJson); }
        catch (Exception ex)
        {
            return RpcResponse.Error(null, -32700, "Parse error: " + ex.Message);
        }

        requestCount++;
        if (authToken != null && Json.GetString(requestJson, "token") != authToken)
            return RpcResponse.Error(req.Id, -32001, "Unauthorized: missing or wrong token");

        try
        {
            switch (req.Method)
            {
                case "host.info":
                    return RpcResponse.Result(req.Id, "{\"ok\":true,\"version\":2,\"mode\":" + Json.Str(mode) +
                        ",\"requests\":" + requestCount + ",\"started\":" + Json.Str(startedAt.ToString("s")) +
                        ",\"v2\":" + (v2Dispatch != null ? "true" : "false") + "}");
                case "ping":
                    return RpcResponse.Result(req.Id, "{\"ok\":true,\"host\":\"vegas-director\",\"mode\":" + Json.Str(mode) + "}");
                case "project.get_state":
                    return InvokeOnUiThread(req, GetProjectState);
                case "project.save":
                    return InvokeOnUiThread(req, SaveProject);
                case "track.add":
                    return InvokeOnUiThread(req, AddTrack);
                case "media.import":
                    return InvokeOnUiThread(req, ImportMedia);
                case "media.place":
                    return InvokeOnUiThread(req, PlaceMedia);
                case "event.add_video":
                    return InvokeOnUiThread(req, AddVideoEvent);
                case "event.add_audio":
                    return InvokeOnUiThread(req, AddAudioEvent);
                case "event.trim":
                    return InvokeOnUiThread(req, TrimEvent);
                case "event.move":
                    return InvokeOnUiThread(req, MoveEvent);
                case "event.delete":
                    return InvokeOnUiThread(req, DeleteEvent);
                case "event.set_motion":
                    return InvokeOnUiThread(req, SetEventMotion);
                case "event.set_fades":
                    return InvokeOnUiThread(req, SetEventFades);
                case "event.set_opacity":
                    return InvokeOnUiThread(req, SetEventOpacity);
                case "event.add_title":
                    return InvokeOnUiThread(req, AddTitleEvent);
                case "track.set_composite_level":
                    return InvokeOnUiThread(req, SetTrackCompositeLevel);
                case "envelope.set_points":
                    return InvokeOnUiThread(req, SetEnvelopePoints);
                case "project.get_selected_events":
                    return InvokeOnUiThread(req, GetSelectedEvents);
                case "render.start":
                    return InvokeOnUiThread(req, RenderStart);
                case "render.status":
                    return RenderStatusQuery(req);
                case "project.set_video":
                    return InvokeOnUiThread(req, SetProjectVideo);
                case "transport.play":
                    return InvokeOnUiThread(req, TransportPlay);
                case "transport.stop":
                    return InvokeOnUiThread(req, TransportStop);
                case "transport.seek":
                    return InvokeOnUiThread(req, TransportSeek);
                case "project.new":
                    return InvokeOnUiThread(req, ProjectNew);
                case "project.open":
                    return InvokeOnUiThread(req, ProjectOpen);
                case "media.list":
                    return InvokeOnUiThread(req, ListMedia);
                case "media.replace":
                    return InvokeOnUiThread(req, ReplaceMedia);
                case "media.set_alpha":
                    return InvokeOnUiThread(req, SetMediaAlpha);
                case "track.set_props":
                    return InvokeOnUiThread(req, SetTrackProps);
                case "track.clear_events":
                    return InvokeOnUiThread(req, ClearTrackEvents);
                case "envelope.clear":
                    return InvokeOnUiThread(req, ClearEnvelope);
                case "event.set_gain":
                    return InvokeOnUiThread(req, SetEventGain);
                case "render.list_templates":
                    return InvokeOnUiThread(req, ListRenderTemplates);
                default:
                    if (v2Dispatch != null)
                    {
                        string r2 = v2Dispatch(req);
                        if (r2 != null) return r2;
                    }
                    return RpcResponse.Error(req.Id, -32601, "Method not found: " + req.Method);
            }
        }
        catch (Exception ex)
        {
            return SoftFail(req.Id, "Internal error: " + ex.Message);
        }
    }

    private string InvokeOnUiThread(RpcRequest req, Func<RpcRequest, string> handler)
    {
        int timeoutMs = Array.IndexOf(LongMethods, req.Method) >= 0 ? 1800000 : 60000;
        return InvokeOnUiThread(req, handler, timeoutMs);
    }

    private string InvokeOnUiThread(RpcRequest req, Func<RpcRequest, string> handler, int timeoutMs)
    {
        if (uiMarshal == null || uiMarshal.IsDisposed)
            return SoftFail(req.Id, "UI marshal unavailable");

        string result = null;
        Exception thrown = null;
        ManualResetEvent done = new ManualResetEvent(false);

        uiMarshal.BeginInvoke((MethodInvoker)delegate
        {
            try { result = RunOnUiThread(req, handler); }
            catch (Exception ex) { thrown = ex; }
            finally { done.Set(); }
        });

        if (!done.WaitOne(timeoutMs))
            return SoftFail(req.Id, "UI marshal timed out after " + timeoutMs + " ms (VEGAS may still finish the call)");
        if (thrown != null)
            return SoftFail(req.Id, thrown.Message);
        return result;
    }

    // Script Menu runs are wrapped in an UndoBlock by VEGAS automatically
    // (the whole FromVegas call). Application Extensions are not — MAGIX
    // documents that custom-command / extension mutations must create one
    // themselves. Without it, Project.Tracks.Add / Video.Width / NewProject
    // raise COM E_UNEXPECTED (0x8000FFFF) even when already on the UI thread.
    // Nested UndoBlocks are valid in script mode.
    private static bool MethodNeedsUndoBlock(string method)
    {
        if (method == null) return false;
        switch (method)
        {
            case "ping":
            case "host.info":
            case "project.get_state":
            case "project.get_selected_events":
            case "project.save":
            case "media.list":
            case "render.list_templates":
            case "render.start":
            case "render.status":
            case "transport.play":
            case "transport.stop":
            case "transport.seek":
                return false;
            default:
                return true;
        }
    }

    private string RunOnUiThread(RpcRequest req, Func<RpcRequest, string> handler)
    {
        if (!MethodNeedsUndoBlock(req.Method))
            return handler(req);

        using (UndoBlock undo = new UndoBlock("Vegas Director: " + req.Method))
        {
            try
            {
                return handler(req);
            }
            catch (Exception)
            {
                try { undo.Cancel = true; } catch { }
                throw;
            }
        }
    }

    private static string SoftFail(string id, string error)
    {
        return RpcResponse.Result(id, "{\"ok\":false,\"error\":" + Json.Str(error) + "}");
    }

    private Project RequireProject()
    {
        Project project = myVegas.Project;
        if (project == null)
            throw new Exception("No active VEGAS project");
        return project;
    }

    private static double TimecodeToSeconds(Timecode tc)
    {
        return tc.ToMilliseconds() / 1000.0;
    }

    private static Timecode SecondsToTimecode(double seconds)
    {
        return Timecode.FromSeconds(seconds);
    }

    private string GetProjectState(RpcRequest req)
    {
        Project project = RequireProject();
        StringBuilder tracks = new StringBuilder();
        tracks.Append("[");
        int videoTracks = 0, audioTracks = 0;
        bool first = true;
        for (int i = 0; i < project.Tracks.Count; i++)
        {
            Track t = project.Tracks[i];
            string kind = t.IsVideo() ? "video" : (t.IsAudio() ? "audio" : "other");
            if (t.IsVideo()) videoTracks++;
            if (t.IsAudio()) audioTracks++;
            if (!first) tracks.Append(",");
            first = false;
            tracks.Append("{\"index\":").Append(i)
                  .Append(",\"name\":").Append(Json.Str(t.Name))
                  .Append(",\"type\":").Append(Json.Str(kind))
                  .Append(",\"event_count\":").Append(t.Events.Count)
                  .Append("}");
        }
        tracks.Append("]");

        StringBuilder events = new StringBuilder();
        events.Append("[");
        first = true;
        for (int ti = 0; ti < project.Tracks.Count; ti++)
        {
            Track t = project.Tracks[ti];
            for (int ei = 0; ei < t.Events.Count; ei++)
            {
                TrackEvent ev = t.Events[ei];
                if (!first) events.Append(",");
                first = false;
                StringBuilder eb = new StringBuilder();
                eb.Append("{\"track_index\":").Append(ti)
                  .Append(",\"event_index\":").Append(ei)
                  .Append(",\"start_seconds\":").Append(Json.Num(TimecodeToSeconds(ev.Start)))
                  .Append(",\"length_seconds\":").Append(Json.Num(TimecodeToSeconds(ev.Length)));
                // Soft: media/take fields optional per event
                try
                {
                    Take take = ev.ActiveTake;
                    if (take != null)
                    {
                        try
                        {
                            if (take.Offset != null)
                                eb.Append(",\"take_offset_seconds\":").Append(Json.Num(TimecodeToSeconds(take.Offset)));
                        }
                        catch { }
                        try
                        {
                            if (take.Length != null)
                                eb.Append(",\"take_length_seconds\":").Append(Json.Num(TimecodeToSeconds(take.Length)));
                        }
                        catch { }
                        try
                        {
                            Media media = take.Media;
                            if (media != null && media.FilePath != null && media.FilePath.Length > 0)
                            {
                                eb.Append(",\"media_path\":").Append(Json.Str(media.FilePath));
                                try
                                {
                                    string name = Path.GetFileName(media.FilePath);
                                    if (name != null && name.Length > 0)
                                        eb.Append(",\"media_name\":").Append(Json.Str(name));
                                }
                                catch { }
                            }
                        }
                        catch { }
                    }
                }
                catch { }
                eb.Append("}");
                events.Append(eb.ToString());
            }
        }
        events.Append("]");

        int vw = 0, vh = 0;
        double fps = 0;
        try { vw = project.Video.Width; vh = project.Video.Height; fps = project.Video.FrameRate; } catch { }

        StringBuilder sb = new StringBuilder();
        sb.Append("{\"ok\":true")
          .Append(",\"length_seconds\":").Append(Json.Num(TimecodeToSeconds(project.Length)))
          .Append(",\"video_width\":").Append(vw)
          .Append(",\"video_height\":").Append(vh)
          .Append(",\"frame_rate\":").Append(Json.Num(fps))
          .Append(",\"video_track_count\":").Append(videoTracks)
          .Append(",\"audio_track_count\":").Append(audioTracks)
          .Append(",\"tracks\":").Append(tracks)
          .Append(",\"events\":").Append(events)
          .Append("}");
        return RpcResponse.Result(req.Id, sb.ToString());
    }

    private string SaveProject(RpcRequest req)
    {
        try
        {
            string path = Json.GetString(req.ParamsJson, "path");
            if (path != null && path.Length > 0)
                myVegas.SaveProject(path);
            else
                myVegas.SaveProject();
            return RpcResponse.Result(req.Id, "{\"ok\":true}");
        }
        catch (Exception ex)
        {
            return SoftFail(req.Id, "Save failed: " + ex.Message);
        }
    }

    private string AddTrack(RpcRequest req)
    {
        Project project = RequireProject();
        string type = (Json.GetString(req.ParamsJson, "type") ?? "video").ToLowerInvariant();
        string name = Json.GetString(req.ParamsJson, "name") ?? "";
        int index = Json.GetInt(req.ParamsJson, "index", project.Tracks.Count);
        if (index < 0 || index > project.Tracks.Count) index = project.Tracks.Count;
        if (type == "audio")
            project.Tracks.Add(new AudioTrack(project, index, name));
        else
            project.Tracks.Add(new VideoTrack(project, index, name));
        return RpcResponse.Result(req.Id,
            "{\"ok\":true,\"track_index\":" + index + ",\"type\":" + Json.Str(type) + "}");
    }

    private string ImportMedia(RpcRequest req)
    {
        Project project = RequireProject();
        string path = Json.GetString(req.ParamsJson, "path");
        if (path == null || path.Length == 0)
            return SoftFail(req.Id, "params.path required");
        if (!File.Exists(path))
            return SoftFail(req.Id, "Media file not found: " + path);

        Media media;
        try { media = project.MediaPool.AddMedia(path); }
        catch (Exception ex) { return SoftFail(req.Id, "Import failed: " + ex.Message); }
        bool hasVideo = false;
        bool hasAudio = false;
        try { hasVideo = media.HasVideo(); } catch { }
        try { hasAudio = media.HasAudio(); } catch { }
        double len = TimecodeToSeconds(media.Length);

        StringBuilder sb = new StringBuilder();
        sb.Append("{\"ok\":true")
          .Append(",\"path\":").Append(Json.Str(path))
          .Append(",\"length_seconds\":").Append(Json.Num(len))
          .Append(",\"has_video\":").Append(hasVideo ? "true" : "false")
          .Append(",\"has_audio\":").Append(hasAudio ? "true" : "false")
          .Append("}");
        return RpcResponse.Result(req.Id, sb.ToString());
    }

    private Media FindMediaByPath(Project project, string path)
    {
        string full = Path.GetFullPath(path);
        foreach (Media m in project.MediaPool)
        {
            try
            {
                if (m.FilePath != null &&
                    string.Equals(Path.GetFullPath(m.FilePath), full, StringComparison.OrdinalIgnoreCase))
                    return m;
            }
            catch { }
        }
        return null;
    }

    private Media EnsureMedia(Project project, string path)
    {
        Media media = FindMediaByPath(project, path);
        if (media != null) return media;
        if (!File.Exists(path))
            throw new Exception("Media file not found: " + path);
        return project.MediaPool.AddMedia(path);
    }

    private string PlaceEventResult(string id, int trackIndex, int eventIndex, double start, double length, string kind)
    {
        StringBuilder sb = new StringBuilder();
        sb.Append("{\"ok\":true")
          .Append(",\"track_index\":").Append(trackIndex)
          .Append(",\"event_index\":").Append(eventIndex)
          .Append(",\"start_seconds\":").Append(Json.Num(start))
          .Append(",\"length_seconds\":").Append(Json.Num(length))
          .Append(",\"media_kind\":").Append(Json.Str(kind))
          .Append("}");
        return RpcResponse.Result(id, sb.ToString());
    }

    private string PlaceMedia(RpcRequest req)
    {
        Project project = RequireProject();
        string path = Json.GetString(req.ParamsJson, "path");
        if (path == null || path.Length == 0)
            path = Json.GetString(req.ParamsJson, "media_path");
        if (path == null || path.Length == 0)
            return SoftFail(req.Id, "params.path or params.media_path required");

        int trackIndex = Json.GetInt(req.ParamsJson, "track_index", -1);
        double start = Json.GetDouble(req.ParamsJson, "start_seconds", 0);
        double length = Json.GetDouble(req.ParamsJson, "length_seconds", -1);
        if (trackIndex < 0 || trackIndex >= project.Tracks.Count)
            return SoftFail(req.Id, "Invalid track_index");

        Track track = project.Tracks[trackIndex];
        Media media;
        try { media = EnsureMedia(project, path); }
        catch (Exception ex) { return SoftFail(req.Id, ex.Message); }

        if (length < 0)
            length = TimecodeToSeconds(media.Length);

        try
        {
            if (track.IsVideo())
            {
                VideoTrack vtrack = (VideoTrack)track;
                VideoEvent ve = vtrack.AddVideoEvent(SecondsToTimecode(start), SecondsToTimecode(length));
                MediaStream stream = media.Streams.GetItemByMediaType(MediaType.Video, 0);
                if (stream == null)
                    return SoftFail(req.Id, "Media has no video stream: " + path);
                ve.AddTake(stream);
                return PlaceEventResult(req.Id, trackIndex, vtrack.Events.Count - 1, start, length, "video");
            }
            if (track.IsAudio())
            {
                AudioTrack atrack = (AudioTrack)track;
                AudioEvent ae = atrack.AddAudioEvent(SecondsToTimecode(start), SecondsToTimecode(length));
                MediaStream stream = media.Streams.GetItemByMediaType(MediaType.Audio, 0);
                if (stream == null)
                    return SoftFail(req.Id, "Media has no audio stream: " + path);
                ae.AddTake(stream);
                return PlaceEventResult(req.Id, trackIndex, atrack.Events.Count - 1, start, length, "audio");
            }
            return SoftFail(req.Id, "Track is neither video nor audio");
        }
        catch (Exception ex)
        {
            return SoftFail(req.Id, "Place failed: " + ex.Message);
        }
    }

    private string AddVideoEvent(RpcRequest req)
    {
        Project project = RequireProject();
        int trackIndex = Json.GetInt(req.ParamsJson, "track_index", 0);
        double start = Json.GetDouble(req.ParamsJson, "start_seconds", 0);
        double length = Json.GetDouble(req.ParamsJson, "length_seconds", -1);
        string path = Json.GetString(req.ParamsJson, "media_path");
        if (path == null || path.Length == 0)
            path = Json.GetString(req.ParamsJson, "path");
        if (path == null || path.Length == 0)
            return SoftFail(req.Id, "params.media_path required");
        if (trackIndex < 0 || trackIndex >= project.Tracks.Count || !project.Tracks[trackIndex].IsVideo())
            return SoftFail(req.Id, "Invalid video track_index");

        Media media;
        try { media = EnsureMedia(project, path); }
        catch (Exception ex) { return SoftFail(req.Id, ex.Message); }
        if (length < 0)
            length = TimecodeToSeconds(media.Length);

        try
        {
            VideoTrack vtrack = (VideoTrack)project.Tracks[trackIndex];
            VideoEvent ve = vtrack.AddVideoEvent(SecondsToTimecode(start), SecondsToTimecode(length));
            MediaStream stream = media.Streams.GetItemByMediaType(MediaType.Video, 0);
            if (stream == null)
                return SoftFail(req.Id, "Media has no video stream: " + path);
            ve.AddTake(stream);
            return PlaceEventResult(req.Id, trackIndex, vtrack.Events.Count - 1, start, length, "video");
        }
        catch (Exception ex)
        {
            return SoftFail(req.Id, "Add video failed: " + ex.Message);
        }
    }

    private string AddAudioEvent(RpcRequest req)
    {
        Project project = RequireProject();
        int trackIndex = Json.GetInt(req.ParamsJson, "track_index", 0);
        double start = Json.GetDouble(req.ParamsJson, "start_seconds", 0);
        double length = Json.GetDouble(req.ParamsJson, "length_seconds", -1);
        string path = Json.GetString(req.ParamsJson, "media_path");
        if (path == null || path.Length == 0)
            path = Json.GetString(req.ParamsJson, "path");
        if (path == null || path.Length == 0)
            return SoftFail(req.Id, "params.media_path required");
        if (trackIndex < 0 || trackIndex >= project.Tracks.Count || !project.Tracks[trackIndex].IsAudio())
            return SoftFail(req.Id, "Invalid audio track_index");

        Media media;
        try { media = EnsureMedia(project, path); }
        catch (Exception ex) { return SoftFail(req.Id, ex.Message); }
        if (length < 0)
            length = TimecodeToSeconds(media.Length);

        try
        {
            AudioTrack atrack = (AudioTrack)project.Tracks[trackIndex];
            AudioEvent ae = atrack.AddAudioEvent(SecondsToTimecode(start), SecondsToTimecode(length));
            MediaStream stream = media.Streams.GetItemByMediaType(MediaType.Audio, 0);
            if (stream == null)
                return SoftFail(req.Id, "Media has no audio stream: " + path);
            ae.AddTake(stream);
            return PlaceEventResult(req.Id, trackIndex, atrack.Events.Count - 1, start, length, "audio");
        }
        catch (Exception ex)
        {
            return SoftFail(req.Id, "Add audio failed: " + ex.Message);
        }
    }

    private TrackEvent GetEvent(Project project, int trackIndex, int eventIndex)
    {
        if (trackIndex < 0 || trackIndex >= project.Tracks.Count)
            throw new Exception("Invalid track_index");
        Track t = project.Tracks[trackIndex];
        if (eventIndex < 0 || eventIndex >= t.Events.Count)
            throw new Exception("Invalid event_index");
        return t.Events[eventIndex];
    }

    private string TrimEvent(RpcRequest req)
    {
        Project project = RequireProject();
        int trackIndex = Json.GetInt(req.ParamsJson, "track_index", -1);
        int eventIndex = Json.GetInt(req.ParamsJson, "event_index", -1);
        double start = Json.GetDouble(req.ParamsJson, "start_seconds", double.NaN);
        double length = Json.GetDouble(req.ParamsJson, "length_seconds", double.NaN);
        double takeOffset = Json.GetDouble(req.ParamsJson, "take_offset_seconds", double.NaN);
        TrackEvent ev;
        try { ev = GetEvent(project, trackIndex, eventIndex); }
        catch (Exception ex) { return SoftFail(req.Id, ex.Message); }
        try
        {
            if (!double.IsNaN(start))
                ev.Start = SecondsToTimecode(start);
            if (!double.IsNaN(length))
                ev.Length = SecondsToTimecode(length);
            if (!double.IsNaN(takeOffset))
            {
                Take take = ev.ActiveTake;
                if (take == null)
                    return SoftFail(req.Id, "No active take to set offset");
                take.Offset = SecondsToTimecode(takeOffset);
            }
        }
        catch (Exception ex)
        {
            return SoftFail(req.Id, "Trim failed: " + ex.Message);
        }
        StringBuilder trimSb = new StringBuilder();
        trimSb.Append("{\"ok\":true")
              .Append(",\"start_seconds\":").Append(Json.Num(TimecodeToSeconds(ev.Start)))
              .Append(",\"length_seconds\":").Append(Json.Num(TimecodeToSeconds(ev.Length)));
        try
        {
            Take take = ev.ActiveTake;
            if (take != null && take.Offset != null)
                trimSb.Append(",\"take_offset_seconds\":").Append(Json.Num(TimecodeToSeconds(take.Offset)));
        }
        catch { }
        trimSb.Append("}");
        return RpcResponse.Result(req.Id, trimSb.ToString());
    }

    private string MoveEvent(RpcRequest req)
    {
        Project project = RequireProject();
        int trackIndex = Json.GetInt(req.ParamsJson, "track_index", -1);
        int eventIndex = Json.GetInt(req.ParamsJson, "event_index", -1);
        double start = Json.GetDouble(req.ParamsJson, "start_seconds", 0);
        TrackEvent ev;
        try { ev = GetEvent(project, trackIndex, eventIndex); }
        catch (Exception ex) { return SoftFail(req.Id, ex.Message); }
        try { ev.Start = SecondsToTimecode(start); }
        catch (Exception ex) { return SoftFail(req.Id, "Move failed: " + ex.Message); }
        return RpcResponse.Result(req.Id,
            "{\"ok\":true,\"start_seconds\":" + Json.Num(TimecodeToSeconds(ev.Start)) + "}");
    }

    private string DeleteEvent(RpcRequest req)
    {
        Project project = RequireProject();
        int trackIndex = Json.GetInt(req.ParamsJson, "track_index", -1);
        int eventIndex = Json.GetInt(req.ParamsJson, "event_index", -1);
        TrackEvent ev;
        try { ev = GetEvent(project, trackIndex, eventIndex); }
        catch (Exception ex) { return SoftFail(req.Id, ex.Message); }
        try { ev.Track.Events.Remove(ev); }
        catch (Exception ex) { return SoftFail(req.Id, "Delete failed: " + ex.Message); }
        return RpcResponse.Result(req.Id, "{\"ok\":true}");
    }


    private static CurveType ParseCurve(string name, CurveType fallback)
    {
        if (name == null || name.Length == 0) return fallback;
        switch (name.Trim().ToLowerInvariant())
        {
            case "fast": return CurveType.Fast;
            case "slow": return CurveType.Slow;
            case "linear": return CurveType.Linear;
            case "sharp": return CurveType.Sharp;
            case "smooth": return CurveType.Smooth;
            default: return fallback;
        }
    }

    private static EnvelopeType? ParseEnvelopeType(string name)
    {
        if (name == null) return null;
        switch (name.Trim().ToLowerInvariant())
        {
            case "volume": return EnvelopeType.Volume;
            case "composite":
            case "compositelevel":
            case "composite_level":
            case "opacity": return EnvelopeType.Composite; // video track composite level
            case "pan": return EnvelopeType.Pan;
            case "fadetocolor":
            case "fade_to_color": return EnvelopeType.FadeToColor;
            case "mute": return EnvelopeType.Mute;
            // FadeIn/FadeOut are TrackEvent.Fade* properties, not EnvelopeType
            default: return null;
        }
    }

    private void ResetVideoMotionIdentity(VideoEvent ve)
    {
        VideoMotion motion = ve.VideoMotion;
        // Remove extra keyframes (keep [0] at Position 0)
        for (int i = motion.Keyframes.Count - 1; i >= 1; i--)
        {
            try { motion.Keyframes.RemoveAt(i); } catch { }
        }
        VideoMotionKeyframe key0 = motion.Keyframes[0];
        int w = myVegas.Project.Video.Width;
        int h = myVegas.Project.Video.Height;
        try
        {
            Take take = ve.ActiveTake;
            if (take != null && take.Media != null)
            {
                MediaStream ms = take.Media.Streams.GetItemByMediaType(MediaType.Video, 0);
                VideoStream vs = ms as VideoStream;
                if (vs != null && vs.Width > 0 && vs.Height > 0)
                {
                    w = vs.Width;
                    h = vs.Height;
                }
            }
        }
        catch { }
        try
        {
            key0.Bounds = new VideoMotionBounds(
                new VideoMotionVertex(0, 0),
                new VideoMotionVertex(w, 0),
                new VideoMotionVertex(w, h),
                new VideoMotionVertex(0, h));
        }
        catch { }
    }

    private VideoKeyframeType ParseVideoKeyframeType(string name, VideoKeyframeType fallback)
    {
        if (name == null || name.Length == 0) return fallback;
        switch (name.Trim().ToLowerInvariant())
        {
            case "linear": return VideoKeyframeType.Linear;
            case "fast": return VideoKeyframeType.Fast;
            case "slow": return VideoKeyframeType.Slow;
            case "smooth": return VideoKeyframeType.Smooth;
            case "sharp": return VideoKeyframeType.Sharp;
            case "hold": return VideoKeyframeType.Hold;
            default: return fallback;
        }
    }

    private void GetMediaSize(VideoEvent ve, out int w, out int h)
    {
        w = myVegas.Project.Video.Width;
        h = myVegas.Project.Video.Height;
        try
        {
            Take take = ve.ActiveTake;
            if (take != null && take.Media != null)
            {
                MediaStream ms = take.Media.Streams.GetItemByMediaType(MediaType.Video, 0);
                VideoStream vs = ms as VideoStream;
                if (vs != null && vs.Width > 0 && vs.Height > 0)
                {
                    w = vs.Width;
                    h = vs.Height;
                }
            }
        }
        catch { }
    }

    private void ApplyMotionKeyframe(VideoMotionKeyframe key, VideoEvent ve, double scale, double panX, double panY,
                                     string typeName, double smoothness)
    {
        // Real Event Pan/Crop zoom: shrink the SOURCE crop rectangle so the output
        // still fills the frame (no black gaps). ScaleBy+MoveBy was translating the
        // whole picture on the canvas instead of zooming it.
        if (scale < 1.0) scale = 1.0;
        if (scale > 3.0) scale = 3.0;

        int w, h;
        GetMediaSize(ve, out w, out h);

        double cropW = w / scale;
        double cropH = h / scale;
        double maxX = Math.Max(0.0, w - cropW);
        double maxY = Math.Max(0.0, h - cropH);
        // pan -1..1: -1 = top/left edge of source, 0 = centered, +1 = bottom/right
        double left = maxX * (panX + 1.0) * 0.5;
        double top = maxY * (panY + 1.0) * 0.5;
        if (left < 0) left = 0;
        if (top < 0) top = 0;
        if (left > maxX) left = maxX;
        if (top > maxY) top = maxY;

        float l = (float)left;
        float tp = (float)top;
        float r = (float)(left + cropW);
        float b = (float)(top + cropH);
        key.Bounds = new VideoMotionBounds(
            new VideoMotionVertex(l, tp),
            new VideoMotionVertex(r, tp),
            new VideoMotionVertex(r, b),
            new VideoMotionVertex(l, b));

        // Magix temporal interpolation (Smooth/Fast/Slow/Hold/…) + spatial Smoothness
        try { key.Type = ParseVideoKeyframeType(typeName, VideoKeyframeType.Smooth); } catch { }
        if (!double.IsNaN(smoothness))
        {
            try { key.Smoothness = (float)Math.Max(0.0, Math.Min(1.0, smoothness)); } catch { }
        }
    }

    private string SetEventMotion(RpcRequest req)
    {
        Project project = RequireProject();
        int trackIndex = Json.GetInt(req.ParamsJson, "track_index", -1);
        int eventIndex = Json.GetInt(req.ParamsJson, "event_index", -1);
        bool reset = Json.GetBool(req.ParamsJson, "reset", true);
        TrackEvent ev;
        try { ev = GetEvent(project, trackIndex, eventIndex); }
        catch (Exception ex) { return SoftFail(req.Id, ex.Message); }
        VideoEvent ve = ev as VideoEvent;
        if (ve == null)
            return SoftFail(req.Id, "event.set_motion requires a VideoEvent");

        try
        {
            if (reset)
                ResetVideoMotionIdentity(ve);

            string arr = Json.GetArray(req.ParamsJson, "keyframes") ?? "[]";
            System.Collections.Generic.List<string> objs = Json.EnumerateObjects(arr);
            int added = 0;
            foreach (string obj in objs)
            {
                double at = Json.GetDouble(obj, "at_seconds", 0);
                double scale = Json.GetDouble(obj, "scale", 1.0);
                double panX = Json.GetDouble(obj, "pan_x", 0);
                double panY = Json.GetDouble(obj, "pan_y", 0);
                string typeName = Json.GetString(obj, "type")
                    ?? Json.GetString(obj, "curve")
                    ?? "smooth";
                double smoothness = Json.GetDouble(obj, "smoothness", 0.5);

                VideoMotionKeyframe key = null;
                if (at <= 0.0001 && ve.VideoMotion.Keyframes.Count > 0)
                {
                    key = ve.VideoMotion.Keyframes[0];
                }
                else
                {
                    key = new VideoMotionKeyframe(SecondsToTimecode(at));
                    ve.VideoMotion.Keyframes.Add(key);
                }
                ApplyMotionKeyframe(key, ve, scale, panX, panY, typeName, smoothness);
                added++;
            }
            return RpcResponse.Result(req.Id,
                "{\"ok\":true,\"keyframes_applied\":" + added + ",\"reset\":" + (reset ? "true" : "false") + "}");
        }
        catch (Exception ex)
        {
            return SoftFail(req.Id, "set_motion failed: " + ex.Message);
        }
    }

    private string SetEventFades(RpcRequest req)
    {
        Project project = RequireProject();
        int trackIndex = Json.GetInt(req.ParamsJson, "track_index", -1);
        int eventIndex = Json.GetInt(req.ParamsJson, "event_index", -1);
        double fadeIn = Json.GetDouble(req.ParamsJson, "fade_in_seconds", double.NaN);
        double fadeOut = Json.GetDouble(req.ParamsJson, "fade_out_seconds", double.NaN);
        bool dissolve = Json.GetBool(req.ParamsJson, "dissolve", false);
        string curveName = Json.GetString(req.ParamsJson, "curve") ?? "smooth";
        string reciprocalName = Json.GetString(req.ParamsJson, "reciprocal_curve");
        CurveType curve = ParseCurve(curveName, CurveType.Smooth);

        TrackEvent ev;
        try { ev = GetEvent(project, trackIndex, eventIndex); }
        catch (Exception ex) { return SoftFail(req.Id, ex.Message); }

        try
        {
            if (!double.IsNaN(fadeIn))
            {
                ev.FadeIn.Length = SecondsToTimecode(fadeIn);
                ev.FadeIn.Curve = curve;
                if (reciprocalName != null && reciprocalName.Length > 0)
                    ev.FadeIn.ReciprocalCurve = ParseCurve(reciprocalName, curve);
            }
            if (!double.IsNaN(fadeOut))
            {
                ev.FadeOut.Length = SecondsToTimecode(fadeOut);
                ev.FadeOut.Curve = curve;
            }

            VideoEvent ve = ev as VideoEvent;
            if (dissolve && ve != null)
            {
                PlugInNode plugIn = myVegas.Transitions.GetChildByName("Dissolve");
                if (plugIn == null)
                    return SoftFail(req.Id, "Dissolve transition plug-in not found");
                if (!double.IsNaN(fadeIn) && fadeIn > 0)
                {
                    Effect fxIn = new Effect(plugIn);
                    ve.FadeIn.Transition = fxIn;
                }
                if (!double.IsNaN(fadeOut) && fadeOut > 0)
                {
                    Effect fxOut = new Effect(plugIn);
                    ve.FadeOut.Transition = fxOut;
                }
            }

            StringBuilder sb = new StringBuilder();
            sb.Append("{\"ok\":true");
            try { sb.Append(",\"fade_in_seconds\":").Append(Json.Num(TimecodeToSeconds(ev.FadeIn.Length))); } catch { }
            try { sb.Append(",\"fade_out_seconds\":").Append(Json.Num(TimecodeToSeconds(ev.FadeOut.Length))); } catch { }
            sb.Append(",\"dissolve\":").Append(dissolve ? "true" : "false");
            sb.Append("}");
            return RpcResponse.Result(req.Id, sb.ToString());
        }
        catch (Exception ex)
        {
            return SoftFail(req.Id, "set_fades failed: " + ex.Message);
        }
    }

    private string SetEventOpacity(RpcRequest req)
    {
        Project project = RequireProject();
        int trackIndex = Json.GetInt(req.ParamsJson, "track_index", -1);
        int eventIndex = Json.GetInt(req.ParamsJson, "event_index", -1);
        double opacity = Json.GetDouble(req.ParamsJson, "opacity", double.NaN);
        if (double.IsNaN(opacity))
            opacity = Json.GetDouble(req.ParamsJson, "gain", double.NaN);
        if (double.IsNaN(opacity))
            return SoftFail(req.Id, "params.opacity (0..1) required");
        if (opacity < 0) opacity = 0;
        if (opacity > 1) opacity = 1;

        TrackEvent ev;
        try { ev = GetEvent(project, trackIndex, eventIndex); }
        catch (Exception ex) { return SoftFail(req.Id, ex.Message); }
        VideoEvent ve = ev as VideoEvent;
        if (ve == null)
            return SoftFail(req.Id, "event.set_opacity requires a VideoEvent");
        try
        {
            // Magix FAQ: individual event opacity via FadeIn.Gain
            ve.FadeIn.Gain = (float)opacity;
            return RpcResponse.Result(req.Id,
                "{\"ok\":true,\"opacity\":" + Json.Num(opacity) + "}");
        }
        catch (Exception ex)
        {
            return SoftFail(req.Id, "set_opacity failed: " + ex.Message);
        }
    }

    private string AddTitleEvent(RpcRequest req)
    {
        Project project = RequireProject();
        int trackIndex = Json.GetInt(req.ParamsJson, "track_index", -1);
        double start = Json.GetDouble(req.ParamsJson, "start_seconds", 0);
        double length = Json.GetDouble(req.ParamsJson, "length_seconds", 5);
        string text = Json.GetString(req.ParamsJson, "text") ?? "";
        string preset = Json.GetString(req.ParamsJson, "preset") ?? "(Default)";

        if (trackIndex < 0 || trackIndex >= project.Tracks.Count || !project.Tracks[trackIndex].IsVideo())
            return SoftFail(req.Id, "Invalid video track_index");

        PlugInNode plugIn = null;
        try { plugIn = myVegas.Generators.GetChildByName("Titles & Text"); } catch { }
        if (plugIn == null)
            return SoftFail(req.Id, "Titles & Text generator not found — use PNG overlays as backup");

        try
        {
            Media media = new Media(plugIn);
            try { media.Generator.Preset = preset; } catch { }

            VideoTrack vtrack = (VideoTrack)project.Tracks[trackIndex];
            VideoEvent ve = vtrack.AddVideoEvent(SecondsToTimecode(start), SecondsToTimecode(length));
            Take take = ve.AddTake(media.GetVideoStreamByIndex(0));

            // Magix FAQ SetTextForTitle — OFX String "Text" via RichTextBox RTF
            try
            {
                Effect fxTX = ve.ActiveTake.Media.Generator;
                if (fxTX != null && fxTX.OFXEffect != null)
                {
                    OFXEffect ofxTX = fxTX.OFXEffect;
                    OFXStringParameter parText = ofxTX.FindParameterByName("Text") as OFXStringParameter;
                    if (parText != null)
                    {
                        RichTextBox rbx = new RichTextBox();
                        try
                        {
                            if (parText.Value != null && parText.Value.Length > 0)
                                rbx.Rtf = parText.Value;
                        }
                        catch { }
                        System.Drawing.Font savedFont = null;
                        HorizontalAlignment savedAlignment = HorizontalAlignment.Center;
                        try
                        {
                            rbx.SelectAll();
                            savedFont = rbx.SelectionFont;
                            savedAlignment = rbx.SelectionAlignment;
                        }
                        catch { }
                        rbx.Rtf = "";
                        rbx.AppendText(text ?? "");
                        rbx.SelectAll();
                        try
                        {
                            if (savedFont != null) rbx.SelectionFont = savedFont;
                            rbx.SelectionAlignment = savedAlignment;
                        }
                        catch
                        {
                            try
                            {
                                rbx.SelectionFont = new System.Drawing.Font("Arial", 48, System.Drawing.FontStyle.Bold);
                                rbx.SelectionAlignment = HorizontalAlignment.Center;
                            }
                            catch { }
                        }
                        parText.Value = rbx.Rtf;
                        try { ofxTX.AllParametersChanged(); } catch { }
                    }
                }
            }
            catch (Exception tex)
            {
                // Title event exists; text set failed — still ok with note
                return RpcResponse.Result(req.Id,
                    "{\"ok\":true,\"track_index\":" + trackIndex +
                    ",\"event_index\":" + (vtrack.Events.Count - 1) +
                    ",\"start_seconds\":" + Json.Num(start) +
                    ",\"length_seconds\":" + Json.Num(length) +
                    ",\"text_set\":false,\"warning\":" + Json.Str(tex.Message) + "}");
            }

            return RpcResponse.Result(req.Id,
                "{\"ok\":true,\"track_index\":" + trackIndex +
                ",\"event_index\":" + (vtrack.Events.Count - 1) +
                ",\"start_seconds\":" + Json.Num(start) +
                ",\"length_seconds\":" + Json.Num(length) +
                ",\"text_set\":true}");
        }
        catch (Exception ex)
        {
            return SoftFail(req.Id, "add_title failed: " + ex.Message);
        }
    }

    private string SetTrackCompositeLevel(RpcRequest req)
    {
        Project project = RequireProject();
        int trackIndex = Json.GetInt(req.ParamsJson, "track_index", -1);
        double level = Json.GetDouble(req.ParamsJson, "level", double.NaN);
        if (double.IsNaN(level))
            level = Json.GetDouble(req.ParamsJson, "composite_level", double.NaN);
        if (double.IsNaN(level))
            return SoftFail(req.Id, "params.level (0..1) required");
        if (level < 0) level = 0;
        if (level > 1) level = 1;
        if (trackIndex < 0 || trackIndex >= project.Tracks.Count)
            return SoftFail(req.Id, "Invalid track_index");
        VideoTrack vt = project.Tracks[trackIndex] as VideoTrack;
        if (vt == null)
            return SoftFail(req.Id, "track.set_composite_level requires a VideoTrack");
        try
        {
            vt.CompositeLevel = (float)level;
            return RpcResponse.Result(req.Id,
                "{\"ok\":true,\"track_index\":" + trackIndex + ",\"level\":" + Json.Num(level) + "}");
        }
        catch (Exception ex)
        {
            return SoftFail(req.Id, "set_composite_level failed: " + ex.Message);
        }
    }

    private string SetEnvelopePoints(RpcRequest req)
    {
        Project project = RequireProject();
        int trackIndex = Json.GetInt(req.ParamsJson, "track_index", -1);
        string typeName = Json.GetString(req.ParamsJson, "envelope_type")
            ?? Json.GetString(req.ParamsJson, "type")
            ?? "Volume";
        EnvelopeType? et = ParseEnvelopeType(typeName);
        if (et == null)
            return SoftFail(req.Id, "Unknown envelope_type: " + typeName);
        if (trackIndex < 0 || trackIndex >= project.Tracks.Count)
            return SoftFail(req.Id, "Invalid track_index");

        Track track = project.Tracks[trackIndex];
        try
        {
            Envelope envelope = null;
            try { envelope = track.Envelopes.FindByType(et.Value); } catch { }
            if (envelope == null)
            {
                envelope = new Envelope(et.Value);
                track.Envelopes.Add(envelope);
            }

            string arr = Json.GetArray(req.ParamsJson, "points") ?? "[]";
            System.Collections.Generic.List<string> objs = Json.EnumerateObjects(arr);
            int added = 0;
            foreach (string obj in objs)
            {
                double at = Json.GetDouble(obj, "at_seconds", 0);
                double value = Json.GetDouble(obj, "value", envelope.Neutral);
                string curveName = Json.GetString(obj, "curve") ?? "smooth";
                CurveType curve = ParseCurve(curveName, CurveType.Smooth);
                if (value < envelope.Min) value = envelope.Min;
                if (value > envelope.Max) value = envelope.Max;

                // Skip duplicate positions — update existing if same time
                EnvelopePoint existing = null;
                foreach (EnvelopePoint p in envelope.Points)
                {
                    try
                    {
                        if (Math.Abs(TimecodeToSeconds(p.X) - at) < 0.001)
                        {
                            existing = p;
                            break;
                        }
                    }
                    catch { }
                }
                if (existing != null)
                {
                    existing.Y = (float)value;
                    try { existing.Curve = curve; } catch { }
                }
                else
                {
                    EnvelopePoint pt = new EnvelopePoint(SecondsToTimecode(at), (float)value, curve);
                    envelope.Points.Add(pt);
                }
                added++;
            }
            return RpcResponse.Result(req.Id,
                "{\"ok\":true,\"track_index\":" + trackIndex +
                ",\"envelope_type\":" + Json.Str(typeName) +
                ",\"points_applied\":" + added + "}");
        }
        catch (Exception ex)
        {
            return SoftFail(req.Id, "envelope.set_points failed: " + ex.Message);
        }
    }

    private string GetSelectedEvents(RpcRequest req)
    {
        Project project = RequireProject();
        StringBuilder sb = new StringBuilder();
        sb.Append("{\"ok\":true,\"events\":[");
        bool first = true;
        int count = 0;
        for (int ti = 0; ti < project.Tracks.Count; ti++)
        {
            Track t = project.Tracks[ti];
            for (int ei = 0; ei < t.Events.Count; ei++)
            {
                TrackEvent ev = t.Events[ei];
                if (!ev.Selected) continue;
                if (!first) sb.Append(",");
                first = false;
                sb.Append("{\"track_index\":").Append(ti)
                  .Append(",\"event_index\":").Append(ei)
                  .Append(",\"start_seconds\":").Append(Json.Num(TimecodeToSeconds(ev.Start)))
                  .Append(",\"length_seconds\":").Append(Json.Num(TimecodeToSeconds(ev.Length)))
                  .Append("}");
                count++;
            }
        }
        sb.Append("],\"count\":").Append(count).Append("}");
        return RpcResponse.Result(req.Id, sb.ToString());
    }

    private string RenderStart(RpcRequest req)
    {
        // Phase 2/4 bridge: best-effort RenderArgs when template is named;
        // otherwise SoftFail with clear File>Render As guidance (no fake success).
        string outputPath = Json.GetString(req.ParamsJson, "output_path");
        string templateName = Json.GetString(req.ParamsJson, "template_name");
        string rendererName = Json.GetString(req.ParamsJson, "renderer_name");
        if (outputPath == null || outputPath.Length == 0)
        {
            return SoftFail(req.Id,
                "render.start requires output_path. Long renders can block the host dialog — prefer File > Render As for now, or pass renderer_name + template_name for best-effort API render.");
        }
        if ((templateName == null || templateName.Length == 0) &&
            (rendererName == null || rendererName.Length == 0))
        {
            return SoftFail(req.Id,
                "render.start: pass template_name (and optionally renderer_name) matching an existing VEGAS render template, or use File > Render As. No silent/fake success.");
        }
        try
        {
            RenderTemplate template = null;
            string foundRenderer = null;
            foreach (Renderer renderer in myVegas.Renderers)
            {
                string rn = null;
                try { rn = renderer.FileTypeName; } catch { try { rn = renderer.Name; } catch { } }
                if (rendererName != null && rendererName.Length > 0)
                {
                    bool match = false;
                    try { if (rn != null && string.Equals(rn, rendererName, StringComparison.OrdinalIgnoreCase)) match = true; } catch { }
                    try { if (!match && renderer.Name != null && string.Equals(renderer.Name, rendererName, StringComparison.OrdinalIgnoreCase)) match = true; } catch { }
                    if (!match) continue;
                }
                try
                {
                    foreach (RenderTemplate t in renderer.Templates)
                    {
                        if (templateName == null || templateName.Length == 0 ||
                            string.Equals(t.Name, templateName, StringComparison.OrdinalIgnoreCase))
                        {
                            template = t;
                            foundRenderer = rn ?? rendererName;
                            break;
                        }
                    }
                }
                catch { }
                if (template != null) break;
            }
            if (template == null)
            {
                return SoftFail(req.Id,
                    "Render template not found (renderer_name=" + (rendererName ?? "") +
                    ", template_name=" + (templateName ?? "") +
                    "). Create/save a template in VEGAS UI, or use File > Render As.");
            }

            RenderArgs args = new RenderArgs();
            args.OutputFile = outputPath;
            args.RenderTemplate = template;
            double start = Json.GetDouble(req.ParamsJson, "start_seconds", double.NaN);
            double length = Json.GetDouble(req.ParamsJson, "length_seconds", double.NaN);
            if (!double.IsNaN(start)) args.Start = SecondsToTimecode(start);
            if (!double.IsNaN(length)) args.Length = SecondsToTimecode(length);

            bool asyncRender = Json.GetBool(req.ParamsJson, "async", true);
            if (!asyncRender)
            {
                RenderStatus status = myVegas.Render(args);
                return RpcResponse.Result(req.Id,
                    "{\"ok\":true,\"async\":false,\"status\":" + Json.Str(status.ToString()) +
                    ",\"state\":" + Json.Str(status.ToString()) +
                    ",\"output_path\":" + Json.Str(outputPath) +
                    ",\"renderer\":" + Json.Str(foundRenderer ?? "") +
                    ",\"template\":" + Json.Str(template.Name) + "}");
            }

            // Vegas.BeginRender starts the encode and returns; progress comes in
            // RenderProgress / RenderFinished. Do not call the blocking Render()
            // on the UI-marshal wait — that froze the host dialog for minutes.
            string jobId = Guid.NewGuid().ToString("N").Substring(0, 8);
            lock (renderGate)
            {
                renderJobId = jobId;
                renderState = "running";
                renderPercent = 0;
                renderOutput = outputPath;
                renderError = "";
            }
            EnsureRenderHooks();
            RenderStatus began = myVegas.BeginRender(args);
            return RpcResponse.Result(req.Id,
                "{\"ok\":true,\"async\":true,\"job_id\":" + Json.Str(jobId) +
                ",\"state\":\"running\",\"status\":" + Json.Str(began.ToString()) +
                ",\"output_path\":" + Json.Str(outputPath) +
                ",\"renderer\":" + Json.Str(foundRenderer ?? "") +
                ",\"template\":" + Json.Str(template.Name) + "}");
        }
        catch (Exception ex)
        {
            lock (renderGate) { renderState = "failed"; renderError = ex.Message; }
            return SoftFail(req.Id,
                "Render failed: " + ex.Message + ". Prefer File > Render As for now.");
        }
    }

    private void EnsureRenderHooks()
    {
        if (renderHooksAttached) return;
        myVegas.RenderProgress += OnRenderProgress;
        myVegas.RenderFinished += OnRenderFinished;
        renderHooksAttached = true;
    }

    private void OnRenderProgress(object sender, RenderStatusEventArgs e)
    {
        try
        {
            lock (renderGate)
            {
                renderPercent = e.PercentComplete;
                if (renderState == "idle") renderState = "running";
            }
        }
        catch { }
    }

    private void OnRenderFinished(object sender, RenderStatusEventArgs e)
    {
        try
        {
            string st = "";
            try { st = e.Status.ToString(); } catch { }
            lock (renderGate)
            {
                if (st.IndexOf("Complete", StringComparison.OrdinalIgnoreCase) >= 0 ||
                    st.IndexOf("Finish", StringComparison.OrdinalIgnoreCase) >= 0)
                    renderState = "complete";
                else if (st.IndexOf("Cancel", StringComparison.OrdinalIgnoreCase) >= 0)
                    renderState = "cancelled";
                else
                    renderState = "failed";
                try
                {
                    if (e.ErrorMessage != null && e.ErrorMessage.Length > 0)
                        renderError = e.ErrorMessage;
                }
                catch { }
                renderPercent = 100;
            }
        }
        catch
        {
            lock (renderGate) { renderState = "failed"; }
        }
    }

    private string RenderStatusQuery(RpcRequest req)
    {
        string want = Json.GetString(req.ParamsJson, "job_id");
        lock (renderGate)
        {
            if (want != null && want.Length > 0 && renderJobId.Length > 0 &&
                !string.Equals(want, renderJobId, StringComparison.OrdinalIgnoreCase))
            {
                return SoftFail(req.Id, "unknown job_id (host keeps one active render)");
            }
            return RpcResponse.Result(req.Id,
                "{\"ok\":true,\"job_id\":" + Json.Str(renderJobId) +
                ",\"state\":" + Json.Str(renderState) +
                ",\"percent\":" + renderPercent +
                ",\"output_path\":" + Json.Str(renderOutput) +
                ",\"error\":" + Json.Str(renderError) + "}");
        }
    }

    private string SetProjectVideo(RpcRequest req)
    {
        Project project = RequireProject();
        int w = Json.GetInt(req.ParamsJson, "width", 0);
        int h = Json.GetInt(req.ParamsJson, "height", 0);
        double fps = Json.GetDouble(req.ParamsJson, "frame_rate", double.NaN);
        try
        {
            if (w > 0) project.Video.Width = w;
            if (h > 0) project.Video.Height = h;
            if (!double.IsNaN(fps) && fps > 0) project.Video.FrameRate = fps;
            return RpcResponse.Result(req.Id,
                "{\"ok\":true,\"width\":" + project.Video.Width +
                ",\"height\":" + project.Video.Height +
                ",\"frame_rate\":" + Json.Num(project.Video.FrameRate) + "}");
        }
        catch (Exception ex)
        {
            return SoftFail(req.Id, "project.set_video failed: " + ex.Message);
        }
    }

    private string TransportPlay(RpcRequest req)
    {
        myVegas.Transport.Play();
        return RpcResponse.Result(req.Id, "{\"ok\":true}");
    }

    private string TransportStop(RpcRequest req)
    {
        myVegas.Transport.Stop();
        return RpcResponse.Result(req.Id, "{\"ok\":true}");
    }

    private string TransportSeek(RpcRequest req)
    {
        double seconds = Json.GetDouble(req.ParamsJson, "seconds", 0);
        myVegas.Transport.CursorPosition = SecondsToTimecode(seconds);
        return RpcResponse.Result(req.Id, "{\"ok\":true,\"seconds\":" + Json.Num(seconds) + "}");
    }

    // ===== Phase 3 (2026-10-22): project lifecycle, media pool, track props, envelope clear, event gain, render templates =====
    private static readonly string[] LongMethods = { "project.save", "project.new", "project.open", "render.start" };

    private bool SaveOrDiscard(RpcRequest req, out string fail)
    {
        fail = null;
        string saveTo = Json.GetString(req.ParamsJson, "save_current_to");
        bool discard = Json.GetBool(req.ParamsJson, "discard_unsaved", false);
        if (saveTo != null && saveTo.Length > 0)
        {
            if (!myVegas.SaveProject(saveTo)) { fail = "Saving the current project failed: " + saveTo; return false; }
            return true;
        }
        if (!discard) { fail = "Pass save_current_to (path) to save the open project first, or discard_unsaved=true."; return false; }
        return true;
    }

    private string ProjectNew(RpcRequest req)
    {
        string fail;
        if (!SaveOrDiscard(req, out fail)) return SoftFail(req.Id, fail);
        bool ok = myVegas.NewProject(false, false);
        return RpcResponse.Result(req.Id, "{\"ok\":" + (ok ? "true" : "false") + "}");
    }

    private string ProjectOpen(RpcRequest req)
    {
        string path = Json.GetString(req.ParamsJson, "path");
        if (path == null || !File.Exists(path)) return SoftFail(req.Id, "project file not found: " + path);
        string fail;
        if (!SaveOrDiscard(req, out fail)) return SoftFail(req.Id, fail);
        myVegas.NewProject(false, false);   // closes the current one without a save prompt (already saved/discarded above)
        bool ok = myVegas.OpenProject(path);
        return RpcResponse.Result(req.Id, "{\"ok\":" + (ok ? "true" : "false") + ",\"path\":" + Json.Str(path) + "}");
    }

    private string ListMedia(RpcRequest req)
    {
        Project project = RequireProject();
        StringBuilder sb = new StringBuilder("{\"ok\":true,\"media\":[");
        bool first = true;
        foreach (Media m in project.MediaPool)
        {
            if (!first) sb.Append(",");
            first = false;
            string alpha = "";
            bool hasVideo = false, off = false;
            try { hasVideo = m.HasVideo(); } catch { }
            try { if (hasVideo) alpha = m.GetVideoStreamByIndex(0).AlphaChannel.ToString(); } catch { }
            try { off = m.IsOffline(); } catch { }
            double len = 0;
            try { len = TimecodeToSeconds(m.Length); } catch { }
            sb.Append("{\"path\":").Append(Json.Str(m.FilePath))
              .Append(",\"offline\":").Append(off ? "true" : "false")
              .Append(",\"has_video\":").Append(hasVideo ? "true" : "false")
              .Append(",\"length_seconds\":").Append(Json.Num(len))
              .Append(",\"alpha\":").Append(Json.Str(alpha)).Append("}");
        }
        sb.Append("]}");
        return RpcResponse.Result(req.Id, sb.ToString());
    }

    private string ReplaceMedia(RpcRequest req)
    {
        Project project = RequireProject();
        string oldPath = Json.GetString(req.ParamsJson, "old_path");
        string newPath = Json.GetString(req.ParamsJson, "new_path");
        if (oldPath == null || oldPath.Length == 0) return SoftFail(req.Id, "params.old_path required");
        if (newPath == null || !File.Exists(newPath)) return SoftFail(req.Id, "new_path not found: " + newPath);
        Media oldM = FindMediaByPath(project, oldPath);
        if (oldM == null) return SoftFail(req.Id, "old_path is not in the media pool: " + oldPath);
        Media newM = EnsureMedia(project, newPath);
        oldM.ReplaceWith(newM);
        return RpcResponse.Result(req.Id, "{\"ok\":true,\"old_path\":" + Json.Str(oldPath) + ",\"new_path\":" + Json.Str(newPath) + "}");
    }

    private string SetMediaAlpha(RpcRequest req)
    {
        Project project = RequireProject();
        string path = Json.GetString(req.ParamsJson, "path") ?? "";
        string contains = Json.GetString(req.ParamsJson, "path_contains") ?? "";
        string mode = Json.GetString(req.ParamsJson, "alpha") ?? "Straight";
        if (path.Length == 0 && contains.Length == 0) return SoftFail(req.Id, "pass path or path_contains");
        VideoAlphaType a;
        try { a = (VideoAlphaType)Enum.Parse(typeof(VideoAlphaType), mode, true); }
        catch { return SoftFail(req.Id, "alpha must be one of: " + string.Join(", ", Enum.GetNames(typeof(VideoAlphaType)))); }
        int n = 0;
        foreach (Media m in project.MediaPool)
        {
            string fp = m.FilePath ?? "";
            bool match = (path.Length > 0 && string.Equals(fp, path, StringComparison.OrdinalIgnoreCase)) ||
                         (contains.Length > 0 && fp.IndexOf(contains, StringComparison.OrdinalIgnoreCase) >= 0);
            if (!match) continue;
            try { if (m.HasVideo()) { m.GetVideoStreamByIndex(0).AlphaChannel = a; n++; } } catch { }
        }
        return RpcResponse.Result(req.Id, "{\"ok\":true,\"alpha\":" + Json.Str(a.ToString()) + ",\"media_updated\":" + n + "}");
    }

    private string SetTrackProps(RpcRequest req)
    {
        Project project = RequireProject();
        int ti = Json.GetInt(req.ParamsJson, "track_index", -1);
        if (ti < 0 || ti >= project.Tracks.Count) return SoftFail(req.Id, "Invalid track_index");
        Track t = project.Tracks[ti];
        string name = Json.GetString(req.ParamsJson, "name");
        if (name != null) t.Name = name;
        if (Json.GetRaw(req.ParamsJson, "mute") != null) t.Mute = Json.GetBool(req.ParamsJson, "mute", t.Mute);
        if (Json.GetRaw(req.ParamsJson, "solo") != null) t.Solo = Json.GetBool(req.ParamsJson, "solo", t.Solo);
        double vol = Json.GetDouble(req.ParamsJson, "volume", double.NaN);
        if (!double.IsNaN(vol))
        {
            AudioTrack at = t as AudioTrack;
            if (at == null) return SoftFail(req.Id, "volume needs an audio track");
            at.Volume = (float)vol;
        }
        return RpcResponse.Result(req.Id, "{\"ok\":true,\"track_index\":" + ti + ",\"name\":" + Json.Str(t.Name) + "}");
    }

    private string ClearTrackEvents(RpcRequest req)
    {
        Project project = RequireProject();
        int ti = Json.GetInt(req.ParamsJson, "track_index", -1);
        if (ti < 0 || ti >= project.Tracks.Count) return SoftFail(req.Id, "Invalid track_index");
        Track t = project.Tracks[ti];
        int n = t.Events.Count;
        for (int i = n - 1; i >= 0; i--) t.Events.Remove(t.Events[i]);
        return RpcResponse.Result(req.Id, "{\"ok\":true,\"track_index\":" + ti + ",\"removed\":" + n + "}");
    }

    private string ClearEnvelope(RpcRequest req)
    {
        Project project = RequireProject();
        int ti = Json.GetInt(req.ParamsJson, "track_index", -1);
        if (ti < 0 || ti >= project.Tracks.Count) return SoftFail(req.Id, "Invalid track_index");
        EnvelopeType? et = ParseEnvelopeType(Json.GetString(req.ParamsJson, "envelope_type") ?? "Volume");
        if (et == null) return SoftFail(req.Id, "Unknown envelope_type");
        Track track = project.Tracks[ti];
        Envelope env = null;
        try { env = track.Envelopes.FindByType(et.Value); } catch { }
        if (env == null) return RpcResponse.Result(req.Id, "{\"ok\":true,\"removed\":false}");
        track.Envelopes.Remove(env);
        return RpcResponse.Result(req.Id, "{\"ok\":true,\"removed\":true}");
    }

    private string SetEventGain(RpcRequest req)
    {
        Project project = RequireProject();
        TrackEvent ev;
        try { ev = GetEvent(project, Json.GetInt(req.ParamsJson, "track_index", -1), Json.GetInt(req.ParamsJson, "event_index", -1)); }
        catch (Exception ex) { return SoftFail(req.Id, ex.Message); }
        AudioEvent ae = ev as AudioEvent;
        if (ae == null) return SoftFail(req.Id, "event.set_gain requires an AudioEvent");
        double gain = Json.GetDouble(req.ParamsJson, "gain", double.NaN);
        double db = Json.GetDouble(req.ParamsJson, "gain_db", double.NaN);
        if (!double.IsNaN(db)) gain = Math.Pow(10.0, db / 20.0);
        if (!double.IsNaN(gain)) ae.FadeIn.Gain = (float)gain;
        if (Json.GetRaw(req.ParamsJson, "normalize") != null) ae.Normalize = Json.GetBool(req.ParamsJson, "normalize", false);
        return RpcResponse.Result(req.Id, "{\"ok\":true,\"gain\":" + Json.Num(ae.FadeIn.Gain) + ",\"normalize\":" + (ae.Normalize ? "true" : "false") + "}");
    }

    private string ListRenderTemplates(RpcRequest req)
    {
        // limit <= 0 means unlimited. The old default of 200/400 hid MAGIX
        // AVC/AAC MP4 templates on installs with 500+ renderers (AVI/MXF first).
        string rf = Json.GetString(req.ParamsJson, "renderer_contains") ?? "";
        string tf = Json.GetString(req.ParamsJson, "template_contains") ?? "";
        int limit = Json.GetInt(req.ParamsJson, "limit", 0);
        int offset = Json.GetInt(req.ParamsJson, "offset", 0);
        if (offset < 0) offset = 0;
        StringBuilder sb = new StringBuilder("{\"ok\":true,\"templates\":[");
        int matched = 0;
        int n = 0;
        bool truncated = false;
        bool stop = false;
        foreach (Renderer r in myVegas.Renderers)
        {
            if (stop) break;
            string ft = "", rn = "";
            try { ft = r.FileTypeName ?? ""; } catch { }
            try { rn = r.Name ?? ""; } catch { }
            if (rf.Length > 0 && ft.IndexOf(rf, StringComparison.OrdinalIgnoreCase) < 0 && rn.IndexOf(rf, StringComparison.OrdinalIgnoreCase) < 0) continue;
            try
            {
                foreach (RenderTemplate t in r.Templates)
                {
                    string tn = t.Name ?? "";
                    if (tf.Length > 0 && tn.IndexOf(tf, StringComparison.OrdinalIgnoreCase) < 0) continue;
                    if (matched < offset)
                    {
                        matched++;
                        continue;
                    }
                    if (limit > 0 && n >= limit)
                    {
                        truncated = true;
                        stop = true;
                        break;
                    }
                    if (n > 0) sb.Append(",");
                    sb.Append("{\"renderer\":").Append(Json.Str(ft)).Append(",\"renderer_name\":").Append(Json.Str(rn))
                      .Append(",\"template\":").Append(Json.Str(tn));
                    try { sb.Append(",\"width\":").Append(t.VideoWidth).Append(",\"height\":").Append(t.VideoHeight)
                            .Append(",\"fps\":").Append(Json.Num(t.VideoFrameRate)).Append(",\"video_bitrate\":").Append(t.VideoBitrate); } catch { }
                    sb.Append("}");
                    n++;
                    matched++;
                }
            }
            catch { }
        }
        sb.Append("],\"count\":").Append(n)
          .Append(",\"offset\":").Append(offset)
          .Append(",\"truncated\":").Append(truncated ? "true" : "false")
          .Append(",\"limit\":").Append(limit)
          .Append("}");
        return RpcResponse.Result(req.Id, sb.ToString());
    }

    private void Log(string msg)
    {
        try
        {
            string dir = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "vegas-director-mcp");
            Directory.CreateDirectory(dir);
            File.AppendAllText(Path.Combine(dir, "host.log"),
                DateTime.Now.ToString("s") + " " + msg + Environment.NewLine);
        }
        catch { }
    }
}

public class RpcRequest
{
    public string Id;
    public string Method;
    public string ParamsJson;

    public static RpcRequest Parse(string json)
    {
        RpcRequest r = new RpcRequest();
        r.Id = Json.GetRaw(json, "id");
        r.Method = Json.GetString(json, "method");
        r.ParamsJson = Json.GetObject(json, "params") ?? "{}";
        if (r.Method == null || r.Method.Length == 0)
            throw new Exception("Missing method");
        return r;
    }
}

public static class RpcResponse
{
    public static string Result(string id, string resultJson)
    {
        return "{\"jsonrpc\":\"2.0\",\"id\":" + (id ?? "null") + ",\"result\":" + resultJson + "}";
    }

    public static string Error(string id, int code, string message)
    {
        return "{\"jsonrpc\":\"2.0\",\"id\":" + (id ?? "null") +
               ",\"error\":{\"code\":" + code + ",\"message\":" + Json.Str(message) + "}}";
    }
}

public static class Json
{
    public static string Str(string s)
    {
        if (s == null) return "null";
        StringBuilder sb = new StringBuilder();
        sb.Append('"');
        foreach (char c in s)
        {
            if (c == '"' || c == '\\') sb.Append('\\').Append(c);
            else if (c == '\n') sb.Append("\\n");
            else if (c == '\r') sb.Append("\\r");
            else if (c == '\t') sb.Append("\\t");
            else sb.Append(c);
        }
        sb.Append('"');
        return sb.ToString();
    }

    public static string Num(double d)
    {
        if (double.IsNaN(d) || double.IsInfinity(d)) return "0";
        return d.ToString("0.####", System.Globalization.CultureInfo.InvariantCulture);
    }

    public static string GetRaw(string json, string key)
    {
        string marker = "\"" + key + "\":";
        int i = json.IndexOf(marker);
        if (i < 0) return null;
        i += marker.Length;
        while (i < json.Length && char.IsWhiteSpace(json[i])) i++;
        if (i >= json.Length) return null;
        if (json[i] == '"')
        {
            int j = i + 1;
            while (j < json.Length)
            {
                if (json[j] == '\\') { j += 2; continue; }
                if (json[j] == '"') break;
                j++;
            }
            return json.Substring(i, j - i + 1);
        }
        int k = i;
        while (k < json.Length && json[k] != ',' && json[k] != '}' && json[k] != ']') k++;
        return json.Substring(i, k - i).Trim();
    }

    public static string GetString(string json, string key)
    {
        string raw = GetRaw(json, key);
        if (raw == null) return null;
        raw = raw.Trim();
        if (raw.Length >= 2 && raw[0] == '"')
        {
            StringBuilder sb = new StringBuilder();
            for (int i = 1; i < raw.Length - 1; i++)
            {
                if (raw[i] == '\\' && i + 1 < raw.Length - 1)
                {
                    char n = raw[i + 1];
                    if (n == 'n') sb.Append('\n');
                    else if (n == 'r') sb.Append('\r');
                    else if (n == 't') sb.Append('\t');
                    else sb.Append(n);
                    i++;
                }
                else sb.Append(raw[i]);
            }
            return sb.ToString();
        }
        if (raw == "null") return null;
        return raw;
    }

    public static int GetInt(string json, string key, int fallback)
    {
        string raw = GetRaw(json, key);
        if (raw == null) return fallback;
        int v;
        if (int.TryParse(raw.Trim().Trim('"'), out v)) return v;
        return fallback;
    }

    public static double GetDouble(string json, string key, double fallback)
    {
        string raw = GetRaw(json, key);
        if (raw == null) return fallback;
        double v;
        if (double.TryParse(raw.Trim().Trim('"'),
            System.Globalization.NumberStyles.Float,
            System.Globalization.CultureInfo.InvariantCulture, out v)) return v;
        return fallback;
    }

    public static string GetObject(string json, string key)
    {
        string marker = "\"" + key + "\":";
        int i = json.IndexOf(marker);
        if (i < 0) return null;
        i += marker.Length;
        while (i < json.Length && char.IsWhiteSpace(json[i])) i++;
        if (i >= json.Length || json[i] != '{') return "{}";
        int depth = 0;
        int start = i;
        for (; i < json.Length; i++)
        {
            char c = json[i];
            if (c == '"')
            {
                i++;
                while (i < json.Length)
                {
                    if (json[i] == '\\') { i += 2; continue; }
                    if (json[i] == '"') break;
                    i++;
                }
                continue;
            }
            if (c == '{') depth++;
            else if (c == '}')
            {
                depth--;
                if (depth == 0) return json.Substring(start, i - start + 1);
            }
        }
        return json.Substring(start);
    }

    public static bool GetBool(string json, string key, bool fallback)
    {
        string raw = GetRaw(json, key);
        if (raw == null) return fallback;
        raw = raw.Trim().Trim('"').ToLowerInvariant();
        if (raw == "true" || raw == "1") return true;
        if (raw == "false" || raw == "0") return false;
        return fallback;
    }

    public static string GetArray(string json, string key)
    {
        string marker = "\"" + key + "\":";
        int i = json.IndexOf(marker);
        if (i < 0) return null;
        i += marker.Length;
        while (i < json.Length && char.IsWhiteSpace(json[i])) i++;
        if (i >= json.Length || json[i] != '[') return null;
        int depth = 0;
        int start = i;
        for (; i < json.Length; i++)
        {
            char c = json[i];
            if (c == '"')
            {
                i++;
                while (i < json.Length)
                {
                    if (json[i] == '\\') { i += 2; continue; }
                    if (json[i] == '"') break;
                    i++;
                }
                continue;
            }
            if (c == '[') depth++;
            else if (c == ']')
            {
                depth--;
                if (depth == 0) return json.Substring(start, i - start + 1);
            }
        }
        return json.Substring(start);
    }

    public static System.Collections.Generic.List<string> EnumerateObjects(string arrayJson)
    {
        var list = new System.Collections.Generic.List<string>();
        if (arrayJson == null) return list;
        int i = 0;
        while (i < arrayJson.Length && arrayJson[i] != '[') i++;
        if (i >= arrayJson.Length) return list;
        i++; // past [
        while (i < arrayJson.Length)
        {
            while (i < arrayJson.Length && (char.IsWhiteSpace(arrayJson[i]) || arrayJson[i] == ',')) i++;
            if (i >= arrayJson.Length || arrayJson[i] == ']') break;
            if (arrayJson[i] != '{') { i++; continue; }
            int depth = 0;
            int start = i;
            for (; i < arrayJson.Length; i++)
            {
                char c = arrayJson[i];
                if (c == '"')
                {
                    i++;
                    while (i < arrayJson.Length)
                    {
                        if (arrayJson[i] == '\\') { i += 2; continue; }
                        if (arrayJson[i] == '"') break;
                        i++;
                    }
                    continue;
                }
                if (c == '{') depth++;
                else if (c == '}')
                {
                    depth--;
                    if (depth == 0)
                    {
                        list.Add(arrayJson.Substring(start, i - start + 1));
                        i++;
                        break;
                    }
                }
            }
        }
        return list;
    }
}
