// VegasDirectorExtension.cs — VEGAS Pro 22 Application Extension.
// Compiled into VegasDirector.dll and copied to the real Documents folder
// (Environment.GetFolderPath(MyDocuments) — follows OneDrive redirection)
// \Vegas Application Extensions.
// VEGAS loads ICustomCommandModule at startup and keeps it for the process lifetime
// (see MAGIX VEGAS Pro 22 Scripting FAQs: Application Extensions).
// InitializeModule starts the TCP host so MCP works without Tools > Scripting.
// Project writes from this module go through Control.BeginInvoke (UI thread)
// plus UndoBlock — VEGAS does not wrap extension mutations the way it wraps
// Script Menu runs, and missing UndoBlock is COM E_UNEXPECTED.

using System;
using System.Collections;
using System.Drawing;
using System.IO;
using System.Windows.Forms;
using ScriptPortal.Vegas;

public class VegasDirectorExtension : ICustomCommandModule
{
    private Vegas myVegas;
    private EntryPoint host;
    private Control marshal;
    private CustomCommand viewCommand;

    public void InitializeModule(Vegas vegas)
    {
        myVegas = vegas;
        try
        {
            marshal = new Control();
            // Force a window handle on the VEGAS UI thread so BeginInvoke works.
            IntPtr unused = marshal.Handle;
            host = new EntryPoint();
            host.StartHeadless(vegas, marshal);
        }
        catch (Exception ex)
        {
            try
            {
                string dir = Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                    "vegas-director-mcp");
                Directory.CreateDirectory(dir);
                File.AppendAllText(
                    Path.Combine(dir, "host.log"),
                    DateTime.Now.ToString("s") + " extension start failed: " + ex.Message + Environment.NewLine);
            }
            catch { }
        }
    }

    public ICollection GetCustomCommands()
    {
        viewCommand = new CustomCommand(CommandCategory.View, "VegasDirectorHostStatus");
        viewCommand.DisplayName = "Vegas Director Host";
        viewCommand.Invoked += HandleInvoked;
        viewCommand.MenuPopup += HandleMenuPopup;
        return new CustomCommand[] { viewCommand };
    }

    void HandleInvoked(Object sender, EventArgs args)
    {
        if (myVegas.ActivateDockView("VegasDirectorHostView"))
            return;

        DockableControl dockView = new DockableControl("VegasDirectorHostView");
        dockView.AutoLoadCommand = viewCommand;
        dockView.PersistDockWindowState = true;
        Label label = new Label();
        label.Dock = DockStyle.Fill;
        label.Padding = new Padding(12);
        label.TextAlign = ContentAlignment.TopLeft;
        label.Text = host != null ? host.StatusText() : "Vegas Director host failed to start. See %LOCALAPPDATA%\\vegas-director-mcp\\host.log";
        dockView.Controls.Add(label);
        myVegas.LoadDockView(dockView);
    }

    void HandleMenuPopup(Object sender, EventArgs args)
    {
        viewCommand.Checked = myVegas.FindDockView("VegasDirectorHostView");
    }
}
