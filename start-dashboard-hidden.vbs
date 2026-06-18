' Launch the quota dashboard fully hidden (no console window).
' Used by the "CLIProxyAPI-Dashboard" scheduled task. Resolves its own folder,
' so it works wherever this repo is placed.
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir
sh.Run """" & dir & "\run-dashboard-hidden.cmd""", 0, False
