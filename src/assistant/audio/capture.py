#!/usr/bin/env python3
# Phase 1 - Module de Capture Audio Pepper

import argparse
import sys
import time
import socket
import struct
import threading
import signal
from typing import Optional, Callable
from datetime import datetime

# Configuration audio Pepper
SAMPLE_RATE = 48000      # Hz
CHANNELS = 4             # 4 microphones
SAMPLE_WIDTH = 2         # 16-bit = 2 bytes
BUFFER_SIZE = 1024       # Samples par canal par buffer (~21ms à 48kHz)

# Protocole TCP
MAGIC_HEADER = b'PAUC'   # Pepper AUdio Capture
VERSION = 1


class AudioCaptureModule:
    # Module NAOqi pour capturer l'audio depuis ALAudioDevice.

    def __init__(self, name: str = "AudioCaptureModule"):
        # Initialise l'objet.
        self.name = name
        self.is_running = False
        self.audio_buffer = []
        self.frames_captured = 0
        self.on_audio_callback: Optional[Callable] = None

        # Stats
        self.start_time = 0
        self.total_bytes = 0

    def processRemote(self, nbOfChannels: int, nbrOfSamplesByChannel: int,
                      # Gere l'action.
                      aTimeStamp: list, buffer: bytes):
        """
        Callback appelé par ALAudioDevice à chaque nouveau buffer audio.

        Args:
            nbOfChannels: Nombre de canaux (4)
            nbrOfSamplesByChannel: Nombre d'échantillons par canal
            aTimeStamp: [seconds, microseconds] timestamp du buffer
            buffer: Données audio brutes (interleaved)
        """
        if not self.is_running:
            return

        self.frames_captured += 1

        # Convertir en bytes si nécessaire
        if isinstance(buffer, (list, tuple)):
            audio_data = bytes(buffer)
        else:
            audio_data = bytes(buffer)

        self.total_bytes += len(audio_data)

        # Appeler le callback externe si défini
        if self.on_audio_callback:
            self.on_audio_callback(
                audio_data,
                nbOfChannels,
                nbrOfSamplesByChannel,
                aTimeStamp
            )

    def start(self):
        # Démarre la capture.
        self.is_running = True
        self.start_time = time.time()
        self.frames_captured = 0
        self.total_bytes = 0

    def stop(self):
        # Arrête la capture.
        self.is_running = False

    def get_stats(self) -> dict:
        # Retourne les statistiques de capture.
        elapsed = time.time() - self.start_time if self.start_time > 0 else 0
        return {
            'frames_captured': self.frames_captured,
            'total_bytes': self.total_bytes,
            'elapsed_seconds': elapsed,
            'avg_fps': self.frames_captured / elapsed if elapsed > 0 else 0,
            'avg_kbps': (self.total_bytes * 8 / 1000) / elapsed if elapsed > 0 else 0
        }


class TCPAudioSender:
    # Envoie les buffers audio vers le Mac via TCP.

    def __init__(self, mac_ip: str, port: int):
        # Initialise l'objet.
        self.mac_ip = mac_ip
        self.port = port
        self.socket: Optional[socket.socket] = None
        self.is_connected = False
        self.reconnect_delay = 1.0  # secondes
        self.max_reconnect_attempts = 10

        # Stats
        self.packets_sent = 0
        self.bytes_sent = 0
        self.connection_errors = 0

        # Lock pour thread-safety
        self._lock = threading.Lock()

    def connect(self) -> bool:
        # Établit la connexion TCP vers le Mac.
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.socket.settimeout(5.0)

            print(f"[TCP] Connexion à {self.mac_ip}:{self.port}...")
            self.socket.connect((self.mac_ip, self.port))

            # Envoyer le handshake
            self._send_handshake()

            self.is_connected = True
            print(f"[TCP] Connecté au Mac")
            return True

        except socket.error as e:
            print(f"[TCP] Erreur connexion: {e}")
            self.connection_errors += 1
            self.is_connected = False
            return False

    def _send_handshake(self):
        # Envoie le handshake initial avec les paramètres audio.
        # Format: MAGIC(4) + VERSION(1) + SAMPLE_RATE(4) + CHANNELS(1) + SAMPLE_WIDTH(1)
        handshake = struct.pack(
            '<4sBIBB',
            MAGIC_HEADER,
            VERSION,
            SAMPLE_RATE,
            CHANNELS,
            SAMPLE_WIDTH
        )
        self.socket.sendall(handshake)

    def send_audio(self, audio_data: bytes, timestamp_sec: int, timestamp_usec: int) -> bool:
        # Envoie un buffer audio avec son timestamp.
        if not self.is_connected:
            return False

        with self._lock:
            try:
                # Header: taille + timestamp
                header = struct.pack(
                    '<III',
                    len(audio_data),
                    timestamp_sec,
                    timestamp_usec
                )

                # Envoyer header puis données
                self.socket.sendall(header + audio_data)

                self.packets_sent += 1
                self.bytes_sent += len(header) + len(audio_data)

                return True

            except socket.error as e:
                print(f"[TCP] Erreur envoi: {e}")
                self.is_connected = False
                self.connection_errors += 1
                return False

    def reconnect(self) -> bool:
        # Tente de se reconnecter.
        self.close()
        time.sleep(self.reconnect_delay)
        return self.connect()

    def close(self):
        # Ferme la connexion.
        if self.socket:
            try:
                self.socket.close()
            except:
                pass
        self.socket = None
        self.is_connected = False

    def get_stats(self) -> dict:
        # Retourne les statistiques d'envoi.
        return {
            'packets_sent': self.packets_sent,
            'bytes_sent': self.bytes_sent,
            'connection_errors': self.connection_errors,
            'is_connected': self.is_connected
        }


class PepperAudioCapture:
    # Classe principale orchestrant la capture audio sur Pepper

    def __init__(self, pepper_ip: str, pepper_port: int,
                 # Initialise l'objet.
                 mac_ip: str, mac_port: int):
        self.pepper_ip = pepper_ip
        self.pepper_port = pepper_port
        self.mac_ip = mac_ip
        self.mac_port = mac_port

        self.capture_module = AudioCaptureModule()
        self.tcp_sender = TCPAudioSender(mac_ip, mac_port)

        self.session = None
        self.audio_service = None
        self.is_running = False

        # Configuration capture
        self.buffer_size = BUFFER_SIZE

    def _on_audio_data(self, audio_data: bytes, channels: int,
                       # Gere audio data.
                       samples_per_channel: int, timestamp: list):
        """Callback appelé pour chaque buffer audio capturé."""
        if not self.tcp_sender.is_connected:
            # Tenter reconnexion
            if not self.tcp_sender.reconnect():
                return

        # Envoyer via TCP
        ts_sec = timestamp[0] if len(timestamp) > 0 else 0
        ts_usec = timestamp[1] if len(timestamp) > 1 else 0

        self.tcp_sender.send_audio(audio_data, ts_sec, ts_usec)

    def start(self) -> bool:
        # Démarre la capture audio.
        try:
            # Importer qi ici pour éviter erreur si pas sur Pepper
            import qi

            print(f"[CAPTURE] Connexion à Pepper {self.pepper_ip}:{self.pepper_port}...")

            self.session = qi.Session()
            self.session.connect(f"tcp://{self.pepper_ip}:{self.pepper_port}")

            self.audio_service = self.session.service("ALAudioDevice")

            # Configurer le callback
            self.capture_module.on_audio_callback = self._on_audio_data

            # S'abonner au flux audio
            # setClientPreferences(name, sampleRate, channels, deinterleave)
            self.audio_service.setClientPreferences(
                self.capture_module.name,
                SAMPLE_RATE,
                CHANNELS,
                0  # 0 = interleaved, 1 = deinterleaved
            )

            # Connecter au Mac
            if not self.tcp_sender.connect():
                print("[CAPTURE] Impossible de se connecter au Mac")
                return False

            # Démarrer la capture
            self.capture_module.start()

            # S'abonner (le callback sera appelé)
            self.audio_service.subscribe(self.capture_module.name)

            self.is_running = True
            print("[CAPTURE] Capture audio démarrée")
            return True

        except ImportError:
            print("[CAPTURE] SDK qi non disponible - Mode simulation")
            return self._start_simulation()
        except Exception as e:
            print(f"[CAPTURE] Erreur démarrage: {e}")
            return False

    def _start_simulation(self) -> bool:
        # Mode simulation pour tests sans Pepper.
        print("[CAPTURE] Démarrage en mode SIMULATION")

        # Connecter au Mac
        if not self.tcp_sender.connect():
            print("[CAPTURE] Impossible de se connecter au Mac")
            return False

        self.capture_module.start()
        self.is_running = True

        # Thread de simulation
        def simulate_audio():
            # Gere audio.
            import math
            sample_count = 0

            while self.is_running:
                # Générer un buffer de test (sinusoïde 440Hz)
                samples = []
                for i in range(BUFFER_SIZE):
                    t = (sample_count + i) / SAMPLE_RATE
                    # Même valeur sur les 4 canaux
                    sample = int(16000 * math.sin(2 * math.pi * 440 * t))
                    for _ in range(CHANNELS):
                        samples.append(sample)

                sample_count += BUFFER_SIZE

                # Convertir en bytes (little-endian signed 16-bit)
                audio_data = struct.pack(f'<{len(samples)}h', *samples)

                # Timestamp simulé
                now = time.time()
                ts_sec = int(now)
                ts_usec = int((now - ts_sec) * 1000000)

                # Envoyer
                self._on_audio_data(audio_data, CHANNELS, BUFFER_SIZE, [ts_sec, ts_usec])

                # Attendre le temps réel du buffer
                time.sleep(BUFFER_SIZE / SAMPLE_RATE)

        self._sim_thread = threading.Thread(target=simulate_audio, daemon=True)
        self._sim_thread.start()

        return True

    def stop(self):
        # Arrête la capture audio.
        self.is_running = False

        if self.capture_module:
            self.capture_module.stop()

        if self.audio_service:
            try:
                self.audio_service.unsubscribe(self.capture_module.name)
            except:
                pass

        if self.session:
            try:
                self.session.close()
            except:
                pass

        self.tcp_sender.close()
        print("[CAPTURE] Capture arrêtée")

    def print_stats(self):
        # Affiche les statistiques.
        capture_stats = self.capture_module.get_stats()
        tcp_stats = self.tcp_sender.get_stats()

        print("\n=== Statistiques Capture Audio ===")
        print(f"  Frames capturés: {capture_stats['frames_captured']}")
        print(f"  Bytes capturés: {capture_stats['total_bytes']:,}")
        print(f"  Durée: {capture_stats['elapsed_seconds']:.1f}s")
        print(f"  FPS moyen: {capture_stats['avg_fps']:.1f}")
        print(f"  Débit: {capture_stats['avg_kbps']:.1f} kbps")
        print(f"\n  Paquets TCP envoyés: {tcp_stats['packets_sent']}")
        print(f"  Bytes TCP envoyés: {tcp_stats['bytes_sent']:,}")
        print(f"  Erreurs connexion: {tcp_stats['connection_errors']}")


def main():
    # Gere l'action.
    parser = argparse.ArgumentParser(
        description="Capture audio Pepper vers Mac - Phase 1",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemples:
    # Sur Pepper, envoyer vers Mac
    python audio_capture.py --mac-ip 192.168.1.50 --port 5555

    # Mode simulation (sans Pepper)
    python audio_capture.py --mac-ip 192.168.1.50 --port 5555 --simulation
        """
    )
    parser.add_argument(
        '--pepper-ip',
        default='127.0.0.1',
        help="IP de Pepper (défaut: 127.0.0.1 pour exécution locale)"
    )
    parser.add_argument(
        '--pepper-port',
        type=int,
        default=9559,
        help="Port NAOqi (défaut: 9559)"
    )
    parser.add_argument(
        '--mac-ip',
        required=True,
        help="Adresse IP du Mac"
    )
    parser.add_argument(
        '--port',
        type=int,
        default=5555,
        help="Port TCP pour l'envoi audio (défaut: 5555)"
    )
    parser.add_argument(
        '--simulation',
        action='store_true',
        help="Mode simulation sans Pepper"
    )

    args = parser.parse_args()

    # Créer le module de capture
    capture = PepperAudioCapture(
        pepper_ip=args.pepper_ip,
        pepper_port=args.pepper_port,
        mac_ip=args.mac_ip,
        mac_port=args.port
    )

    # Gestion Ctrl+C
    def signal_handler(sig, frame):
        # Gere handler.
        print("\n[CAPTURE] Arrêt demandé...")
        capture.stop()
        capture.print_stats()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)

    # Démarrer
    if args.simulation:
        success = capture._start_simulation()
    else:
        success = capture.start()

    if not success:
        print("[CAPTURE] Échec du démarrage")
        sys.exit(1)

    # Boucle principale
    print("[CAPTURE] Appuyez sur Ctrl+C pour arrêter")
    try:
        while capture.is_running:
            time.sleep(1)
            # Afficher stats toutes les 10 secondes
            stats = capture.capture_module.get_stats()
            if int(stats['elapsed_seconds']) % 10 == 0 and stats['elapsed_seconds'] > 0:
                print(f"[STATS] {stats['frames_captured']} frames, "
                      f"{stats['total_bytes']/1024:.1f} KB, "
                      f"{stats['avg_kbps']:.0f} kbps")
    except KeyboardInterrupt:
        pass
    finally:
        capture.stop()
        capture.print_stats()


if __name__ == "__main__":
    main()
