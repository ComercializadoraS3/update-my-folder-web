@echo off
rem Lanzador estable de Update My Folder.
rem
rem El acceso directo del usuario apunta SIEMPRE a este archivo. El lanzador
rem lee versions\current.txt y arranca la version que indique. Asi el
rem actualizador puede instalar una version nueva junto a la anterior sin
rem tocar ningun archivo bloqueado, y revertir es editar una linea de texto.

setlocal EnableDelayedExpansion
set "ROOT=%~dp0"
set "VERS=%ROOT%versions"
set "PICK="

if exist "%VERS%\current.txt" set /p PICK=<"%VERS%\current.txt"

rem El puntero se puede editar a mano para revertir, y el Bloc de notas suele
rem guardar UTF-8 con BOM. Esos bytes invisibles llegan pegados al numero de
rem version y harian que no coincida con ninguna carpeta, arrancando la
rem version equivocada en silencio. Se conservan solo digitos y puntos, que
rem tambien limpia espacios, comillas y retornos de carro.
if defined PICK (
    set "CLEAN="
    call :sanitize
    set "PICK=!CLEAN!"
)

if defined PICK if not exist "%VERS%\!PICK!\UpdateMyFolder.exe" set "PICK="

rem Respaldo: si el puntero falta o apunta a algo que ya no existe, se toma
rem cualquier version instalada en vez de dejar al usuario sin aplicacion.
if not defined PICK (
    for /f "delims=" %%d in ('dir /b /ad "%VERS%" 2^>nul') do (
        if exist "%VERS%\%%d\UpdateMyFolder.exe" set "PICK=%%d"
    )
)

if not defined PICK (
    echo No se encontro ninguna version instalada en "%VERS%".
    echo Reinstala la aplicacion desde el paquete de instalacion.
    pause
    exit /b 1
)

start "" "%VERS%\!PICK!\UpdateMyFolder.exe" %*
exit /b 0

:sanitize
if not defined PICK goto :eof
set "CHAR=!PICK:~0,1!"
set "PICK=!PICK:~1!"
echo !CHAR!| findstr /r "^[0-9.]$" >nul 2>&1 && set "CLEAN=!CLEAN!!CHAR!"
goto :sanitize
