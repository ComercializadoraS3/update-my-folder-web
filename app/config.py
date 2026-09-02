"""Configuracion persistente en %APPDATA%/UpdateMyFolder/config.json.

El archivo guarda una lista de perfiles con nombre, de modo que el mismo
ejecutable sirva para varios pares origen/destino sin reconfigurar nada.
La escritura es atomica (temporal + os.replace) para que un cierre abrupto
no deje la configuracion corrupta.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path

from .version import APP_NAME


def data_dir() -> Path:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    path = Path(base) / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_path() -> Path:
    return data_dir() / "config.json"


def log_dir() -> Path:
    path = data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_workers() -> int:
    """Mas hilos que nucleos: el trabajo es de E/S y pasa el tiempo esperando
    al disco o a la red, no calculando."""
    return min(32, (os.cpu_count() or 4) * 4)


def is_network_path(path: str) -> bool:
    """True para rutas UNC o unidades de red mapeadas."""
    if not path:
        return False
    try:
        full = os.path.abspath(path)
    except (OSError, ValueError):
        return False
    if full.startswith(os.sep * 2):                 # \\servidor\recurso
        return True
    drive = os.path.splitdrive(full)[0]
    if not drive or os.name != "nt":
        return False
    try:
        import ctypes
        DRIVE_REMOTE = 4
        return ctypes.windll.kernel32.GetDriveTypeW(drive + os.sep) == DRIVE_REMOTE
    except Exception:                               # noqa: BLE001
        return False


def auto_workers(kind: str, *paths: str) -> int:
    """Hilos recomendados segun donde vivan las rutas.

    Medido en este proyecto sobre disco local: el recorrido no gana nada con
    hilos (os.scandir ya es tan rapido que solo se paga el coste de coordinar
    los hilos), mientras que la copia si escala hasta unas 2.4x. En red manda
    la latencia por operacion, y ahi ambos casos ganan mucho con mas hilos.

    Por eso el valor por defecto depende de si algun extremo es de red, en vez
    de ser un numero fijo. El usuario puede forzar el suyo en Configuracion.
    """
    remote = any(is_network_path(p) for p in paths if p)
    cpu = os.cpu_count() or 4
    if kind == "scan":
        return 24 if remote else min(4, cpu)
    return 32 if remote else min(16, cpu * 2)       # copia


# Canal de actualizacion por defecto. Se deja escrito en el codigo para que una
# instalacion nueva encuentre las versiones sin que nadie configure nada; el
# usuario puede apuntar a otro repositorio o a un manifest.json propio desde
# Configuracion -> Avanzado, y vaciarlo desactiva la busqueda.
DEFAULT_UPDATE_URL = "https://github.com/ComercializadoraS3/update-my-folder-web"


# Omisiones con las que nace todo perfil nuevo: codigo fuente, temporales y
# archivos de estado que cada instalacion genera por su cuenta. Copiarlos de un
# entorno a otro no aporta nada y en algunos casos (web.config, log.config)
# pisa la configuracion propia del destino.
DEFAULT_EXCLUDE = [
    "*.cs",
    "*.tmp",
    "*.rpt",
    "*.pdb",
    "*.rsp",
    "web.config",
    "lastreorg.dat",
    "client.log",
    "lastcalltree.info",
    "log.config",
    "/reorgs/",
    "/PublicTempStorage/",
    "/PrivateTempStorage/",
]


@dataclass
class Profile:
    name: str = "Predeterminado"
    source: str = ""
    dest: str = ""
    include: list[str] = field(default_factory=list)
    # Una copia por perfil: la lista es editable y no debe compartirse entre
    # perfiles ni mutar la constante.
    exclude: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE))
    copy_all: bool = False
    verify_content: bool = False
    dry_run: bool = False
    mirror_delete: bool = False
    threads: int = 0            # 0 = automatico
    mtime_tolerance: float = 2.0

    def workers_for(self, kind: str) -> int:
        """Hilos a usar para 'scan' o 'copy'. `threads` > 0 fuerza el valor."""
        if self.threads > 0:
            return self.threads
        return auto_workers(kind, self.source, self.dest)

    @property
    def worker_count(self) -> int:
        return self.workers_for("copy")

    @classmethod
    def from_dict(cls, raw: dict) -> "Profile":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in known})


@dataclass
class AppConfig:
    profiles: list[Profile] = field(default_factory=lambda: [Profile()])
    active_profile: str = "Predeterminado"
    update_url: str = DEFAULT_UPDATE_URL
    auto_check_updates: bool = True
    appearance: str = "system"      # system | light | dark
    max_rows_display: int = 5000
    window_geometry: str = ""

    # ---------------------------------------------------------------- perfiles
    def active(self) -> Profile:
        for p in self.profiles:
            if p.name == self.active_profile:
                return p
        if not self.profiles:
            self.profiles.append(Profile())
        self.active_profile = self.profiles[0].name
        return self.profiles[0]

    def profile_names(self) -> list[str]:
        return [p.name for p in self.profiles]

    def unique_name(self, base: str) -> str:
        existing = set(self.profile_names())
        if base not in existing:
            return base
        n = 2
        while f"{base} ({n})" in existing:
            n += 1
        return f"{base} ({n})"

    # ------------------------------------------------------------ persistencia
    @classmethod
    def load(cls, path: Path | None = None) -> "AppConfig":
        path = path or config_path()
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # Configuracion ilegible: se conserva a un lado y se arranca limpio
            # en vez de impedir que la aplicacion abra.
            try:
                path.replace(path.with_suffix(".json.bad"))
            except OSError:
                pass
            return cls()
        cfg = cls()
        cfg.profiles = [Profile.from_dict(p) for p in raw.get("profiles", [])] or [Profile()]
        cfg.active_profile = raw.get("active_profile", cfg.profiles[0].name)
        cfg.update_url = raw.get("update_url", DEFAULT_UPDATE_URL)
        cfg.auto_check_updates = bool(raw.get("auto_check_updates", True))
        cfg.appearance = raw.get("appearance", "system")
        cfg.max_rows_display = int(raw.get("max_rows_display", 5000))
        cfg.window_geometry = raw.get("window_geometry", "")
        return cfg

    def save(self, path: Path | None = None) -> None:
        path = path or config_path()
        payload = {
            "version": 1,
            "profiles": [asdict(p) for p in self.profiles],
            "active_profile": self.active_profile,
            "update_url": self.update_url,
            "auto_check_updates": self.auto_check_updates,
            "appearance": self.appearance,
            "max_rows_display": self.max_rows_display,
            "window_geometry": self.window_geometry,
        }
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)


def install_root() -> Path:
    """Carpeta raiz de la instalacion (la que contiene 'versions').

    Empaquetado:  <raiz>/versions/<version>/UpdateMyFolder.exe
    Desarrollo:   la carpeta del repositorio.
    """
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        if exe_dir.parent.name.lower() == "versions":
            return exe_dir.parent.parent
        return exe_dir
    return Path(__file__).resolve().parent.parent


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))
