#!/usr/bin/env python3
# Phase 7 - Module Sécurité

import os
import re
import time
import json
import wave
import struct
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Set, Tuple
from enum import Enum
import hashlib


# CONFIGURATION

# Dossier pour les fichiers audio pré-générés
AUDIO_DIR = Path(__file__).parent / "audio"

# Latence cible pour le filtre
FILTER_LATENCY_TARGET_MS = 1.0


# TYPES D'ALERTES

class AlertType(Enum):
    # Types d'alertes de sécurité.
    NONE = "none"                    # Pas d'alerte
    MEDICATION = "medication"         # Médicament détecté (EAN)
    MEDICAL_KEYWORD = "medical"       # Mot-clé médical détecté
    DANGEROUS_USE = "dangerous"       # Usage dangereux
    ANIMAL_USE = "animal"             # Usage animal
    MEDICAL_CONDITION = "condition"   # Condition médicale mentionnée


class AlertSeverity(Enum):
    # Gravité de l'alerte.
    LOW = "low"           # Information simple
    MEDIUM = "medium"     # Redirection pharmacien
    HIGH = "high"         # Refus catégorique
    CRITICAL = "critical"  # Alerte urgente


# STRUCTURES DE DONNÉES

@dataclass
class SecurityAlert:
    # Résultat d'une vérification de sécurité.
    triggered: bool = False
    alert_type: AlertType = AlertType.NONE
    severity: AlertSeverity = AlertSeverity.LOW
    matched_keywords: List[str] = field(default_factory=list)
    matched_ean: str = ""
    response: str = ""
    audio_file: str = ""
    robot_action: str = ""  # "led_orange", "gesture_stop", etc.
    check_time_ms: float = 0.0


@dataclass
class SecurityConfig:
    # Configuration du module de sécurité.
    enable_ean_check: bool = True
    enable_keyword_filter: bool = True
    enable_dangerous_use_check: bool = True
    enable_animal_check: bool = True
    log_alerts: bool = True
    strict_mode: bool = True  # Refuse tout terme médical


# MOTS-CLÉS MÉDICAUX

# Liste des mots-clés médicaux à détecter
MEDICAL_KEYWORDS = {
    # Symptômes
    "douleur", "douleurs",
    "symptôme", "symptômes", "symptome", "symptomes",
    "gonflement", "gonflements",
    "fièvre", "fievre",

    # Conditions médicales
    "allergie", "allergies", "allergique",
    "eczéma", "eczema",
    "psoriasis",
    "dermite", "dermatite",
    "infection", "infections",
    "maladie", "maladies",
    "pathologie", "pathologies",
    "alopécie", "alopecie",

    # Termes médicaux
    "traitement", "traitements",
    "médicament", "médicaments", "medicament",
    "ordonnance", "ordonnances",
    "diagnostic", "diagnostique",
    "prescription", "prescriptions",
    "posologie",
    "contre-indication", "contre-indications", "contreindication",
    "effet secondaire", "effets secondaires",
    "surdosage",
    "intoxication",

    # Actions médicales
    "médecin", "medecin", "docteur",
    "dermatologue", "dermato",
    "urgence", "urgences",
    "hôpital", "hopital",
    "samu",

    # Substances
    "antibiotique", "antibiotiques",
    "antifongique", "antifongiques",
    "cortisone", "corticoïde", "corticoide",
    "antihistaminique",
}

# Mots-clés pour usage dangereux
DANGEROUS_USE_KEYWORDS = {
    "boire", "avaler", "ingérer", "ingerer", "manger",
    "yeux", "oeil", "œil",
    "bouche", "lèvres", "levres",
    "enfant", "enfants", "bébé", "bebe", "nourrisson",
    "enceinte", "grossesse", "allaitement", "allaitante",
}

# Mots-clés pour usage animal
ANIMAL_KEYWORDS = {
    "chien", "chiens", "chat", "chats",
    "animal", "animaux", "vétérinaire", "veterinaire",
    "cheval", "chevaux", "lapin", "hamster",
}

# Conditions médicales spécifiques
MEDICAL_CONDITIONS = {
    "tension", "hypertension", "hypotension",
    "diabète", "diabete", "diabétique",
    "cancer", "tumeur",
    "thyroïde", "thyroide",
    "cardiaque", "coeur", "cœur",
    "foie", "hépatique", "hepatique",
    "rein", "rénal", "renal",
    "poumon", "pulmonaire",
}


# RÉPONSES PRÉ-GÉNÉRÉES

SECURITY_RESPONSES = {
    "medical_generic": {
        "text": "Cette question dépasse mes compétences. Je te conseille de consulter le pharmacien qui pourra t'aider.",
        "audio": "medical_generic.wav",
        "severity": AlertSeverity.MEDIUM
    },
    "medication_detected": {
        "text": "Je détecte un médicament. Je ne suis pas habilité à donner des informations sur les médicaments. Le pharmacien peut t'aider.",
        "audio": "medication_detected.wav",
        "severity": AlertSeverity.HIGH
    },
    "dangerous_use": {
        "text": "Attention, ce produit est uniquement destiné à un usage externe sur les cheveux. Ne jamais avaler ni mettre en contact avec les yeux ou la bouche.",
        "audio": "dangerous_use.wav",
        "severity": AlertSeverity.HIGH
    },
    "animal_use": {
        "text": "Ces produits sont conçus uniquement pour les humains. Pour ton animal, je te conseille de consulter un vétérinaire.",
        "audio": "animal_use.wav",
        "severity": AlertSeverity.MEDIUM
    },
    "medical_condition": {
        "text": "Tu mentionnes une condition médicale. Je ne peux pas te conseiller à ce sujet. Le pharmacien pourra t'orienter.",
        "audio": "medical_condition.wav",
        "severity": AlertSeverity.MEDIUM
    },
    "children_warning": {
        "text": "Pour les enfants en bas âge, je te recommande de demander conseil au pharmacien avant utilisation.",
        "audio": "children_warning.wav",
        "severity": AlertSeverity.MEDIUM
    },
    "pregnancy_warning": {
        "text": "Pour les femmes enceintes ou allaitantes, je te conseille de consulter le pharmacien ou ton médecin.",
        "audio": "pregnancy_warning.wav",
        "severity": AlertSeverity.MEDIUM
    }
}


# MODULE SÉCURITÉ

class SecurityModule:
    # Module de sécurité pour l'assistant parapharmacie.

    def __init__(
        # Initialise l'objet.
        self,
        config: Optional[SecurityConfig] = None,
        blacklist_path: Optional[str] = None
    ):
        self.config = config or SecurityConfig()

        # Compiler les regex pour performance
        self._compile_patterns()

        # Charger la blacklist EAN
        self._ean_blacklist: Set[str] = set()
        self._ean_prefixes: Set[str] = set()

        if blacklist_path:
            self._load_blacklist(blacklist_path)
        else:
            # Charger depuis Phase 6
            default_path = Path(__file__).parent.parent / "phase6_database" / "blacklist_medicaments.json"
            if default_path.exists():
                self._load_blacklist(str(default_path))

        # Statistiques
        self._stats = {
            "total_checks": 0,
            "alerts_triggered": 0,
            "avg_check_time_ms": 0.0,
            "by_type": {t.value: 0 for t in AlertType}
        }

        # S'assurer que le dossier audio existe
        AUDIO_DIR.mkdir(exist_ok=True)

    def _compile_patterns(self):
        # Compile les patterns regex pour performance optimale.
        # Pattern pour mots médicaux (boundary matching)
        all_medical = MEDICAL_KEYWORDS | MEDICAL_CONDITIONS
        pattern = r'\b(' + '|'.join(re.escape(w) for w in all_medical) + r')\b'
        self._medical_pattern = re.compile(pattern, re.IGNORECASE)

        # Pattern pour usage dangereux
        pattern = r'\b(' + '|'.join(re.escape(w) for w in DANGEROUS_USE_KEYWORDS) + r')\b'
        self._dangerous_pattern = re.compile(pattern, re.IGNORECASE)

        # Pattern pour animaux
        pattern = r'\b(' + '|'.join(re.escape(w) for w in ANIMAL_KEYWORDS) + r')\b'
        self._animal_pattern = re.compile(pattern, re.IGNORECASE)

    def _load_blacklist(self, path: str):
        # Charge la blacklist depuis un fichier JSON.
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            # Charger les EAN
            for item in data.get('ean_codes', []):
                ean = item.get('ean13', '')
                if ean:
                    self._ean_blacklist.add(ean)

            # Charger les préfixes
            for item in data.get('prefixes', []):
                prefix = item.get('prefix', '')
                if prefix:
                    self._ean_prefixes.add(prefix)

            print(f"[SECURITY] Blacklist chargée: {len(self._ean_blacklist)} EAN, {len(self._ean_prefixes)} préfixes")

        except Exception as e:
            print(f"[SECURITY] Erreur chargement blacklist: {e}")

    # VÉRIFICATIONS

    def check_ean(self, ean13: str) -> SecurityAlert:
        # Vérifie si un EAN correspond à un médicament.
        start_time = time.time()

        alert = SecurityAlert()

        if not self.config.enable_ean_check or not ean13:
            alert.check_time_ms = (time.time() - start_time) * 1000
            return alert

        # Vérifier EAN exact
        if ean13 in self._ean_blacklist:
            alert.triggered = True
            alert.alert_type = AlertType.MEDICATION
            alert.severity = AlertSeverity.HIGH
            alert.matched_ean = ean13
            alert.response = SECURITY_RESPONSES["medication_detected"]["text"]
            alert.audio_file = str(AUDIO_DIR / SECURITY_RESPONSES["medication_detected"]["audio"])
            alert.robot_action = "led_orange,gesture_stop"

        # Vérifier préfixes
        elif any(ean13.startswith(prefix) for prefix in self._ean_prefixes):
            alert.triggered = True
            alert.alert_type = AlertType.MEDICATION
            alert.severity = AlertSeverity.HIGH
            alert.matched_ean = ean13
            alert.response = SECURITY_RESPONSES["medication_detected"]["text"]
            alert.audio_file = str(AUDIO_DIR / SECURITY_RESPONSES["medication_detected"]["audio"])
            alert.robot_action = "led_orange,gesture_stop"

        alert.check_time_ms = (time.time() - start_time) * 1000
        self._update_stats(alert)

        return alert

    def check_text(self, text: str) -> SecurityAlert:
        # Vérifie un texte pour les mots-clés sensibles.
        start_time = time.time()

        alert = SecurityAlert()

        if not text:
            alert.check_time_ms = (time.time() - start_time) * 1000
            return alert

        text_lower = text.lower()

        # 1. Vérifier usage animal (priorité haute)
        if self.config.enable_animal_check:
            matches = self._animal_pattern.findall(text_lower)
            if matches:
                alert.triggered = True
                alert.alert_type = AlertType.ANIMAL_USE
                alert.severity = AlertSeverity.MEDIUM
                alert.matched_keywords = list(set(matches))
                alert.response = SECURITY_RESPONSES["animal_use"]["text"]
                alert.audio_file = str(AUDIO_DIR / SECURITY_RESPONSES["animal_use"]["audio"])
                alert.robot_action = "led_orange"

        # 2. Vérifier usage dangereux
        if not alert.triggered and self.config.enable_dangerous_use_check:
            matches = self._dangerous_pattern.findall(text_lower)
            if matches:
                # Analyser le contexte
                dangerous_context = self._analyze_dangerous_context(text_lower, matches)
                if dangerous_context:
                    alert.triggered = True
                    alert.alert_type = AlertType.DANGEROUS_USE
                    alert.severity = AlertSeverity.HIGH
                    alert.matched_keywords = list(set(matches))
                    alert.response = dangerous_context["response"]
                    alert.audio_file = str(AUDIO_DIR / dangerous_context["audio"])
                    alert.robot_action = "led_orange,gesture_stop"

        # 3. Vérifier mots-clés médicaux
        if not alert.triggered and self.config.enable_keyword_filter:
            matches = self._medical_pattern.findall(text_lower)
            if matches:
                alert.triggered = True
                alert.alert_type = AlertType.MEDICAL_KEYWORD
                alert.severity = AlertSeverity.MEDIUM
                alert.matched_keywords = list(set(matches))
                alert.response = SECURITY_RESPONSES["medical_generic"]["text"]
                alert.audio_file = str(AUDIO_DIR / SECURITY_RESPONSES["medical_generic"]["audio"])
                alert.robot_action = "led_orange"

        alert.check_time_ms = (time.time() - start_time) * 1000
        self._update_stats(alert)

        # Vérifier la latence
        if alert.check_time_ms > FILTER_LATENCY_TARGET_MS:
            print(f"[SECURITY] ATTENTION: Latence filtre {alert.check_time_ms:.2f}ms > {FILTER_LATENCY_TARGET_MS}ms")

        return alert

    def _analyze_dangerous_context(self, text: str, matches: List[str]) -> Optional[Dict]:
        # Analyse le contexte pour les usages dangereux.
        # Ingestion
        if any(w in matches for w in ["boire", "avaler", "ingérer", "ingerer", "manger"]):
            return {
                "response": SECURITY_RESPONSES["dangerous_use"]["text"],
                "audio": SECURITY_RESPONSES["dangerous_use"]["audio"]
            }

        # Enfants
        if any(w in matches for w in ["enfant", "enfants", "bébé", "bebe", "nourrisson"]):
            return {
                "response": SECURITY_RESPONSES["children_warning"]["text"],
                "audio": SECURITY_RESPONSES["children_warning"]["audio"]
            }

        # Grossesse
        if any(w in matches for w in ["enceinte", "grossesse", "allaitement", "allaitante"]):
            return {
                "response": SECURITY_RESPONSES["pregnancy_warning"]["text"],
                "audio": SECURITY_RESPONSES["pregnancy_warning"]["audio"]
            }

        # Contact yeux/bouche
        if any(w in matches for w in ["yeux", "oeil", "œil", "bouche", "lèvres"]):
            return {
                "response": SECURITY_RESPONSES["dangerous_use"]["text"],
                "audio": SECURITY_RESPONSES["dangerous_use"]["audio"]
            }

        return None

    def check_full(self, text: str = "", ean13: str = "") -> SecurityAlert:
        # Vérification complète (texte + EAN).
        alerts = []

        if ean13:
            alerts.append(self.check_ean(ean13))

        if text:
            alerts.append(self.check_text(text))

        # Retourner l'alerte la plus grave
        triggered_alerts = [a for a in alerts if a.triggered]

        if not triggered_alerts:
            return SecurityAlert(check_time_ms=sum(a.check_time_ms for a in alerts))

        # Trier par gravité
        severity_order = {
            AlertSeverity.CRITICAL: 0,
            AlertSeverity.HIGH: 1,
            AlertSeverity.MEDIUM: 2,
            AlertSeverity.LOW: 3
        }

        triggered_alerts.sort(key=lambda a: severity_order[a.severity])
        return triggered_alerts[0]

    def _update_stats(self, alert: SecurityAlert):
        # Met à jour les statistiques.
        self._stats["total_checks"] += 1

        if alert.triggered:
            self._stats["alerts_triggered"] += 1
            self._stats["by_type"][alert.alert_type.value] += 1

        # Moyenne mobile du temps
        n = self._stats["total_checks"]
        old_avg = self._stats["avg_check_time_ms"]
        self._stats["avg_check_time_ms"] = old_avg + (alert.check_time_ms - old_avg) / n

    def get_stats(self) -> Dict:
        # Retourne les statistiques.
        return self._stats.copy()

    # GÉNÉRATION AUDIO

    def generate_security_audio(self, openai_client=None) -> Dict[str, str]:
        # Génère les fichiers audio de sécurité avec OpenAI TTS.
        generated = {}

        for key, response in SECURITY_RESPONSES.items():
            audio_path = AUDIO_DIR / response["audio"]

            # Skip si existe déjà
            if audio_path.exists():
                generated[key] = str(audio_path)
                continue

            text = response["text"]

            if openai_client:
                try:
                    # Utiliser OpenAI TTS
                    audio_response = openai_client.audio.speech.create(
                        model="tts-1",
                        voice="nova",  # Voix féminine naturelle
                        input=text,
                        response_format="wav"
                    )

                    with open(audio_path, 'wb') as f:
                        f.write(audio_response.content)

                    generated[key] = str(audio_path)
                    print(f"[SECURITY] Audio généré: {response['audio']}")

                except Exception as e:
                    print(f"[SECURITY] Erreur génération audio {key}: {e}")
                    # Fallback: générer un WAV silencieux
                    self._generate_silent_wav(audio_path, duration_ms=2000)
                    generated[key] = str(audio_path)
            else:
                # Mode sans OpenAI: générer WAV silencieux
                self._generate_silent_wav(audio_path, duration_ms=2000)
                generated[key] = str(audio_path)

        return generated

    def _generate_silent_wav(self, path: Path, duration_ms: int = 1000, sample_rate: int = 24000):
        # Génère un fichier WAV silencieux (placeholder).
        num_samples = int(sample_rate * duration_ms / 1000)
        samples = [0] * num_samples

        with wave.open(str(path), 'wb') as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            wav.writeframes(struct.pack(f'<{num_samples}h', *samples))

        print(f"[SECURITY] WAV placeholder généré: {path.name}")

    def get_audio_path(self, response_key: str) -> Optional[str]:
        # Retourne le chemin de l'audio pour une réponse.
        if response_key in SECURITY_RESPONSES:
            path = AUDIO_DIR / SECURITY_RESPONSES[response_key]["audio"]
            if path.exists():
                return str(path)
        return None


# ACTIONS ROBOT

class RobotSecurityActions:
    # Actions de sécurité sur le robot Pepper.

    def __init__(self, pepper_ip: str = None):
        # Initialise l'objet.
        self._naoqi = None
        self._leds = None
        self._motion = None
        self._tts = None

        if pepper_ip:
            self._connect(pepper_ip)

    def _connect(self, pepper_ip: str):
        # Connexion à Pepper.
        try:
            import qi
            session = qi.Session()
            session.connect(f"tcp://{pepper_ip}:9559")

            self._leds = session.service("ALLeds")
            self._motion = session.service("ALMotion")
            self._tts = session.service("ALTextToSpeech")

            print(f"[ROBOT] Connecté à Pepper {pepper_ip}")

        except ImportError:
            print("[ROBOT] NAOqi non disponible")
        except Exception as e:
            print(f"[ROBOT] Erreur connexion: {e}")

    def execute_action(self, action: str):
        # Exécute une action de sécurité.
        actions = [a.strip() for a in action.split(",")]

        for act in actions:
            if act == "led_orange":
                self._set_leds_orange()
            elif act == "led_red":
                self._set_leds_red()
            elif act == "led_normal":
                self._set_leds_normal()
            elif act == "gesture_stop":
                self._gesture_stop()
            elif act == "gesture_shrug":
                self._gesture_shrug()

    def _set_leds_orange(self):
        # Active les LEDs en orange (avertissement).
        if self._leds:
            try:
                self._leds.fadeRGB("FaceLeds", 1.0, 0.5, 0.0, 0.5)  # Orange
            except:
                pass
        print("[ROBOT] LEDs → Orange")

    def _set_leds_red(self):
        # Active les LEDs en rouge (alerte).
        if self._leds:
            try:
                self._leds.fadeRGB("FaceLeds", 1.0, 0.0, 0.0, 0.5)  # Rouge
            except:
                pass
        print("[ROBOT] LEDs → Rouge")

    def _set_leds_normal(self):
        # Remet les LEDs en blanc.
        if self._leds:
            try:
                self._leds.fadeRGB("FaceLeds", 1.0, 1.0, 1.0, 0.5)  # Blanc
            except:
                pass
        print("[ROBOT] LEDs → Normal")

    def _gesture_stop(self):
        # Geste de stop (main levée).
        if self._motion:
            try:
                # Lever le bras droit
                names = ["RShoulderPitch", "RShoulderRoll", "RElbowYaw", "RElbowRoll", "RWristYaw"]
                angles = [-0.5, -0.3, 1.0, 0.5, 0.0]
                times = [1.0, 1.0, 1.0, 1.0, 1.0]
                self._motion.angleInterpolation(names, angles, times, True)
            except:
                pass
        print("[ROBOT] Geste → Stop")

    def _gesture_shrug(self):
        # Geste d'incompréhension (hausse épaules).
        if self._motion:
            try:
                # Hausser les épaules
                names = ["LShoulderPitch", "RShoulderPitch"]
                angles = [0.2, 0.2]
                times = [0.5, 0.5]
                self._motion.angleInterpolation(names, angles, times, True)
            except:
                pass
        print("[ROBOT] Geste → Haussement épaules")


# TEST

if __name__ == "__main__":
    print("=" * 70)
    print("TEST MODULE SÉCURITÉ - PHASE 7")
    print("=" * 70)

    # Créer module
    security = SecurityModule()

    # Test 1: Vérification EAN
    print("\n[1] TEST DÉTECTION MÉDICAMENT (EAN)")
    print("-" * 50)

    test_eans = [
        ("3400930000014", "Doliprane"),
        ("3400999999999", "Préfixe 3400"),
        ("3282770149272", "Klorane Camomille"),
    ]

    for ean, desc in test_eans:
        alert = security.check_ean(ean)
        status = "⚠️ BLOQUÉ" if alert.triggered else "✓ OK"
        print(f"  {desc} ({ean}): {status}")
        if alert.triggered:
            print(f"    → {alert.response[:50]}...")

    # Test 2: Filtre mots-clés
    print("\n[2] TEST FILTRE MOTS-CLÉS (<1ms)")
    print("-" * 50)

    test_texts = [
        "C'est quoi le prix de ce shampooing ?",
        "J'ai mal à la tête depuis ce matin",
        "Est-ce que mon chien peut utiliser ce shampooing ?",
        "Peut-on boire ce shampooing ?",
        "Je perds mes cheveux à cause de ma tension artérielle",
        "C'est adapté pour les femmes enceintes ?",
    ]

    for text in test_texts:
        alert = security.check_text(text)
        status = "⚠️" if alert.triggered else "✓"
        print(f"\n  {status} \"{text[:50]}...\"")
        print(f"    Temps: {alert.check_time_ms:.3f}ms (objectif <1ms)")
        if alert.triggered:
            print(f"    Type: {alert.alert_type.value}")
            print(f"    Mots: {alert.matched_keywords}")
            print(f"    → {alert.response[:60]}...")

    # Test 3: Génération audio
    print("\n[3] TEST GÉNÉRATION AUDIO")
    print("-" * 50)

    generated = security.generate_security_audio()
    print(f"  Fichiers générés: {len(generated)}")
    for key, path in generated.items():
        print(f"    - {key}: {Path(path).name}")

    # Statistiques
    print("\n[4] STATISTIQUES")
    print("-" * 50)

    stats = security.get_stats()
    print(f"  Vérifications: {stats['total_checks']}")
    print(f"  Alertes: {stats['alerts_triggered']}")
    print(f"  Temps moyen: {stats['avg_check_time_ms']:.3f}ms")

    print("\n" + "=" * 70)
