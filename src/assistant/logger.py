# Systeme de Logging

import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field
from enum import Enum
import time
import traceback


class LogEvent(Enum):
    # Types d'evenements loggables.
    # Systeme
    SYSTEM_START = "system_start"
    SYSTEM_STOP = "system_stop"
    CONFIG_LOADED = "config_loaded"

    # Session
    SESSION_START = "session_start"
    SESSION_END = "session_end"

    # Etats
    STATE_CHANGE = "state_change"

    # Produits
    PRODUCT_IDENTIFIED = "product_identified"
    PRODUCT_SEARCH = "product_search"

    # Securite
    SECURITY_ALERT = "security_alert"
    BLACKLIST_MATCH = "blacklist_match"

    # Performance
    LATENCY_MEASURE = "latency_measure"

    # Tablette
    TABLET_CONNECTED = "tablet_connected"
    TABLET_DISCONNECTED = "tablet_disconnected"
    TABLET_ACTION = "tablet_action"

    # Audio
    SPEECH_DETECTED = "speech_detected"
    SPEECH_ENDED = "speech_ended"
    AUDIO_SENT = "audio_sent"
    AUDIO_RECEIVED = "audio_received"

    # Vision
    CAMERA_FRAME = "camera_frame"
    VLM_INFERENCE = "vlm_inference"
    BARCODE_DETECTED = "barcode_detected"

    # Erreurs
    ERROR = "error"
    WARNING = "warning"


@dataclass
class LatencyTimer:
    # Mesure de latence pour operations critiques.
    name: str
    start_time: float = field(default_factory=time.time)
    end_time: Optional[float] = None

    def stop(self) -> float:
        # Arrete le timer et retourne la duree en ms.
        self.end_time = time.time()
        return (self.end_time - self.start_time) * 1000

    @property
    def duration_ms(self) -> float:
        # Gere ms.
        if self.end_time:
            return (self.end_time - self.start_time) * 1000
        return (time.time() - self.start_time) * 1000


class SystemLogger:
    # Gestionnaire de logs structure JSONL.

    def __init__(
        # Initialise l'objet.
        self,
        log_directory: str = "logs",
        file_prefix: str = "assistant",
        console_level: str = "INFO",
        file_level: str = "DEBUG"
    ):
        self.log_directory = Path(log_directory)
        self.file_prefix = file_prefix
        self.console_level = getattr(logging, console_level.upper(), logging.INFO)
        self.file_level = getattr(logging, file_level.upper(), logging.DEBUG)

        # Creer repertoire
        self.log_directory.mkdir(parents=True, exist_ok=True)

        # Fichier du jour
        today = datetime.now().strftime("%Y-%m-%d")
        self.log_file = self.log_directory / f"{file_prefix}_{today}.jsonl"

        # Session
        self.session_id: Optional[str] = None
        self._session_start: Optional[float] = None

        # Statistiques
        self._stats = {
            "events_logged": 0,
            "errors": 0,
            "warnings": 0,
            "state_changes": 0,
            "products_identified": 0,
            "security_alerts": 0
        }

        # Logger Python standard pour console
        self._setup_console_logger()

        # File handle
        self._file_handle = None

    def _setup_console_logger(self):
        # Configure le logger console.
        self._console = logging.getLogger(f"assistant.{self.file_prefix}")
        self._console.setLevel(logging.DEBUG)

        # Handler console
        if not self._console.handlers:
            handler = logging.StreamHandler(sys.stdout)
            handler.setLevel(self.console_level)
            formatter = logging.Formatter(
                '%(asctime)s | %(levelname)-8s | %(message)s',
                datefmt='%H:%M:%S'
            )
            handler.setFormatter(formatter)
            self._console.addHandler(handler)

    def _write_jsonl(self, level: str, event: str, data: Dict[str, Any]):
        # Ecrit une ligne JSONL dans le fichier.
        record = {
            "timestamp": datetime.now().isoformat(),
            "level": level,
            "event": event,
            "data": data
        }

        if self.session_id:
            record["session_id"] = self.session_id

        try:
            with open(self.log_file, 'a', encoding='utf-8') as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            self._console.error(f"Erreur ecriture log: {e}")

    def log_event(self, event: LogEvent, data: Optional[Dict[str, Any]] = None):
        # Logue un evenement structure.
        data = data or {}
        self._write_jsonl("INFO", event.value, data)
        self._stats["events_logged"] += 1

        # Compter par type
        if event == LogEvent.STATE_CHANGE:
            self._stats["state_changes"] += 1
        elif event == LogEvent.PRODUCT_IDENTIFIED:
            self._stats["products_identified"] += 1
        elif event == LogEvent.SECURITY_ALERT:
            self._stats["security_alerts"] += 1

    def log_state_change(self, from_state: str, to_state: str, trigger: str = ""):
        # Logue un changement d'etat.
        self.log_event(LogEvent.STATE_CHANGE, {
            "from": from_state,
            "to": to_state,
            "trigger": trigger
        })
        self._console.info(f"Etat: {from_state} -> {to_state} ({trigger})")

    def log_latency(self, name: str, duration_ms: float, threshold_ms: float = 0):
        # Logue une mesure de latence.
        is_slow = duration_ms > threshold_ms if threshold_ms > 0 else False
        self.log_event(LogEvent.LATENCY_MEASURE, {
            "name": name,
            "duration_ms": round(duration_ms, 2),
            "is_slow": is_slow,
            "threshold_ms": threshold_ms
        })
        if is_slow:
            self._console.warning(f"Latence elevee: {name} = {duration_ms:.1f}ms (seuil: {threshold_ms}ms)")

    def log_debug(self, message: str, **kwargs):
        # Log niveau DEBUG.
        self._console.debug(message)
        if self.file_level <= logging.DEBUG:
            self._write_jsonl("DEBUG", "debug", {"message": message, **kwargs})

    def log_info(self, message: str, **kwargs):
        # Log niveau INFO.
        self._console.info(message)
        self._write_jsonl("INFO", "info", {"message": message, **kwargs})

    def log_warning(self, message: str, **kwargs):
        # Log niveau WARNING.
        self._console.warning(message)
        self._write_jsonl("WARNING", "warning", {"message": message, **kwargs})
        self._stats["warnings"] += 1

    def log_error(self, message: str, exception: Optional[Exception] = None, **kwargs):
        # Log niveau ERROR.
        error_data = {"message": message, **kwargs}
        if exception:
            error_data["exception"] = str(exception)
            error_data["traceback"] = traceback.format_exc()

        self._console.error(message)
        if exception:
            self._console.exception(exception)

        self._write_jsonl("ERROR", "error", error_data)
        self._stats["errors"] += 1

    def log_critical(self, message: str, exception: Optional[Exception] = None, **kwargs):
        # Log niveau CRITICAL.
        error_data = {"message": message, **kwargs}
        if exception:
            error_data["exception"] = str(exception)
            error_data["traceback"] = traceback.format_exc()

        self._console.critical(message)
        self._write_jsonl("CRITICAL", "critical", error_data)
        self._stats["errors"] += 1

    def start_session(self) -> str:
        # Demarre une nouvelle session et retourne l'ID.
        self.session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._session_start = time.time()
        self.log_event(LogEvent.SESSION_START, {"session_id": self.session_id})
        return self.session_id

    def end_session(self, stats: Optional[Dict[str, Any]] = None):
        # Termine la session.
        duration_sec = time.time() - self._session_start if self._session_start else 0
        self.log_event(LogEvent.SESSION_END, {
            "session_id": self.session_id,
            "duration_sec": round(duration_sec, 1),
            "stats": stats or self._stats
        })
        self.session_id = None
        self._session_start = None

    def get_stats(self) -> Dict[str, Any]:
        # Retourne les statistiques de la session.
        return self._stats.copy()

    def close(self):
        # Ferme le logger.
        if self._file_handle:
            self._file_handle.close()

    def __enter__(self):
        # Gere l'action.
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        # Gere l'action.
        self.close()



_logger_instance: Optional[SystemLogger] = None


def get_logger(
    # Recupere logger.
    log_directory: str = "logs",
    file_prefix: str = "assistant",
    console_level: str = "INFO",
    file_level: str = "DEBUG"
) -> SystemLogger:
    """
    Recupere l'instance du logger (singleton).

    Args:
        log_directory: Repertoire des logs
        file_prefix: Prefixe des fichiers log
        console_level: Niveau minimum console
        file_level: Niveau minimum fichier

    Returns:
        Instance du logger
    """
    global _logger_instance

    if _logger_instance is None:
        _logger_instance = SystemLogger(
            log_directory=log_directory,
            file_prefix=file_prefix,
            console_level=console_level,
            file_level=file_level
        )

    return _logger_instance


def reset_logger():
    # Reinitialise le logger (pour les tests).
    global _logger_instance
    if _logger_instance:
        _logger_instance.close()
    _logger_instance = None
