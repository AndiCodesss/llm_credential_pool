Option Explicit

Dim shell, fso, home, wrapper, log, cmd, rc
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
home = shell.ExpandEnvironmentStrings("%USERPROFILE%") & "\.cli-proxy-api"
wrapper = home & "\run-broker.cmd"
log = home & "\launcher.log"

If Not fso.FileExists(wrapper) Then
  AppendLog log, "Missing wrapper: " & wrapper
  WScript.Quit 1
End If

If IsPortListening(8317) Then
  AppendLog log, "Broker already listening on 127.0.0.1:8317; not launching duplicate."
  WScript.Quit 0
End If

cmd = "cmd.exe /c """ & wrapper & """"
AppendLog log, "Launching hidden broker wrapper: " & cmd
rc = shell.Run(cmd, 0, False)
WScript.Quit 0

Function IsPortListening(port)
  Dim exec, line, needle
  needle = ":" & CStr(port)
  IsPortListening = False

  On Error Resume Next
  Set exec = shell.Exec("cmd.exe /c netstat -ano")
  If Err.Number <> 0 Then
    AppendLog log, "Port check failed to start netstat: " & Err.Description
    Err.Clear
    On Error GoTo 0
    Exit Function
  End If

  Do While Not exec.StdOut.AtEndOfStream
    line = exec.StdOut.ReadLine()
    If InStr(line, needle) > 0 And InStr(UCase(line), "LISTENING") > 0 Then
      IsPortListening = True
      Exit Do
    End If
  Loop

  On Error GoTo 0
End Function

Sub AppendLog(path, message)
  Dim ts
  ts = Year(Now) & "-" & Right("0" & Month(Now),2) & "-" & Right("0" & Day(Now),2) & " " & Right("0" & Hour(Now),2) & ":" & Right("0" & Minute(Now),2) & ":" & Right("0" & Second(Now),2)
  Dim file
  Set file = fso.OpenTextFile(path, 8, True)
  file.WriteLine "[" & ts & "] " & message
  file.Close
End Sub
