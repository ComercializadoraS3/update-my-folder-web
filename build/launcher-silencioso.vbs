' Envoltura opcional del lanzador.
'
' UpdateMyFolder.cmd muestra una ventana de consola durante una fraccion de
' segundo al arrancar. Si eso molesta, apunta el acceso directo a este archivo
' en lugar de al .cmd: hace exactamente lo mismo pero sin ventana visible.

Dim shell, here
Set shell = CreateObject("WScript.Shell")
here = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
shell.Run """" & here & "\UpdateMyFolder.cmd""", 0, False
