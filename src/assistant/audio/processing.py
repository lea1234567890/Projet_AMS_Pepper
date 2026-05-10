#!/usr/bin/env python3
# Phase 2 - Module de Traitement Audio Entrée

import numpy as np
from typing import Optional, Tuple, Dict, Any
from dataclasses import dataclass, field
from enum import Enum
import struct
import warnings

# Supprimer warnings scipy
warnings.filterwarnings('ignore', category=RuntimeWarning)


# CONFIGURATION

@dataclass
class AudioConfig:
    # Configuration audio pour Pepper et OpenAI.

    # Entrée Pepper
    input_sample_rate: int = 48000
    input_channels: int = 4
    input_sample_width: int = 2  # 16-bit

    # Sortie OpenAI Realtime
    output_sample_rate: int = 24000
    output_channels: int = 1  # Mono
    output_sample_width: int = 2  # PCM16

    # Beamforming
    beamforming_enabled: bool = True
    front_weight: float = 0.6      # Poids canal front (principal)
    side_weight: float = 0.25      # Poids canaux left/right
    rear_weight: float = 0.05     # Poids canal rear (rejection)

    # Réduction de bruit
    noise_reduction_enabled: bool = True
    noise_reduction_strength: float = 0.3  # 0.0-1.0, modéré par défaut
    noise_floor_db: float = -50.0  # Seuil de bruit

    # Contrôle de gain (AGC)
    agc_enabled: bool = True
    target_level_db: float = -20.0  # Niveau cible
    max_gain_db: float = 20.0       # Gain max
    min_gain_db: float = -10.0      # Gain min (atténuation)
    attack_time_ms: float = 10.0    # Temps d'attaque
    release_time_ms: float = 100.0  # Temps de relâchement

    # Anti-saturation
    limiter_enabled: bool = True
    limiter_threshold: float = 0.95  # Seuil de limitation

    # Filtre passe-haut (remove DC offset et basses fréquences)
    highpass_enabled: bool = True
    highpass_cutoff_hz: float = 80.0  # Coupe sous 80Hz


class BeamformingMode(Enum):
    # Modes de beamforming disponibles.
    WEIGHTED_SUM = "weighted_sum"      # Somme pondérée simple
    DELAY_AND_SUM = "delay_and_sum"    # Avec compensation de délai
    ADAPTIVE = "adaptive"              # Adaptatif selon énergie


# BEAMFORMING

class Beamformer:
    # Beamforming directionnel pour microphones Pepper.

    def __init__(self, config: AudioConfig):
        # Initialise l'objet.
        self.config = config
        self.channels = max(1, int(config.input_channels or 1))

        # Poids normalisés
        if self.channels == 1:
            self.weights = np.array([1.0], dtype=np.float64)
        elif self.channels == 4:
            total_weight = (config.front_weight + config.rear_weight +
                            2 * config.side_weight)
            if total_weight <= 0:
                total_weight = 1.0
            self.weights = np.array([
                config.front_weight / total_weight,   # Front
                config.rear_weight / total_weight,    # Rear
                config.side_weight / total_weight,    # Left
                config.side_weight / total_weight     # Right
            ], dtype=np.float64)
        else:
            self.weights = np.ones(self.channels, dtype=np.float64) / self.channels

        # Buffer pour mode adaptatif
        self._energy_history = []
        self._max_history = 50

    def process(self, audio_data: np.ndarray,
                # Traite l'action.
                mode: BeamformingMode = BeamformingMode.WEIGHTED_SUM) -> np.ndarray:
        """
        Applique le beamforming sur l'audio multicanal.

        Args:
            audio_data: Array (samples, channels) ou 1D interleavé
            mode: Mode de beamforming

        Returns:
            Array mono (samples,)
        """
        if audio_data.size == 0:
            return np.zeros((0,), dtype=np.float64)

        audio_nch = np.asarray(audio_data, dtype=np.float64)
        if audio_nch.ndim == 1:
            # Désentrelacer si nécessaire
            audio_nch = self._deinterleave(audio_nch, self.channels)

        if audio_nch.ndim == 1:
            audio_nch = audio_nch.reshape(-1, 1)

        channels = audio_nch.shape[1]
        if channels <= 1:
            return audio_nch[:, 0] if audio_nch.size else np.zeros((0,), dtype=np.float64)

        if channels != self.channels:
            self.channels = channels
            if self.weights.size != channels:
                self.weights = np.ones(channels, dtype=np.float64) / channels

        if mode == BeamformingMode.WEIGHTED_SUM:
            return self._weighted_sum(audio_nch)
        elif mode == BeamformingMode.DELAY_AND_SUM:
            return self._delay_and_sum(audio_nch)
        elif mode == BeamformingMode.ADAPTIVE:
            return self._adaptive(audio_nch)
        else:
            return self._weighted_sum(audio_nch)

    def _weighted_sum(self, audio_nch: np.ndarray) -> np.ndarray:
        # Beamforming par somme pondérée simple.
        # audio_nch shape: (samples, channels)
        if audio_nch.size == 0:
            return np.zeros((0,), dtype=np.float64)
        channels = audio_nch.shape[1]
        if channels <= 0:
            return np.zeros((0,), dtype=np.float64)
        if channels == 1:
            return audio_nch[:, 0]

        if self.weights.size != channels:
            weights = np.ones(channels, dtype=np.float64) / channels
        else:
            weights = self.weights
        return np.matmul(audio_nch, weights[:channels])

    def _delay_and_sum(self, audio_nch: np.ndarray) -> np.ndarray:
        # Beamforming avec compensation de délai.
        # Délais en samples (approximatifs pour source frontale)
        delays = [0, 14, 7, 7]  # Front, Rear, Left, Right
        channels = audio_nch.shape[1] if audio_nch.ndim > 1 else 1
        if channels <= 1:
            return audio_nch[:, 0] if audio_nch.size else np.zeros((0,), dtype=np.float64)
        if channels > len(delays):
            delays = delays + [7] * (channels - len(delays))

        mono = np.zeros(audio_nch.shape[0], dtype=np.float64)

        for ch in range(channels):
            delay = delays[ch] if ch < len(delays) else 0
            if delay > 0:
                # Décaler le signal
                shifted = np.roll(audio_nch[:, ch], -delay)
                shifted[-delay:] = 0  # Zéros à la fin
            else:
                shifted = audio_nch[:, ch]

            w = self.weights[ch] if ch < len(self.weights) else (1.0 / channels)
            mono += w * shifted

        return mono

    def _adaptive(self, audio_nch: np.ndarray) -> np.ndarray:
        # Beamforming adaptatif basé sur l'énergie.
        # Calculer l'énergie par canal
        channels = audio_nch.shape[1] if audio_nch.ndim > 1 else 1
        if channels <= 1:
            return audio_nch[:, 0] if audio_nch.size else np.zeros((0,), dtype=np.float64)

        energies = np.array([np.sum(audio_nch[:, ch]**2) for ch in range(channels)])

        # Éviter division par zéro
        total_energy = np.sum(energies) + 1e-10

        # Poids adaptatifs (combinaison fixe + adaptatif)
        adaptive_weights = energies / total_energy
        base_weights = self.weights if self.weights.size == channels else (
            np.ones(channels, dtype=np.float64) / channels
        )
        combined_weights = 0.7 * base_weights + 0.3 * adaptive_weights

        # Normaliser
        combined_weights /= np.sum(combined_weights)
        return np.matmul(audio_nch, combined_weights)

    def _deinterleave(self, interleaved: np.ndarray, channels: int) -> np.ndarray:
        # Convertit audio interleaved en (samples, channels).
        if channels <= 0:
            channels = 1
        samples_per_channel = len(interleaved) // channels
        if samples_per_channel <= 0:
            return np.zeros((0, channels), dtype=np.float64)
        return interleaved[:samples_per_channel * channels].reshape(samples_per_channel, channels)


# RÉDUCTION DE BRUIT

class NoiseReducer:
    # Réduction de bruit modérée préservant la voix naturelle.

    def __init__(self, config: AudioConfig):
        # Initialise l'objet.
        self.config = config
        self.noise_profile: Optional[np.ndarray] = None
        self._noise_frames = []

    def estimate_noise(self, audio: np.ndarray, duration_ms: float = 100):
        # Estime le profil de bruit depuis un segment silencieux.
        samples = int(self.config.input_sample_rate * duration_ms / 1000)
        samples = min(samples, len(audio))

        # Calculer le spectre moyen du bruit
        window_size = 1024
        hop_size = 512

        noise_spectra = []
        for i in range(0, samples - window_size, hop_size):
            frame = audio[i:i + window_size]
            spectrum = np.abs(np.fft.rfft(frame * np.hanning(window_size)))
            noise_spectra.append(spectrum)

        if noise_spectra:
            self.noise_profile = np.mean(noise_spectra, axis=0)

    def process(self, audio: np.ndarray) -> np.ndarray:
        # Applique la réduction de bruit.
        if not self.config.noise_reduction_enabled:
            return audio

        # Si pas de profil de bruit, estimer depuis les premiers frames
        if self.noise_profile is None:
            self._estimate_from_signal(audio)

        if self.noise_profile is None:
            return audio

        # Paramètres
        window_size = 1024
        hop_size = 512
        strength = self.config.noise_reduction_strength

        # Traitement par frames
        output = np.zeros_like(audio)
        window = np.hanning(window_size)

        num_frames = (len(audio) - window_size) // hop_size + 1

        for i in range(num_frames):
            start = i * hop_size
            end = start + window_size

            if end > len(audio):
                break

            # FFT
            frame = audio[start:end] * window
            spectrum = np.fft.rfft(frame)
            magnitude = np.abs(spectrum)
            phase = np.angle(spectrum)

            # Soustraction spectrale douce
            # On ne soustrait qu'une fraction du bruit (préserve la voix)
            noise_estimate = self.noise_profile * strength

            # Seuil spectral (évite les valeurs négatives)
            clean_magnitude = np.maximum(magnitude - noise_estimate, magnitude * 0.1)

            # Reconstruire
            clean_spectrum = clean_magnitude * np.exp(1j * phase)
            clean_frame = np.fft.irfft(clean_spectrum)

            # Overlap-add
            output[start:end] += clean_frame * window

        # Normaliser pour overlap-add
        output /= (window_size / hop_size / 2)

        return output

    def _estimate_from_signal(self, audio: np.ndarray):
        # Estime le bruit depuis les segments les plus faibles du signal.
        window_size = 1024
        hop_size = 512

        # Calculer l'énergie par frame
        energies = []
        for i in range(0, len(audio) - window_size, hop_size):
            frame = audio[i:i + window_size]
            energies.append(np.sum(frame**2))

        if not energies:
            return

        # Prendre les 10% de frames avec le moins d'énergie
        threshold = np.percentile(energies, 10)

        noise_spectra = []
        for i, energy in enumerate(energies):
            if energy <= threshold:
                start = i * hop_size
                frame = audio[start:start + window_size]
                spectrum = np.abs(np.fft.rfft(frame * np.hanning(window_size)))
                noise_spectra.append(spectrum)

        if noise_spectra:
            self.noise_profile = np.mean(noise_spectra, axis=0)


# CONTRÔLE DE GAIN (AGC)

class AutomaticGainControl:
    # Contrôle automatique du gain pour normaliser le volume.

    def __init__(self, config: AudioConfig):
        # Initialise l'objet.
        self.config = config

        # Constantes de temps
        sample_rate = config.input_sample_rate
        self.attack_coeff = np.exp(-1.0 / (config.attack_time_ms * sample_rate / 1000))
        self.release_coeff = np.exp(-1.0 / (config.release_time_ms * sample_rate / 1000))

        # État
        self.current_gain_db = 0.0
        self.envelope = 0.0

        # Convertir seuils en linéaire
        self.target_level = 10 ** (config.target_level_db / 20)
        self.max_gain = 10 ** (config.max_gain_db / 20)
        self.min_gain = 10 ** (config.min_gain_db / 20)

    def process(self, audio: np.ndarray) -> np.ndarray:
        # Applique le contrôle de gain automatique.
        if not self.config.agc_enabled:
            return audio

        output = np.zeros_like(audio)

        for i in range(len(audio)):
            # Détection d'enveloppe (valeur absolue lissée)
            abs_sample = np.abs(audio[i])

            if abs_sample > self.envelope:
                # Attack
                self.envelope = self.attack_coeff * self.envelope + (1 - self.attack_coeff) * abs_sample
            else:
                # Release
                self.envelope = self.release_coeff * self.envelope + (1 - self.release_coeff) * abs_sample

            # Calculer le gain nécessaire
            if self.envelope > 1e-6:
                desired_gain = self.target_level / self.envelope
            else:
                desired_gain = 1.0

            # Limiter le gain
            desired_gain = np.clip(desired_gain, self.min_gain, self.max_gain)

            # Lissage du gain (évite les sauts)
            gain_coeff = 0.999
            current_gain = gain_coeff * self.current_gain_db + (1 - gain_coeff) * desired_gain
            self.current_gain_db = current_gain

            # Appliquer
            output[i] = audio[i] * current_gain

        return output

    def reset(self):
        # Réinitialise l'état de l'AGC.
        self.current_gain_db = 0.0
        self.envelope = 0.0


# LIMITEUR

class SoftLimiter:
    # Limiteur doux pour éviter la saturation.

    def __init__(self, config: AudioConfig):
        # Initialise l'objet.
        self.config = config
        self.threshold = config.limiter_threshold

    def process(self, audio: np.ndarray) -> np.ndarray:
        # Applique une limitation douce (soft clipping).
        if not self.config.limiter_enabled:
            return audio

        # Soft clipping avec tanh
        # Normaliser par le threshold, appliquer tanh, dénormaliser
        normalized = audio / self.threshold
        limited = np.tanh(normalized) * self.threshold

        return limited


# FILTRE PASSE-HAUT

class HighPassFilter:
    # Filtre passe-haut pour supprimer DC offset et basses fréquences.

    def __init__(self, config: AudioConfig):
        # Initialise l'objet.
        self.config = config

        # Coefficient du filtre IIR simple
        # y[n] = alpha * (y[n-1] + x[n] - x[n-1])
        fc = config.highpass_cutoff_hz
        fs = config.input_sample_rate
        rc = 1.0 / (2 * np.pi * fc)
        dt = 1.0 / fs
        self.alpha = rc / (rc + dt)

        # État
        self.prev_input = 0.0
        self.prev_output = 0.0

    def process(self, audio: np.ndarray) -> np.ndarray:
        # Applique le filtre passe-haut.
        if not self.config.highpass_enabled:
            return audio

        output = np.zeros_like(audio)

        for i in range(len(audio)):
            output[i] = self.alpha * (self.prev_output + audio[i] - self.prev_input)
            self.prev_input = audio[i]
            self.prev_output = output[i]

        return output

    def reset(self):
        # Réinitialise l'état du filtre.
        self.prev_input = 0.0
        self.prev_output = 0.0


# RESAMPLER

class Resampler:
    # Conversion de fréquence d'échantillonnage.

    def __init__(self, input_rate: int, output_rate: int):
        # Initialise l'objet.
        self.input_rate = input_rate
        self.output_rate = output_rate
        self.ratio = output_rate / input_rate

    def process(self, audio: np.ndarray) -> np.ndarray:
        # Resample l'audio.
        if self.input_rate == self.output_rate:
            return audio

        # Utiliser scipy si disponible, sinon interpolation simple
        try:
            from scipy import signal
            num_samples = int(len(audio) * self.ratio)
            resampled = signal.resample(audio, num_samples)
            return resampled
        except ImportError:
            # Interpolation linéaire simple
            return self._linear_resample(audio)

    def _linear_resample(self, audio: np.ndarray) -> np.ndarray:
        # Resampling par interpolation linéaire.
        num_output_samples = int(len(audio) * self.ratio)
        output = np.zeros(num_output_samples)

        for i in range(num_output_samples):
            # Position dans le signal d'entrée
            pos = i / self.ratio
            idx = int(pos)
            frac = pos - idx

            if idx + 1 < len(audio):
                output[i] = audio[idx] * (1 - frac) + audio[idx + 1] * frac
            else:
                output[i] = audio[idx] if idx < len(audio) else 0

        return output


# PROCESSEUR PRINCIPAL

@dataclass
class ProcessingStats:
    # Statistiques de traitement.
    input_samples: int = 0
    output_samples: int = 0
    input_level_db: float = 0.0
    output_level_db: float = 0.0
    gain_applied_db: float = 0.0
    clipping_count: int = 0
    processing_time_ms: float = 0.0


class AudioProcessor:
    # Processeur audio principal combinant tous les traitements.

    def __init__(self, config: Optional[AudioConfig] = None):
        # Initialise l'objet.
        self.config = config or AudioConfig()

        # Initialiser les modules
        self.beamformer = Beamformer(self.config)
        self.highpass = HighPassFilter(self.config)
        self.noise_reducer = NoiseReducer(self.config)
        self.agc = AutomaticGainControl(self.config)
        self.limiter = SoftLimiter(self.config)
        self.resampler = Resampler(
            self.config.input_sample_rate,
            self.config.output_sample_rate
        )

        # Stats
        self.stats = ProcessingStats()

    def process(self, audio_bytes: bytes,
                # Traite l'action.
                beamforming_mode: BeamformingMode = BeamformingMode.WEIGHTED_SUM
                ) -> bytes:
        """
        Traite l'audio brut depuis Pepper vers format OpenAI.

        Args:
            audio_bytes: Audio brut PCM16 interleavé
            beamforming_mode: Mode de beamforming

        Returns:
            Audio PCM16 mono 24kHz
        """
        import time
        start_time = time.time()

        # 1. Décoder PCM16 → float
        audio_4ch = self._decode_pcm16(audio_bytes, self.config.input_channels)
        self.stats.input_samples = len(audio_4ch)
        self.stats.input_level_db = self._calculate_level_db(audio_4ch.flatten())

        # 2. Beamforming -> mono
        if self.config.input_channels <= 1 or not self.config.beamforming_enabled:
            mono = audio_4ch[:, 0] if audio_4ch.ndim == 2 and audio_4ch.shape[1] > 0 else np.zeros((0,), dtype=np.float64)
        else:
            mono = self.beamformer.process(audio_4ch, beamforming_mode)

        if mono.size == 0:
            self.stats.output_samples = 0
            self.stats.output_level_db = 0.0
            self.stats.gain_applied_db = 0.0
            self.stats.processing_time_ms = (time.time() - start_time) * 1000
            return b""

        # 3. Filtre passe-haut
        mono = self.highpass.process(mono)

        # 4. Réduction de bruit
        mono = self.noise_reducer.process(mono)

        # 5. Contrôle de gain
        mono = self.agc.process(mono)

        # 6. Limiteur
        mono = self.limiter.process(mono)

        # Compter clipping avant limitation
        self.stats.clipping_count = np.sum(np.abs(mono) > 0.99)

        # 7. Resampling 48kHz → 24kHz
        mono_24k = self.resampler.process(mono)

        # 8. Encoder float → PCM16
        output_bytes = self._encode_pcm16(mono_24k)

        # Stats
        self.stats.output_samples = len(mono_24k)
        self.stats.output_level_db = self._calculate_level_db(mono_24k)
        self.stats.gain_applied_db = self.stats.output_level_db - self.stats.input_level_db
        self.stats.processing_time_ms = (time.time() - start_time) * 1000

        return output_bytes

    def process_numpy(self, audio_4ch: np.ndarray,
                      # Traite numpy.
                      beamforming_mode: BeamformingMode = BeamformingMode.WEIGHTED_SUM
                      ) -> np.ndarray:
        """
        Version numpy du traitement (sans conversion bytes).

        Args:
            audio_4ch: Array (samples, 4) float

        Returns:
            Array mono 24kHz float
        """
        # 2. Beamforming
        if audio_4ch is None:
            return np.zeros((0,), dtype=np.float64)
        if audio_4ch.size == 0:
            return np.zeros((0,), dtype=np.float64)
        if audio_4ch.ndim == 1:
            audio_4ch = audio_4ch.reshape(-1, 1)

        if self.config.input_channels <= 1 or not self.config.beamforming_enabled:
            mono = audio_4ch[:, 0] if audio_4ch.shape[1] > 0 else np.zeros((0,), dtype=np.float64)
        else:
            mono = self.beamformer.process(audio_4ch, beamforming_mode)

        if mono.size == 0:
            return np.zeros((0,), dtype=np.float64)

        # 3-6. Filtres
        mono = self.highpass.process(mono)
        mono = self.noise_reducer.process(mono)
        mono = self.agc.process(mono)
        mono = self.limiter.process(mono)

        # 7. Resampling
        mono_24k = self.resampler.process(mono)

        return mono_24k

    def _decode_pcm16(self, data: bytes, channels: int) -> np.ndarray:
        # Décode PCM16 interleaved en float array (samples, channels).
        if not data:
            return np.zeros((0, max(1, int(channels or 1))), dtype=np.float64)

        if channels <= 0:
            channels = 1

        num_samples = len(data) // 2  # 2 bytes par sample
        samples = struct.unpack(f'<{num_samples}h', data)

        # Convertir en float [-1, 1]
        float_samples = np.array(samples, dtype=np.float64) / 32768.0

        # Reshape en (samples_per_channel, channels)
        samples_per_channel = len(float_samples) // channels
        if samples_per_channel <= 0:
            return np.zeros((0, channels), dtype=np.float64)
        float_samples = float_samples[:samples_per_channel * channels]
        return float_samples.reshape(samples_per_channel, channels)

    def _encode_pcm16(self, audio: np.ndarray) -> bytes:
        # Encode float array en PCM16.
        # Clip et convertir
        clipped = np.clip(audio, -1.0, 1.0)
        int_samples = (clipped * 32767).astype(np.int16)
        return int_samples.tobytes()

    def _calculate_level_db(self, audio: np.ndarray) -> float:
        # Calcule le niveau en dB.
        rms = np.sqrt(np.mean(audio**2))
        if rms > 0:
            return 20 * np.log10(rms)
        return -100.0

    def reset(self):
        # Réinitialise tous les états internes.
        self.highpass.reset()
        self.agc.reset()
        self.noise_reducer.noise_profile = None
        self.stats = ProcessingStats()

    def get_stats(self) -> Dict[str, Any]:
        # Retourne les statistiques de traitement.
        return {
            'input_samples': self.stats.input_samples,
            'output_samples': self.stats.output_samples,
            'input_level_db': round(self.stats.input_level_db, 1),
            'output_level_db': round(self.stats.output_level_db, 1),
            'gain_applied_db': round(self.stats.gain_applied_db, 1),
            'clipping_count': self.stats.clipping_count,
            'processing_time_ms': round(self.stats.processing_time_ms, 2)
        }


# CONFIGURATION PRÉRÉGLÉES

def get_preset_config(preset: str) -> AudioConfig:
    # Retourne une configuration préréglée.
    presets = {
        'default': AudioConfig(),

        'quiet_room': AudioConfig(
            noise_reduction_enabled=False,
            noise_reduction_strength=0.1,
            agc_enabled=True,
            max_gain_db=10.0
        ),

        'noisy_room': AudioConfig(
            noise_reduction_enabled=True,
            noise_reduction_strength=0.5,
            agc_enabled=True,
            front_weight=0.7,
            rear_weight=0.02
        ),

        'far_field': AudioConfig(
            agc_enabled=True,
            max_gain_db=30.0,
            target_level_db=-15.0,
            noise_reduction_strength=0.4
        ),

        'close_talk': AudioConfig(
            agc_enabled=True,
            max_gain_db=10.0,
            min_gain_db=-20.0,
            target_level_db=-25.0,
            noise_reduction_strength=0.2
        )
    }

    return presets.get(preset, presets['default'])


# TEST

if __name__ == "__main__":
    print("=" * 60)
    print("TEST MODULE TRAITEMENT AUDIO")
    print("=" * 60)

    # Créer un processeur
    config = AudioConfig()
    processor = AudioProcessor(config)

    print(f"\nConfiguration:")
    print(f"  Entrée: {config.input_sample_rate}Hz, {config.input_channels}ch")
    print(f"  Sortie: {config.output_sample_rate}Hz, {config.output_channels}ch")
    print(f"  Beamforming: {config.beamforming_enabled}")
    print(f"  Réduction bruit: {config.noise_reduction_enabled} (force={config.noise_reduction_strength})")
    print(f"  AGC: {config.agc_enabled}")

    # Générer un signal de test (4 canaux, 1 seconde)
    duration = 1.0
    t = np.linspace(0, duration, int(config.input_sample_rate * duration))

    # Simuler une voix (200-400Hz) + bruit
    voice = 0.3 * np.sin(2 * np.pi * 300 * t) * (1 + 0.5 * np.sin(2 * np.pi * 5 * t))
    noise = 0.05 * np.random.randn(len(t))

    # Canal front plus fort (source devant)
    ch_front = voice + noise
    ch_rear = 0.2 * voice + noise * 1.5
    ch_left = 0.6 * voice + noise * 1.2
    ch_right = 0.6 * voice + noise * 1.2

    # Assembler en interleaved
    audio_4ch = np.column_stack([ch_front, ch_rear, ch_left, ch_right])

    # Convertir en bytes PCM16
    audio_bytes = (audio_4ch.flatten() * 32767).astype(np.int16).tobytes()

    print(f"\nSignal de test:")
    print(f"  Durée: {duration}s")
    print(f"  Samples: {len(t)} x 4 canaux")
    print(f"  Taille: {len(audio_bytes)} bytes")

    # Traiter
    print("\nTraitement...")
    output_bytes = processor.process(audio_bytes)

    stats = processor.get_stats()
    print(f"\nRésultats:")
    print(f"  Entrée: {stats['input_samples']} samples ({stats['input_level_db']} dB)")
    print(f"  Sortie: {stats['output_samples']} samples ({stats['output_level_db']} dB)")
    print(f"  Gain appliqué: {stats['gain_applied_db']} dB")
    print(f"  Clipping: {stats['clipping_count']}")
    print(f"  Temps traitement: {stats['processing_time_ms']:.2f} ms")

    # Vérifier le ratio de resampling
    expected_samples = int(stats['input_samples'] * config.output_sample_rate / config.input_sample_rate)
    print(f"\n  Resampling 48kHz→24kHz: {stats['input_samples']} → {stats['output_samples']} (attendu: ~{expected_samples})")

    print("\n" + "=" * 60)
    print("TEST TERMINÉ")
    print("=" * 60)
