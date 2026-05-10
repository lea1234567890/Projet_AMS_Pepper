#!/usr/bin/env python3
# Phase 4 - Configuration VAD (Voice Activity Detection)

from enum import Enum
from dataclasses import dataclass
from typing import Dict, Any


class VADPreset(Enum):
    # Presets VAD pour différentes situations.
    DEFAULT = "default"              # Configuration équilibrée
    HESITANT_SPEAKER = "hesitant"    # Locuteur hésitant ("euh...")
    FAST_SPEAKER = "fast"            # Locuteur rapide
    NOISY_ENVIRONMENT = "noisy"      # Environnement bruité
    QUIET_ENVIRONMENT = "quiet"      # Environnement silencieux
    ELDERLY = "elderly"              # Personnes âgées (pauses plus longues)


@dataclass
class VADConfig:
    # Configuration VAD pour OpenAI Realtime.
    type: str = "server_vad"
    threshold: float = 0.5
    prefix_padding_ms: int = 300
    silence_duration_ms: int = 500

    # Paramètres additionnels pour tuning fin
    min_speech_duration_ms: int = 100  # Ignorer sons < 100ms
    max_speech_duration_ms: int = 30000  # Timeout après 30s

    def to_dict(self) -> Dict[str, Any]:
        # Convertit en dict pour l'API OpenAI.
        return {
            "type": self.type,
            "threshold": self.threshold,
            "prefix_padding_ms": self.prefix_padding_ms,
            "silence_duration_ms": self.silence_duration_ms
        }


# PRESETS VAD

VAD_PRESETS: Dict[VADPreset, VADConfig] = {
    # Configuration par défaut - équilibrée
    VADPreset.DEFAULT: VADConfig(
        type="server_vad",
        threshold=0.5,
        prefix_padding_ms=300,
        silence_duration_ms=500
    ),

    # Locuteur hésitant - tolère les pauses "euh...", "hmm..."
    VADPreset.HESITANT_SPEAKER: VADConfig(
        type="server_vad",
        threshold=0.4,           # Plus sensible pour capter les "euh"
        prefix_padding_ms=400,   # Plus de padding avant
        silence_duration_ms=800  # Tolère 800ms de silence
    ),

    # Locuteur rapide - réponse rapide
    VADPreset.FAST_SPEAKER: VADConfig(
        type="server_vad",
        threshold=0.6,           # Moins sensible
        prefix_padding_ms=200,   # Moins de padding
        silence_duration_ms=350  # Réponse rapide
    ),

    # Environnement bruité - seuil élevé
    VADPreset.NOISY_ENVIRONMENT: VADConfig(
        type="server_vad",
        threshold=0.7,           # Seuil élevé pour filtrer bruit
        prefix_padding_ms=400,
        silence_duration_ms=600
    ),

    # Environnement silencieux - très sensible
    VADPreset.QUIET_ENVIRONMENT: VADConfig(
        type="server_vad",
        threshold=0.3,           # Très sensible
        prefix_padding_ms=250,
        silence_duration_ms=400
    ),

    # Personnes âgées - pauses plus longues
    VADPreset.ELDERLY: VADConfig(
        type="server_vad",
        threshold=0.4,
        prefix_padding_ms=500,   # Plus de padding
        silence_duration_ms=1200  # Tolère longues pauses
    )
}


def get_vad_config(preset: VADPreset = VADPreset.DEFAULT) -> VADConfig:
    # Retourne la configuration VAD pour un preset donné.
    return VAD_PRESETS.get(preset, VAD_PRESETS[VADPreset.DEFAULT])


def create_custom_vad_config(
    # Cree custom vad config.
    threshold: float = 0.5,
    silence_duration_ms: int = 500,
    prefix_padding_ms: int = 300
) -> VADConfig:
    """
    Crée une configuration VAD personnalisée.

    Args:
        threshold: Sensibilité (0.0-1.0)
        silence_duration_ms: Durée silence fin de phrase
        prefix_padding_ms: Audio conservé avant détection

    Returns:
        Configuration VAD personnalisée
    """
    return VADConfig(
        type="server_vad",
        threshold=max(0.0, min(1.0, threshold)),
        prefix_padding_ms=max(0, prefix_padding_ms),
        silence_duration_ms=max(100, silence_duration_ms)
    )


# RECOMMANDATIONS PAR SITUATION

VAD_RECOMMENDATIONS = """
╔══════════════════════════════════════════════════════════════════════════════╗
║                    RECOMMANDATIONS CONFIGURATION VAD                         ║
╠══════════════════════════════════════════════════════════════════════════════╣
║                                                                              ║
║  SITUATION                    │ PRESET            │ PARAMÈTRES CLÉS          ║
║  ─────────────────────────────┼───────────────────┼────────────────────────  ║
║  Client standard              │ DEFAULT           │ threshold=0.5            ║
║                               │                   │ silence=500ms            ║
║  ─────────────────────────────┼───────────────────┼────────────────────────  ║
║  Client qui hésite            │ HESITANT_SPEAKER  │ threshold=0.4            ║
║  "euh... je cherche..."       │                   │ silence=800ms            ║
║  ─────────────────────────────┼───────────────────┼────────────────────────  ║
║  Client pressé                │ FAST_SPEAKER      │ threshold=0.6            ║
║  Phrases rapides et courtes   │                   │ silence=350ms            ║
║  ─────────────────────────────┼───────────────────┼────────────────────────  ║
║  Magasin bruité               │ NOISY_ENVIRONMENT │ threshold=0.7            ║
║  Bruit de fond, musique       │                   │ silence=600ms            ║
║  ─────────────────────────────┼───────────────────┼────────────────────────  ║
║  Magasin calme                │ QUIET_ENVIRONMENT │ threshold=0.3            ║
║  Peu de bruit ambiant         │                   │ silence=400ms            ║
║  ─────────────────────────────┼───────────────────┼────────────────────────  ║
║  Personne âgée                │ ELDERLY           │ threshold=0.4            ║
║  Pauses réflexion longues     │                   │ silence=1200ms           ║
║                                                                              ║
╠══════════════════════════════════════════════════════════════════════════════╣
║                                                                              ║
║  CONSEILS DE TUNING:                                                         ║
║                                                                              ║
║  • Si coupures trop fréquentes → Augmenter silence_duration_ms               ║
║  • Si réponses trop lentes → Diminuer silence_duration_ms                    ║
║  • Si détection sur bruit → Augmenter threshold                              ║
║  • Si voix faible non détectée → Diminuer threshold                          ║
║  • Si début de mots coupés → Augmenter prefix_padding_ms                     ║
║                                                                              ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""


def print_recommendations():
    # Affiche les recommandations VAD.
    print(VAD_RECOMMENDATIONS)


# TEST

if __name__ == "__main__":
    print("=" * 60)
    print("CONFIGURATION VAD - PHASE 4")
    print("=" * 60)

    print_recommendations()

    print("\n[PRESETS DISPONIBLES]\n")

    for preset in VADPreset:
        config = get_vad_config(preset)
        print(f"  {preset.value.upper():<20}")
        print(f"    threshold:          {config.threshold}")
        print(f"    silence_duration:   {config.silence_duration_ms}ms")
        print(f"    prefix_padding:     {config.prefix_padding_ms}ms")
        print()

    print("\n[EXEMPLE CUSTOM]\n")
    custom = create_custom_vad_config(
        threshold=0.45,
        silence_duration_ms=700,
        prefix_padding_ms=350
    )
    print(f"  Custom config: {custom.to_dict()}")
