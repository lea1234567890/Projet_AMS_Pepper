# Adaptateur Choregraphe pour la gestion capteurs via flux TCP.

import os
import queue
import socket
import struct
import threading
import time
from typing import Callable, Optional

from .base import AdapterConfig, LEDColor, RobotAdapter


class ChoregrapheAdapter(RobotAdapter):
    # Adaptateur qui parle directement au flux audio/vidéo exposé par un comportement Choregraphe.

    _FRAME_HEADER_FMT = "<III"  # len + ts_sec + ts_usec
    _FRAME_HEADER_SIZE = struct.calcsize(_FRAME_HEADER_FMT)
    _CONTROL_PREFIX = "TTS:"

    def __init__(
        self,
        ip: str,
        port: int = 9559,
        config: Optional[AdapterConfig] = None,
        bind_host: str = "0.0.0.0",
    ):
        super().__init__(config)
        self.ip = ip
        self.port = port
        self._bind_host = bind_host

        # Sockets écouteurs + connexion de sortie.
        self._audio_capture_socket: Optional[socket.socket] = None
        self._video_capture_socket: Optional[socket.socket] = None
        self._audio_playback_socket: Optional[socket.socket] = None
        self._audio_playback_lock = threading.Lock()
        self._audio_playback_queue: "queue.Queue[bytes]" = queue.Queue(maxsize=128)
        self._control_socket: Optional[socket.socket] = None
        self._control_lock = threading.Lock()

        # Threads.
        self._capture_thread: Optional[threading.Thread] = None
        self._video_thread: Optional[threading.Thread] = None
        self._playback_thread: Optional[threading.Thread] = None

        # Etats.
        self._is_capturing = False
        self._is_playing = False
        self._is_streaming_video = False
        self._audio_callback: Optional[Callable[[bytes], None]] = None
        self._video_callback: Optional[Callable[[bytes], None]] = None
        self._latest_video_frame: Optional[bytes] = None
        self._latest_video_ts: float = 0.0
        self._last_audio_format = self.get_audio_capture_format()

        # Métriques.
        self._audio_frames = 0
        self._video_frames = 0
        self._audio_errors = 0
        self._video_errors = 0

        # Ports sur comportement Choregraphe (si non fournis, ceux de config sont utilisés).
        self._audio_listen_port = int(os.getenv("PEPPER_CHOREGRAPHE_AUDIO_CAPTURE_PORT", "") or 0) or \
            self.config.audio_capture_port
        self._audio_send_port = int(os.getenv("PEPPER_CHOREGRAPHE_AUDIO_PLAYBACK_PORT", "") or 0) or \
            self.config.audio_playback_port
        self._video_listen_port = int(os.getenv("PEPPER_CHOREGRAPHE_VIDEO_PORT", "") or 0) or \
            self.config.video_capture_port
        self._control_port = int(os.getenv("PEPPER_CHOREGRAPHE_CONTROL_PORT", "") or 0) or 5558

        # Paramètres de format attendus (stables entre scripts Choregraphe et Python).
        # Valeurs par défaut forcées pour cette branche: mono 16kHz.
        self.config.sample_rate = int(os.getenv("PEPPER_CHOREGRAPHE_AUDIO_SAMPLE_RATE", "16000") or 16000)
        self.config.channels_in = int(os.getenv("PEPPER_CHOREGRAPHE_AUDIO_CHANNELS", "1") or 1)
        if self.config.channels_in <= 0:
            self.config.channels_in = 1
        if self.config.sample_rate <= 0:
            self.config.sample_rate = 16000
        self._qi_session = None
        self._tts_proxy = None
        self._naoqi_tts = None

    # CONNEXION

    def connect(self) -> bool:
        # Pas de poignée d'API réseau persistante côté client.
        if not self.ip:
            print("[Choregraphe] IP Pepper non configurée (PEPPER_IP manquant)")
            self._is_connected = False
            return False

        self._is_connected = True
        print(f"[Choregraphe] Connecté (mode capteurs externe): {self.ip}:{self.port}")
        print(
            f"[Choregraphe] Format attendu -> {self.config.sample_rate}Hz, "
            f"{self.config.channels_in} ch, bind={self._bind_host}:"
            f"{self._audio_listen_port}/{self._video_listen_port}"
        )
        return True

    def disconnect(self):
        # Ferme proprement les listeners + flux.
        self.stop_audio_capture()
        self.stop_audio_playback()
        self.stop_video_stream()
        self._close_control_socket()
        self._tts_proxy = None
        self._qi_session = None
        self._naoqi_tts = None
        self._is_connected = False
        print("[Choregraphe] Déconnecté")

    # AUDIO

    def start_audio_capture(self, callback: Callable[[bytes], None]) -> bool:
        if not self._is_connected:
            return False
        if self._is_capturing:
            return True

        self._audio_callback = callback
        self._is_capturing = True
        self._capture_thread = threading.Thread(
            target=self._audio_capture_loop,
            daemon=True
        )
        self._capture_thread.start()
        print(
            f"[Choregraphe] Capture audio démarrée sur {self._bind_host}:{self._audio_listen_port} "
            "(header len + ts_sec + ts_usec)"
        )
        return True

    def _audio_capture_loop(self):
        try:
            self._audio_capture_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._audio_capture_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._audio_capture_socket.bind((self._bind_host, self._audio_listen_port))
            self._audio_capture_socket.listen(1)
            self._audio_capture_socket.settimeout(1.0)

            while self._is_capturing:
                try:
                    conn, addr = self._audio_capture_socket.accept()
                except socket.timeout:
                    continue
                except Exception:
                    break

                print(f"[Choregraphe] Flux audio connecté depuis {addr}")
                with conn:
                    while self._is_capturing:
                        try:
                            frame = self._recv_typed_frame(conn)
                        except (ConnectionError, OSError):
                            break
                        if not frame:
                            break
                        payload = frame
                        self._audio_frames += 1
                        # Format actif pour la chaîne audio.
                        self._last_audio_format["sample_width"] = 2
                        self._last_audio_format["sample_rate"] = self.config.sample_rate
                        self._last_audio_format["channels"] = self.config.channels_in
                        if self._audio_callback:
                            self._audio_callback(payload)
                print("[Choregraphe] Flux audio fermé")
        except Exception as e:
            self._audio_errors += 1
            print(f"[Choregraphe] Erreur capture audio: {e}")
        finally:
            if self._audio_capture_socket:
                try:
                    self._audio_capture_socket.close()
                except Exception:
                    pass
                self._audio_capture_socket = None
            self._is_capturing = False

    def _recv_typed_frame(self, conn: socket.socket) -> Optional[bytes]:
        header = self._recv_exact(conn, self._FRAME_HEADER_SIZE)
        if not header:
            return None
        size, ts_sec, ts_usec = struct.unpack(self._FRAME_HEADER_FMT, header)
        if size <= 0:
            return None

        payload = self._recv_exact(conn, size)
        if not payload:
            return None

        if os.getenv("PEPPER_AUDIO_DEBUG", "0").strip().lower() in {"1", "true", "yes", "on"}:
            if self._audio_frames % 20 == 0:
                print(
                    "[Choregraphe] Frame audio reçue "
                    f"(size={size}B, ts={ts_sec}.{ts_usec:06d})"
                )
        return payload

    @staticmethod
    def _recv_exact(conn: socket.socket, size: int) -> Optional[bytes]:
        data = bytearray()
        while len(data) < size:
            remaining = size - len(data)
            chunk = conn.recv(remaining)
            if not chunk:
                return None
            data.extend(chunk)
        return bytes(data)

    def stop_audio_capture(self):
        self._is_capturing = False
        if self._audio_capture_socket:
            try:
                self._audio_capture_socket.close()
            except Exception:
                pass
            self._audio_capture_socket = None
        if self._capture_thread:
            self._capture_thread.join(timeout=2.0)
            self._capture_thread = None

    def play_audio(self, audio_bytes: bytes) -> bool:
        # Envoi audio brute PCM16 vers comportement Choregraphe.
        if not self._is_connected:
            return False
        if not audio_bytes:
            return True

        if not self._playback_thread:
            self._is_playing = True
            self._playback_thread = threading.Thread(
                target=self._playback_loop,
                daemon=True
            )
            self._playback_thread.start()

        try:
            self._audio_playback_queue.put_nowait(bytes(audio_bytes))
            return True
        except queue.Full:
            # Garde un flux temps réel: décaler si saturé.
            try:
                _ = self._audio_playback_queue.get_nowait()
                self._audio_playback_queue.put_nowait(bytes(audio_bytes))
                return True
            except Exception:
                return False

    def _playback_loop(self):
        try:
            while self._is_playing:
                try:
                    data = self._audio_playback_queue.get(timeout=0.1)
                except queue.Empty:
                    continue

                if not self._is_playing:
                    break
                if not data:
                    continue

                with self._audio_playback_lock:
                    self._ensure_playback_socket()

                if not self._audio_playback_socket:
                    continue

                try:
                    now = time.time()
                    now_sec = int(now)
                    now_usec = int((now - now_sec) * 1_000_000)
                    header = struct.pack(self._FRAME_HEADER_FMT, len(data), now_sec, now_usec)
                    self._audio_playback_socket.sendall(header + data)
                except Exception:
                    self._close_playback_socket()
                    self._audio_errors += 1
                    continue
        finally:
            self._close_playback_socket()

    def _ensure_playback_socket(self):
        if self._audio_playback_socket:
            return
        try:
            self._audio_playback_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._audio_playback_socket.settimeout(1.0)
            self._audio_playback_socket.connect((self.ip, self._audio_send_port))
        except Exception as e:
            self._audio_playback_socket = None
            if os.getenv("PEPPER_AUDIO_DEBUG", "0").strip().lower() in {"1", "true", "yes", "on"}:
                print(f"[Choregraphe] Connect playback échouée: {e}")

    def _close_playback_socket(self):
        if self._audio_playback_socket:
            try:
                self._audio_playback_socket.close()
            except Exception:
                pass
            self._audio_playback_socket = None

    def _ensure_control_socket(self):
        if self._control_socket:
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1.0)
        sock.connect((self.ip, self._control_port))
        self._control_socket = sock

    def _close_control_socket(self):
        if self._control_socket:
            try:
                self._control_socket.close()
            except Exception:
                pass
            self._control_socket = None

    def stop_audio_playback(self):
        self._is_playing = False
        if self._playback_thread:
            self._playback_thread.join(timeout=2.0)
            self._playback_thread = None
        self._close_playback_socket()
        with self._audio_playback_queue.mutex:
            self._audio_playback_queue.queue.clear()

    # LEDs (non gérés en mode Choregraphe)

    def set_led_color(self, color: LEDColor, fade: bool = True):
        print(f"[Choregraphe] LEDs ignorées en mode capteurs externe: {color.name}")

    def set_led_rgb(self, r: int, g: int, b: int, fade: bool = True):
        print(f"[Choregraphe] LEDs RGB ignorées en mode capteurs externe: ({r},{g},{b})")

    # CAMERA

    def capture_image(self) -> Optional[bytes]:
        # Dernière frame reçue, sans garantie de fraîcheur.
        return self._latest_video_frame

    def start_video_stream(self, callback: Callable[[bytes], None]) -> bool:
        if not self._is_connected:
            return False
        if self._is_streaming_video:
            return True

        self._video_callback = callback
        self._is_streaming_video = True
        self._video_thread = threading.Thread(
            target=self._video_capture_loop,
            daemon=True
        )
        self._video_thread.start()
        print(
            f"[Choregraphe] Flux vidéo démarré sur {self._bind_host}:{self._video_listen_port} "
            "(header len + ts_sec + ts_usec)"
        )
        return True

    def _video_capture_loop(self):
        try:
            self._video_capture_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._video_capture_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._video_capture_socket.bind((self._bind_host, self._video_listen_port))
            self._video_capture_socket.listen(1)
            self._video_capture_socket.settimeout(1.0)

            while self._is_streaming_video:
                try:
                    conn, addr = self._video_capture_socket.accept()
                except socket.timeout:
                    continue
                except Exception:
                    break

                print(f"[Choregraphe] Flux vidéo connecté depuis {addr}")
                with conn:
                    while self._is_streaming_video:
                        try:
                            frame = self._recv_typed_frame(conn)
                        except (ConnectionError, OSError):
                            break
                        if not frame:
                            break
                        self._latest_video_frame = frame
                        self._latest_video_ts = time.time()
                        self._video_frames += 1
                        if self._video_callback:
                            self._video_callback(frame)
                print("[Choregraphe] Flux vidéo fermé")
        except Exception as e:
            self._video_errors += 1
            print(f"[Choregraphe] Erreur flux vidéo: {e}")
        finally:
            if self._video_capture_socket:
                try:
                    self._video_capture_socket.close()
                except Exception:
                    pass
                self._video_capture_socket = None
            self._is_streaming_video = False

    def stop_video_stream(self):
        self._is_streaming_video = False
        if self._video_capture_socket:
            try:
                self._video_capture_socket.close()
            except Exception:
                pass
            self._video_capture_socket = None
        if self._video_thread:
            self._video_thread.join(timeout=2.0)
            self._video_thread = None

    # PAROLE

    def say(self, text: str, blocking: bool = False) -> bool:
        # Stratégie:
        # 1) ALTextToSpeech via qi (si disponible côté Mac),
        # 2) fallback socket control vers comportement Choregraphe.
        if not self._is_connected:
            return False
        payload = str(text or "").strip()
        if not payload:
            return True

        if self._say_via_qi(payload, blocking=blocking):
            return True
        if self._say_via_naoqi(payload, blocking=blocking):
            return True
        if self._say_via_control_socket(payload):
            return True
        print("[Choregraphe] TTS indisponible (qi/naoqi et canal control KO)")
        return False

    def _say_via_qi(self, text: str, blocking: bool = False) -> bool:
        try:
            if self._tts_proxy is None:
                import qi  # type: ignore

                if self._qi_session is None:
                    self._qi_session = qi.Session()
                    self._qi_session.connect(f"tcp://{self.ip}:{self.port}")
                self._tts_proxy = self._qi_session.service("ALTextToSpeech")

            if blocking:
                self._tts_proxy.say(text)
            else:
                t = threading.Thread(target=self._tts_proxy.say, args=(text,), daemon=True)
                t.start()
            return True
        except Exception:
            self._tts_proxy = None
            return False

    def _say_via_naoqi(self, text: str, blocking: bool = False) -> bool:
        try:
            if self._naoqi_tts is None:
                from naoqi import ALProxy  # type: ignore
                self._naoqi_tts = ALProxy("ALTextToSpeech", self.ip, int(self.port))

            if blocking:
                self._naoqi_tts.say(text)
            else:
                t = threading.Thread(target=self._naoqi_tts.say, args=(text,), daemon=True)
                t.start()
            return True
        except Exception:
            self._naoqi_tts = None
            return False

    def _say_via_control_socket(self, text: str) -> bool:
        # Fallback optionnel: comportement Choregraphe doit écouter le port control.
        command = f"{self._CONTROL_PREFIX}{text}".encode("utf-8")
        now = time.time()
        now_sec = int(now)
        now_usec = int((now - now_sec) * 1_000_000)
        header = struct.pack(self._FRAME_HEADER_FMT, len(command), now_sec, now_usec)
        try:
            with self._control_lock:
                self._ensure_control_socket()
                if not self._control_socket:
                    return False
                self._control_socket.sendall(header + command)
            return True
        except Exception:
            self._close_control_socket()
            return False

    def stop_speaking(self):
        # Aucun moteur TTS local à stopper ici.
        return

    # DETECTION PRESENCE (dégradé)

    def is_person_present(self) -> bool:
        return False

    def get_person_distance(self) -> Optional[float]:
        return None

    # TABLETTE (non gérées ici)

    def show_on_tablet(self, url: str) -> bool:
        return False

    def hide_tablet(self):
        return

    # MOUVEMENTS

    def wave(self):
        return

    def nod(self):
        return

    def point_at_tablet(self):
        return

    def freeze_head(self):
        return

    def unfreeze_head(self):
        return

    # UTILITAIRES

    def get_audio_capture_format(self) -> dict:
        base = super().get_audio_capture_format()
        base["sample_rate"] = int(self.config.sample_rate)
        base["channels"] = int(self.config.channels_in)
        return base

    def get_audio_active_format(self) -> dict:
        return {
            "sample_rate": int(self._last_audio_format.get("sample_rate", self.config.sample_rate)),
            "channels": int(max(1, self._last_audio_format.get("channels", self.config.channels_in))),
            "sample_width": int(self._last_audio_format.get("sample_width", 2)),
        }

    def get_status(self) -> dict:
        # Retourne l'état du lien Choregraphe.
        return {
            "type": "choregraphe",
            "ip": self.ip,
            "port": self.port,
            "is_connected": self._is_connected,
            "is_capturing_audio": self._is_capturing,
            "is_streaming_video": self._is_streaming_video,
            "audio_listener_port": self._audio_listen_port,
            "audio_sender_port": self._audio_send_port,
            "control_port": self._control_port,
            "video_listener_port": self._video_listen_port,
            "audio_frames": self._audio_frames,
            "video_frames": self._video_frames,
            "audio_errors": self._audio_errors,
            "video_errors": self._video_errors,
            "tts_qi_ready": bool(self._tts_proxy),
            "tts_naoqi_ready": bool(self._naoqi_tts),
            "audio_format": self.get_audio_active_format(),
            "video_last_ts": self._latest_video_ts,
        }
