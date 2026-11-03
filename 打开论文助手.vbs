Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = root
shell.Run Chr(34) & root & "\runtime\pythonw.exe" & Chr(34) & " -B " & Chr(34) & root & "\app.py" & Chr(34), 0, False
