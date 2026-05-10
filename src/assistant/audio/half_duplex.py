#!/usr/bin/env python3
# Phase 4 - Stratégie Demi-Duplex

import threading
import time
import struct
from dataclasses import dataclass, field
from typing import Optional, Callable, List
from enum import Enum


class SpeakingState(Enum):
    # États du robot.
    LISTENING = "listening"      # Robot écoute
    SPEAKING = "speaking"        # Robot parle
    COOLDOWN = "cooldown"        # Période tampon après parole


@dataclass
class HalfDuplexConfig:
    # Configuration demi-duplex.
    # Délai après fin de parole avant de réécouter
    cooldown_ms: int = 200

    # Fade out/in en ms pour éviter clicks
    fade_duration_ms: int = 20

    # Niveau de silence (0 = silence total)
    silence_level: float = 0.0

    # Log des changements d'état
    log_state_changes: bool = True


@dataclass
class SpeakingStats:
    # Statistiques de parole.
    total_speaking_time_ms: float = 0
    total_listening_time_ms: float = 0
    speaking_count: int = 0
    interruption_count: int = 0
    last_speaking_duration_ms: float = 0

    # Timestamps
    session_start: float = field(default_factory=time.time)
    last_state_change: float = field(default_factory=time.time)


class HalfDuplexManager:
    # Gestionnaire demi-duplex pour éviter les boucles audio.

    def __init__(self, config: Optional[HalfDuplexConfig] = None):
        # Initialise l'objet.
        self.config = config or HalfDuplexConfig()

        # État actuel
        self._state = SpeakingState.LISTENING
        self._state_lock = threading.Lock()

        # Timestamps
        self._speaking_start: Optional[float] = None
        self._speaking_end: Optional[float] = None

        # Statistiques
        self.stats = SpeakingStats()

        # Callbacks
        self._on_state_change: List[Callable[[SpeakingState], None]] = []

        # Timer pour cooldown
        self._cooldown_timer: Optional[threading.Timer] = None

    @property
    def state(self) -> SpeakingState:
        # État actuel.
        with self._state_lock:
            return self._state

    @property
    def is_speaking(self) -> bool:
        # True si Pepper parle ou en cooldown.
        with self._state_lock:
            return self._state in (SpeakingState.SPEAKING, SpeakingState.COOLDOWN)

    @property
    def is_listening(self) -> bool:
        # True si Pepper écoute.
        with self._state_lock:
            return self._state == SpeakingState.LISTENING

    def start_speaking(self):
        # Appelé quand Pepper commence à parler.
        with self._state_lock:
            if self._state == SpeakingState.SPEAKING:
                return  # Déjà en train de parler

            # Annuler cooldown en cours
            if self._cooldown_timer:
                self._cooldown_timer.cancel()
                self._cooldown_timer = None

            # Mise à jour stats
            if self._state == SpeakingState.LISTENING:
                listening_duration = (time.time() - self.stats.last_state_change) * 1000
                self.stats.total_listening_time_ms += listening_duration

            # Changement d'état
            old_state = self._state
            self._state = SpeakingState.SPEAKING
            self._speaking_start = time.time()
            self.stats.speaking_count += 1
            self.stats.last_state_change = time.time()

            if self.config.log_state_changes:
                print(f"[HalfDuplex] État: {old_state.value} → {self._state.value}")

        # Notifier callbacks
        self._notify_state_change(SpeakingState.SPEAKING)

    def stop_speaking(self):
        # Appelé quand Pepper arrête de parler.
        with self._state_lock:
            if self._state != SpeakingState.SPEAKING:
                return  # N'était pas en train de parler

            # Calculer durée de parole
            if self._speaking_start:
                duration = (time.time() - self._speaking_start) * 1000
                self.stats.total_speaking_time_ms += duration
                self.stats.last_speaking_duration_ms = duration

            self._speaking_end = time.time()

            # Passer en cooldown
            old_state = self._state
            self._state = SpeakingState.COOLDOWN
            self.stats.last_state_change = time.time()

            if self.config.log_state_changes:
                print(f"[HalfDuplex] État: {old_state.value} → {self._state.value}")

            # Timer pour fin de cooldown
            cooldown_sec = self.config.cooldown_ms / 1000.0
            self._cooldown_timer = threading.Timer(cooldown_sec, self._end_cooldown)
            self._cooldown_timer.start()

        # Notifier callbacks
        self._notify_state_change(SpeakingState.COOLDOWN)

    def _end_cooldown(self):
        # Fin de la période de cooldown.
        with self._state_lock:
            if self._state != SpeakingState.COOLDOWN:
                return

            old_state = self._state
            self._state = SpeakingState.LISTENING
            self.stats.last_state_change = time.time()

            if self.config.log_state_changes:
                print(f"[HalfDuplex] État: {old_state.value} → {self._state.value}")

        # Notifier callbacks
        self._notify_state_change(SpeakingState.LISTENING)

    def filter_input_audio(self, audio_bytes: bytes, sample_width: int = 2) -> bytes:
        # Filtre l'audio d'entrée selon l'état.
        if self.is_listening:
            return audio_bytes

        # Générer silence
        if self.config.silence_level == 0.0:
            return bytes(len(audio_bytes))

        # Silence avec niveau non-nul (bruit de fond faible)
        num_samples = len(audio_bytes) // sample_width
        silence_value = int(self.config.silence_level * 32767)

        if sample_width == 2:
            return struct.pack(f'<{num_samples}h', *([silence_value] * num_samples))
        else:
            return bytes(len(audio_bytes))

    def filter_input_audio_with_fade(
        # Gere input audio with fade.
        self,
        audio_bytes: bytes,
        sample_rate: int = 24000,
        sample_width: int = 2
    ) -> bytes:
        """
        Filtre avec fade in/out pour éviter les clicks.

        Args:
            audio_bytes: Audio PCM brut
            sample_rate: Fréquence d'échantillonnage
            sample_width: Octets par sample

        Returns:
            Audio filtré avec fade
        """
        if self.is_listening and not self._is_near_state_change():
            return audio_bytes

        if not self.is_listening and not self._is_near_state_change():
            return bytes(len(audio_bytes))

        # Appliquer fade
        num_samples = len(audio_bytes) // sample_width
        fade_samples = int(self.config.fade_duration_ms * sample_rate / 1000)
        fade_samples = min(fade_samples, num_samples)

        # Décoder samples
        if sample_width == 2:
            samples = list(struct.unpack(f'<{num_samples}h', audio_bytes))
        else:
            return audio_bytes  # Non supporté

        # Appliquer fade selon direction
        time_since_change = (time.time() - self.stats.last_state_change) * 1000
        fade_progress = min(1.0, time_since_change / self.config.fade_duration_ms)

        if self.is_listening:
            # Fade in (retour écoute)
            for i in range(min(fade_samples, num_samples)):
                factor = fade_progress + (1 - fade_progress) * (i / fade_samples)
                samples[i] = int(samples[i] * factor)
        else:
            # Fade out (début parole)
            for i in range(min(fade_samples, num_samples)):
                factor = 1 - fade_progress * (i / fade_samples)
                samples[i] = int(samples[i] * factor)
            # Reste en silence
            for i in range(fade_samples, num_samples):
                samples[i] = 0

        return struct.pack(f'<{num_samples}h', *samples)

    def _is_near_state_change(self) -> bool:
        # True si proche d'un changement d'état (pour fade).
        time_since_change = (time.time() - self.stats.last_state_change) * 1000
        return time_since_change < self.config.fade_duration_ms * 2

    def register_callback(self, callback: Callable[[SpeakingState], None]):
        # Enregistre un callback pour les changements d'état.
        self._on_state_change.append(callback)

    def unregister_callback(self, callback: Callable[[SpeakingState], None]):
        # Supprime un callback.
        if callback in self._on_state_change:
            self._on_state_change.remove(callback)

    def _notify_state_change(self, new_state: SpeakingState):
        # Notifie tous les callbacks.
        for callback in self._on_state_change:
            try:
                callback(new_state)
            except Exception as e:
                print(f"[HalfDuplex] Erreur callback: {e}")

    def force_listening(self):
        # Force le retour à l'état écoute (interruption).
        with self._state_lock:
            if self._state == SpeakingState.LISTENING:
                return

            # Annuler timers
            if self._cooldown_timer:
                self._cooldown_timer.cancel()
                self._cooldown_timer = None

            # Compter comme interruption
            if self._state == SpeakingState.SPEAKING:
                self.stats.interruption_count += 1

            old_state = self._state
            self._state = SpeakingState.LISTENING
            self.stats.last_state_change = time.time()

            if self.config.log_state_changes:
                print(f"[HalfDuplex] INTERRUPTION: {old_state.value} → {self._state.value}")

        self._notify_state_change(SpeakingState.LISTENING)

    def get_stats(self) -> dict:
        # Retourne les statistiques.
        session_duration = (time.time() - self.stats.session_start) * 1000

        return {
            "state": self.state.value,
            "session_duration_ms": session_duration,
            "total_speaking_ms": self.stats.total_speaking_time_ms,
            "total_listening_ms": self.stats.total_listening_time_ms,
            "speaking_count": self.stats.speaking_count,
            "interruption_count": self.stats.interruption_count,
            "last_speaking_duration_ms": self.stats.last_speaking_duration_ms,
            "speaking_ratio": (
                self.stats.total_speaking_time_ms / session_duration
                if session_duration > 0 else 0
            )
        }

    def reset_stats(self):
        # Remet les statistiques à zéro.
        self.stats = SpeakingStats()

    def cleanup(self):
        # Nettoyage des ressources.
        if self._cooldown_timer:
            self._cooldown_timer.cancel()
            self._cooldown_timer = None
        self._on_state_change.clear()


# INTÉGRATION AVEC OPENAI REALTIME

class RealtimeHalfDuplexAdapter:
    # Adaptateur pour intégrer HalfDuplex avec OpenAI Realtime.

    def __init__(self, half_duplex: HalfDuplexManager):
        # Initialise l'objet.
        self.half_duplex = half_duplex
        self._audio_started = False

    def handle_realtime_event(self, event_type: str, event_data: dict):
        # Gère les événements OpenAI Realtime.
        # Début de réponse audio
        if event_type == "response.audio.delta":
            if not self._audio_started:
                self._audio_started = True
                self.half_duplex.start_speaking()

        # Fin de réponse audio
        elif event_type == "response.audio.done":
            self._audio_started = False
            self.half_duplex.stop_speaking()

        # Réponse complète terminée
        elif event_type == "response.done":
            if self._audio_started:
                self._audio_started = False
                self.half_duplex.stop_speaking()

        # Interruption utilisateur détectée
        elif event_type == "input_audio_buffer.speech_started":
            # L'utilisateur parle pendant que Pepper parle
            if self.half_duplex.is_speaking:
                self.half_duplex.force_listening()

        # Erreur - retour à écoute
        elif event_type == "error":
            if self._audio_started:
                self._audio_started = False
                self.half_duplex.stop_speaking()


# TEST

if __name__ == "__main__":
    import time

    print("=" * 60)
    print("TEST HALF-DUPLEX - PHASE 4")
    print("=" * 60)

    # Créer manager
    config = HalfDuplexConfig(
        cooldown_ms=300,
        fade_duration_ms=30,
        log_state_changes=True
    )
    manager = HalfDuplexManager(config)

    # Callback de test
    def on_state_change(state: SpeakingState):
        # Gere state change.
        print(f"  → Callback: nouvel état = {state.value}")

    manager.register_callback(on_state_change)

    # Simuler audio
    test_audio = bytes([0x00, 0x10] * 1000)  # 1000 samples 16-bit

    print("\n[1] ÉTAT INITIAL (LISTENING)")
    print(f"  État: {manager.state.value}")
    print(f"  is_speaking: {manager.is_speaking}")
    print(f"  is_listening: {manager.is_listening}")

    filtered = manager.filter_input_audio(test_audio)
    print(f"  Audio filtré: {'silence' if filtered == bytes(len(test_audio)) else 'original'}")

    print("\n[2] PEPPER COMMENCE À PARLER")
    manager.start_speaking()
    print(f"  État: {manager.state.value}")

    filtered = manager.filter_input_audio(test_audio)
    print(f"  Audio filtré: {'silence' if filtered == bytes(len(test_audio)) else 'original'}")

    print("\n[3] PEPPER PARLE PENDANT 500ms")
    time.sleep(0.5)

    print("\n[4] PEPPER ARRÊTE DE PARLER")
    manager.stop_speaking()
    print(f"  État: {manager.state.value}")

    filtered = manager.filter_input_audio(test_audio)
    print(f"  Audio filtré: {'silence' if filtered == bytes(len(test_audio)) else 'original'}")

    print("\n[5] ATTENTE FIN COOLDOWN")
    time.sleep(0.4)
    print(f"  État: {manager.state.value}")

    filtered = manager.filter_input_audio(test_audio)
    print(f"  Audio filtré: {'silence' if filtered == bytes(len(test_audio)) else 'original'}")

    print("\n[6] TEST INTERRUPTION")
    manager.start_speaking()
    time.sleep(0.1)
    print(f"  État pendant parole: {manager.state.value}")
    manager.force_listening()
    print(f"  État après interruption: {manager.state.value}")

    print("\n[7] STATISTIQUES")
    stats = manager.get_stats()
    for key, value in stats.items():
        if isinstance(value, float):
            print(f"  {key}: {value:.1f}")
        else:
            print(f"  {key}: {value}")

    # Nettoyage
    manager.cleanup()

    print("\n" + "=" * 60)
    print("TEST TERMINÉ")
    print("=" * 60)
