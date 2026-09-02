# Update My Folder

Sincronizador incremental de carpetas para Windows. Copia solo lo que cambió
entre un origen y un destino, con reglas de inclusión y omisión configurables,
y se actualiza solo desde GitHub Releases.

---

## Para el usuario

### Instalación

1. Descarga `UpdateMyFolder-<versión>-instalador.zip` del release.
2. Descomprímelo donde quieras, por ejemplo `C:\Apps\UpdateMyFolder`.
3. Crea un acceso directo a **`UpdateMyFolder.cmd`** y ábrelo siempre desde ahí.

> No abras el `.exe` de `versions/` directamente. El lanzador es lo que permite
> que la aplicación se actualice sola sin romper tu acceso directo.
>
> Si molesta el parpadeo de consola al abrir, apunta el acceso directo a
> `UpdateMyFolder-silencioso.vbs`: hace lo mismo sin ventana.

No hace falta tener Python instalado.

### Uso

1. **Configuración** → escribe la ruta de origen y la de destino. Quedan
   guardadas; no hay que volver a ponerlas nunca.
2. **Analizar** → recorre ambos árboles y muestra qué cambió.
3. Revisa la lista y destilda lo que no quieras (clic en la primera columna,
   o barra espaciadora sobre las filas seleccionadas).
4. **Copiar seleccionados**.

Nada se escribe en el destino hasta que confirmas.

### Opciones

| Opción | Qué hace |
|---|---|
| Copiar todo | Ignora la comparación y copia todo lo que pasen las reglas. |
| Verificar contenido | Cuando el tamaño coincide pero la fecha no, compara los bytes antes de copiar. Evita copias inútiles. |
| Simulación | Muestra qué se haría sin tocar el destino. |
| Eliminar sobrantes | Modo espejo: borra del destino lo que ya no existe en el origen. Pide confirmación aparte. |

### Perfiles

Cada perfil guarda sus propias rutas, reglas y opciones. Sirven para tener
"Producción", "Pruebas", etc. en la misma instalación, cambiando con el
desplegable de arriba.

### Reglas de inclusión y omisión

Un patrón por línea, en **Configuración → Reglas**:

| Patrón | Significado |
|---|---|
| `*.cs` | todos los archivos con esa extensión, a cualquier profundidad |
| `ejemplo.cs` | solo los archivos con ese nombre exacto |
| `/reorgs/` | la carpeta `reorgs` **en la raíz** del origen |
| `reorgs/` | cualquier carpeta llamada `reorgs`, a cualquier profundidad |
| `/src/app/*.cs` | ruta anclada a la raíz, con comodín |
| `**/obj/` | igual que `obj/` |

- La barra final indica **carpeta**; la barra inicial **ancla a la raíz**.
- Lista de inclusión vacía → entra todo.
- **La omisión siempre gana** sobre la inclusión.
- Insensible a mayúsculas. Las líneas que empiezan con `#` son comentarios.

El botón **Probar reglas contra el origen** recorre el árbol real y muestra qué
entra y qué queda fuera. Conviene usarlo: depurar patrones a ciegas es la forma
más rápida de copiar de menos sin darse cuenta.

### Dónde queda todo

```
%APPDATA%\UpdateMyFolder\config.json     perfiles y ajustes
%APPDATA%\UpdateMyFolder\logs\           registro de cada copia
```

---

## Para quien publica versiones

### Configurar el canal

En **Configuración → Avanzado → URL de actualización**, la URL del repositorio:

```
https://github.com/tu-organizacion/update-my-folder
```

Se acepta también una URL de la API de GitHub o un `manifest.json` propio con
los campos `version`, `url`, `sha256` y `notes`.

### Publicar automaticamente (lo normal)

Cada push a `main` dispara `.github/workflows/release.yml`, que sube la
version, compila, y crea el release con los zip. No hay nada que hacer a mano:
el numero de version sale del mensaje del commit.

| Mensaje del commit                      | 1.2.3 pasa a |
| --------------------------------------- | ------------ |
| `fix: ...` (o cualquier otro)            | 1.2.4        |
| `feat: ...`                              | 1.3.0        |
| `feat!: ...` o `BREAKING CHANGE` en el cuerpo | 2.0.0   |

El propio flujo devuelve a `main` un commit `chore: version X.Y.Z [skip ci]`
con `app/version.py` ya actualizado, asi que el repositorio y el release nunca
se desincronizan. Para empujar sin publicar, escribe `[skip release]` en el
mensaje del commit. Para forzar una version concreta, lanza el flujo a mano
desde **Actions → release → Run workflow** e indica `X.Y.Z`, `major`, `minor`
o `patch`.

### Publicar a mano

```bash
python build/publish.py --bump 1.1.0 --release --notes "Corrige rutas UNC"
```

`--bump` acepta tambien `major`, `minor`, `patch` o `auto` (deduce el nivel de
`$COMMIT_MESSAGE` o de `--commit-message`).

Eso hace, en orden:

1. escribe la versión nueva en `app/version.py`;
2. compila con PyInstaller en modo carpeta;
3. arma `UpdateMyFolder-1.1.0.zip` (actualización) y
   `UpdateMyFolder-1.1.0-instalador.zip` (instalación nueva);
4. calcula el SHA-256 y escribe `manifest.json`;
5. crea el release en GitHub y sube los archivos (necesita la CLI `gh`).

Sin `--release` deja todo en `dist/` y te dice qué subir a mano.

Los usuarios ya instalados solo necesitan el primer zip. La próxima vez que
abran la aplicación verán el aviso de versión disponible.

### Cómo funciona la actualización

```
<raíz>/UpdateMyFolder.cmd        lanzador estable — a esto apunta el acceso directo
<raíz>/versions/current.txt      texto plano con la versión activa
<raíz>/versions/1.0.0/...        una carpeta por versión instalada
<raíz>/versions/1.1.0/...
```

Windows bloquea el ejecutable en uso, así que sobrescribirlo en caliente no es
posible. En vez de pelear con eso, cada versión vive en su propia carpeta y un
puntero de texto indica cuál está activa. Actualizar es descargar, **verificar
el SHA-256**, extraer a una carpeta nueva y mover el puntero.

Consecuencias prácticas:

- **Revertir** = editar `current.txt` con la versión anterior, que sigue en disco.
- Un paquete corrupto o alterado se rechaza antes de instalarse.
- Un corte a mitad de la descarga no deja la instalación en un estado inválido.

Se conservan las 3 versiones más recientes; la que está corriendo nunca se borra.

---

## Para desarrollar

```bash
pip install -r requirements-dev.txt
python main.py                              # ejecutar
python main.py --debug                      # ejecutar con registro detallado
python -m unittest discover -s tests        # pruebas
```

### Depurar

**En VS Code**: `F5` y elige una configuración de [.vscode/launch.json](.vscode/launch.json):

| Configuración | Para qué |
|---|---|
| App (modo depuración) | Ejecuta con `--debug`. Los puntos de interrupción funcionan también dentro de los hilos trabajadores. |
| App (normal) | Como lo ve el usuario. |
| Pruebas: todas | Las 63, con detalle. |
| Pruebas: archivo abierto | Solo el archivo que tengas al frente. |
| Empaquetar | Depurar el propio script de publicación. |

Las configuraciones usan `justMyCode: false`, así que puedes entrar también en
CustomTkinter cuando el problema esté en el widget y no en nuestro código. El
explorador de pruebas de VS Code descubre las pruebas solo, por
[.vscode/settings.json](.vscode/settings.json).

**Qué cambia con `--debug`** (o con la variable de entorno `UMF_DEBUG=1`):

- el registro sale por consola en tiempo real, además de al archivo;
- se registra el nivel `DEBUG`: tiempos de recorrido, conteos de la
  comparación, pasos del actualizador;
- los cuadros de error muestran la **traza completa** en vez de solo el mensaje.

**Registros**:

```
%APPDATA%\UpdateMyFolder\logs\app.log      diagnóstico, rotativo (1 MB × 3)
%APPDATA%\UpdateMyFolder\logs\sync-*.log   resumen de cada copia
```

`app.log` se escribe **siempre**, incluso en la versión empaquetada sin
consola. Cada línea lleva el hilo que la emitió (`[umf-worker]`, `[copy_3]`),
que es lo que necesitas cuando algo falla solo con concurrencia. Los fallos
dentro de un hilo trabajador quedan ahí con traza completa; cuando un usuario
reporte un problema, pídele ese archivo.

**Depurar la versión empaquetada.** Si un fallo solo se reproduce compilado,
compila una variante con consola: sin ella `stderr` no existe y el error se
pierde antes de llegar a ningún lado.

```bash
set UMF_CONSOLE=1
python -m PyInstaller build/UpdateMyFolder.spec --noconfirm
dist\UpdateMyFolder\UpdateMyFolder.exe --debug
```

Recuerda limpiar `UMF_CONSOLE` antes de publicar: la compilación de entrega no
debe llevar consola.

**Depurar el lanzador** (`.cmd`): quítale el `@echo off` de la primera línea y
ejecútalo desde una terminal para ver cada paso y qué versión termina eligiendo.

### Estructura

```
main.py                 punto de entrada (absoluto: lo necesita PyInstaller)
app/config.py           perfiles persistentes, escritura atómica
app/logging_setup.py    registro rotativo + captura de fallos en hilos
app/rules.py            motor de reglas: glob -> regex, compilado una vez
app/scanner.py          recorrido paralelo con os.scandir
app/comparer.py         clasificación NUEVO / MODIFICADO / IGUAL / SOBRANTE
app/copier.py           copia multihilo con temporal + os.replace
app/updater.py          consulta, descarga, verificación e instalación
app/ui/                 CustomTkinter; no lo importa ninguna capa del motor
build/                  spec de PyInstaller, lanzador y script de publicación
tests/                  54 pruebas, sin dependencias externas
```

El motor no conoce la interfaz. Se comunica por una `queue.Queue` que la
ventana drena cada 100 ms con `after`, y los widgets solo se tocan desde el
hilo principal. Por eso la ventana no se congela durante una copia larga.

### Decisiones de rendimiento

1. **`os.scandir`, no `os.walk` + `stat`.** En Windows el `stat` de un
   `DirEntry` viene precargado desde `FindFirstFile`: tamaño y fecha salen sin
   una sola llamada al sistema extra.
2. **Número de hilos adaptativo, medido.** En este equipo, sobre disco local:

   | Fase | 1 hilo | 4 | 8 | 16 | 32 |
   |---|---|---|---|---|---|
   | Recorrido (6.878 archivos) | 45 ms | 43 ms | 48 ms | 50 ms | 60 ms |
   | Copia (1.208 archivos, 43 MB) | 1258 ms | 581 ms | 531 ms | 555 ms | 533 ms |

   El recorrido local **no gana nada** con hilos: `os.scandir` ya es tan rápido
   que solo se paga el coste de coordinarlos. La copia sí escala, unas 2.4x, y
   se aplana hacia los 8 hilos. En rutas de red la historia cambia porque el
   coste dominante es la latencia por operación, y ahí ambas fases ganan mucho.

   Por eso el valor por defecto **depende de si algún extremo es de red**
   (UNC o unidad mapeada) en vez de ser un número fijo: pocos hilos en local,
   muchos en red. Se puede forzar un valor propio en Configuración.
3. **Origen y destino se recorren a la vez.** Con un destino en red, el tiempo
   total pasa a ser el del lado más lento en vez de la suma de los dos.
4. **Poda temprana.** Una carpeta excluida nunca se lista.
5. **Comparación byte a byte con salida anticipada** en lugar de hash: termina
   en el primer bloque distinto, sin leer los archivos completos.
6. **Escritura atómica.** Temporal + `os.replace`; el destino nunca queda con
   un archivo a medio escribir.
7. **Reintentos con espera creciente** ante fallos transitorios de SMB, y
   `copystat` para conservar la fecha (de ella depende la siguiente pasada).
