# Interface de Base pour Adaptateurs Robot

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, Callable, Any, Tuple
from enum import Enum


class LEDColor(Enum):
    # Couleurs predefinies pour les LEDs.
    OFF = (0, 0, 0)
    WHITE = (255, 255, 255)
    RED = (255, 0, 0)
    GREEN = (0, 255, 0)
    BLUE = (0, 0, 255)
    YELLOW = (255, 255, 0)
    ORANGE = (255, 128, 0)
    PURPLE = (128, 0, 255)
    CYAN = (0, 255, 255)


@dataclass
class AdapterConfig:
    # Configuration de base pour les adaptateurs.
    name: str = "robot"
    # Audio
    audio_capture_port: int = 5555
    audio_playback_port: int = 5556
    video_capture_port: int = 5557
    sample_rate: int = 48000
    channels_in: int = 4
    channels_out: int = 2
    # LEDs
    led_fade_duration: float = 0.3
    # Timeouts
    connection_timeout: float = 10.0
    command_timeout: float = 5.0


class RobotAdapter(ABC):
    # Interface abstraite pour controle du robot.

    def __init__(self, config: Optional[AdapterConfig] = None):
        # Initialise l'objet.
        self.config = config or AdapterConfig()
        self._is_connected = False
        self._callbacks = {}

    @property
    def is_connected(self) -> bool:
        # Verifie si le robot est connecte.
        return self._is_connected

    # CONNEXION

    @abstractmethod
    def connect(self) -> bool:
        # Etablit la connexion avec le robot.
        pass

    @abstractmethod
    def disconnect(self):
        # Ferme la connexion avec le robot.
        pass

    # AUDIO

    @abstractmethod
    def start_audio_capture(self, callback: Callable[[bytes], None]) -> bool:
        # Demarre la capture audio.
        pass

    @abstractmethod
    def stop_audio_capture(self):
        # Arrete la capture audio.
        pass

    @abstractmethod
    def play_audio(self, audio_bytes: bytes) -> bool:
        # Joue de l'audio sur les haut-parleurs.
        pass

    @abstractmethod
    def stop_audio_playback(self):
        # Arrete la lecture audio.
        pass

    # LEDs

    @abstractmethod
    def set_led_color(self, color: LEDColor, fade: bool = True):
        # Change la couleur des LEDs des yeux.
        pass

    @abstractmethod
    def set_led_rgb(self, r: int, g: int, b: int, fade: bool = True):
        # Change la couleur des LEDs avec valeurs RGB.
        pass

    # CAMERA

    @abstractmethod
    def capture_image(self) -> Optional[bytes]:
        # Capture une image depuis la camera.
        pass

    @abstractmethod
    def start_video_stream(self, callback: Callable[[bytes], None]) -> bool:
        # Demarre le flux video.
        pass

    @abstractmethod
    def stop_video_stream(self):
        # Arrete le flux video.
        pass

    # PAROLE

    @abstractmethod
    def say(self, text: str, blocking: bool = False) -> bool:
        # Fait parler le robot avec TTS integre.
        pass

    @abstractmethod
    def stop_speaking(self):
        # Interrompt la parole en cours.
        pass

    # DETECTION PRESENCE

    @abstractmethod
    def is_person_present(self) -> bool:
        # Detecte si une personne est devant le robot.
        pass

    @abstractmethod
    def get_person_distance(self) -> Optional[float]:
        # Estime la distance de la personne.
        pass

    # TABLETTE

    @abstractmethod
    def show_on_tablet(self, url: str) -> bool:
        # Affiche une URL sur la tablette.
        pass

    @abstractmethod
    def hide_tablet(self):
        # Cache le contenu de la tablette.
        pass

    # MOUVEMENTS (optionnel)

    def wave(self):
        # Fait un geste de salut.
        pass

    def nod(self):
        # Fait un hochement de tete.
        pass

    def point_at_tablet(self):
        # Pointe vers la tablette.
        pass

    def freeze_head(self):
        # Fige la tete (utile pendant un scan caméra).
        pass

    def unfreeze_head(self):
        # Restaure le comportement normal de la tete.
        pass

    # UTILITAIRES

    def register_callback(self, event: str, callback: Callable):
        # Enregistre un callback pour un evenement.
        if event not in self._callbacks:
            self._callbacks[event] = []
        self._callbacks[event].append(callback)

    def _emit(self, event: str, *args, **kwargs):
        # Emet un evenement vers les callbacks.
        for callback in self._callbacks.get(event, []):
            try:
                callback(*args, **kwargs)
            except Exception as e:
                print(f"[Adapter] Erreur callback {event}: {e}")

    @abstractmethod
    def get_status(self) -> dict:
        # Retourne l'etat du robot.
        pass

    def get_audio_capture_format(self) -> dict:
        # Format audio déclaré par l'adaptateur.
        return {
            "sample_rate": int(self.config.sample_rate),
            "channels": int(self.config.channels_in),
            "sample_width": 2,
        }

    def get_audio_active_format(self) -> dict:
        # Format audio observé en runtime (fallback: format déclaré).
        return self.get_audio_capture_format()
