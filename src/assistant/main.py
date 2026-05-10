#!/usr/bin/env python3
# Point d'Entree Principal - Parapharma Assistant

import asyncio
import argparse
import signal
import sys
import os
import socket
import urllib.request
import time
import ipaddress
import re
import unicodedata
from collections import deque
from urllib.parse import urlparse
from pathlib import Path
from typing import Optional, Any, Dict, List, Tuple

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

# Ajouter le repertoire src au path
src_path = Path(__file__).parent.parent
if str(src_path) not in sys.path:
    sys.path.insert(0, str(src_path))

# Charger automatiquement .env si python-dotenv est disponible.
if load_dotenv is not None:
    project_root = Path(__file__).resolve().parents[2]
    load_dotenv(project_root / ".env")
    load_dotenv()


def _configure_tls_ca_bundle() -> None:
    # Force un bundle CA cohérent (certifi) pour OpenAI/HuggingFace/requests.
    force = os.getenv("PEPPER_FORCE_CERTIFI_CA", "1").strip().lower() in {"1", "true", "yes", "on"}
    if not force:
        return
    try:
        import certifi
    except Exception:
        return
    try:
        ca_path = certifi.where()
    except Exception:
        return
    if not ca_path or not Path(ca_path).exists():
        return

    os.environ.setdefault("SSL_CERT_FILE", ca_path)
    os.environ.setdefault("REQUESTS_CA_BUNDLE", ca_path)
    os.environ.setdefault("CURL_CA_BUNDLE", ca_path)


_configure_tls_ca_bundle()

from assistant.config import (
    Config, RunMode, get_config, reset_config,
    get_development_config, get_production_config, get_simulation_config
)
from assistant.logger import SystemLogger, LogEvent, get_logger


class PepperAssistant:
    # Assistant Parapharmacie Robotique - Systeme Integre.

    def __init__(self, config: Config, tablet_url_override: str = ""):
        # Initialise l'objet.
        self.config = config
        self.logger = SystemLogger(
            log_directory=config.logging.log_directory,
            file_prefix=config.logging.log_file_prefix,
            console_level=config.logging.console_level,
            file_level=config.logging.file_level
        )

        # Modules (initialises dans setup())
        self.adapter = None
        self.orchestrator = None
        self.openai_client = None
        self.http_fallback_client = None
        self.voice_fallback = None
        self.vision_pipeline = None
        self.database = None
        self.security_module = None
        self.tablet_server = None
        self._tablet_product_cls = None
        self._tablet_top3_cls = None

        # Etat
        self._running = False
        self._tasks = []
        self._shutdown_event = asyncio.Event()
        self._main_loop = None
        self._tablet_url_override = (tablet_url_override or "").strip()
        self._scan_lock = asyncio.Lock()
        disable_vlm_env = os.getenv("PEPPER_VLM_DISABLED", "").strip().lower()
        self._vlm_disabled = disable_vlm_env in {"1", "true", "yes", "on"}
        self._vlm_runtime_failures = 0
        text_q_env = os.getenv("TABLET_TEXT_QUESTION_ENABLED", "0").strip().lower()
        self._tablet_text_question_enabled = text_q_env in {"1", "true", "yes", "on"}
        self._voice_request_seq = 0
        self._voice_transcript_seq = 0
        self._voice_status_seq = 0
        self._realtime_tablet_callbacks_registered = False
        self._voice_use_product_context = True
        self._scan_dump_logged = False
        self._voice_trace = os.getenv("PEPPER_VOICE_TRACE", "1").strip().lower() in {"1", "true", "yes", "on"}

    async def setup(self):
        # Initialise tous les modules.
        self.logger.log_event(LogEvent.SYSTEM_START, {
            "mode": self.config.mode.value,
            "production": self.config.PRODUCTION_MODE
        })

        self.logger.log_info(f"Initialisation en mode {self.config.mode.value}...")

        await self._setup_adapter()

        await self._setup_database()

        await self._setup_security()

        await self._setup_vision()

        await self._setup_audio()

        await self._setup_tablet()

        await self._setup_orchestrator()

        await self._setup_voice_fallback()

        await self._setup_tablet_handlers()

        self.logger.log_event(LogEvent.CONFIG_LOADED, {
            "modules_loaded": self._get_loaded_modules()
        })

        self.logger.log_info("Tous les modules initialises")

    async def _setup_adapter(self):
        # Configure l'adaptateur robot.
        self.logger.log_info("Chargement adaptateur robot...")

        try:
            from assistant.adapters import get_adapter

            self.adapter = get_adapter(self.config)
            adapter_type = type(self.adapter).__name__

            if self.config.mode != RunMode.SIMULATION:
                if self.adapter.connect():
                    self.logger.log_info(f"  Adaptateur: {adapter_type} (connecte)")
                    audio_fmt = self._get_adapter_audio_format()
                    control_mode = os.getenv("PEPPER_CONTROL_MODE", "").strip().lower()
                    if not control_mode:
                        control_mode = "choregraphe" if adapter_type == "ChoregrapheAdapter" else "auto"
                    self.logger.log_info(
                        "  Format capteurs: "
                        f"{audio_fmt.get('sample_rate')}Hz/{audio_fmt.get('channels')}ch "
                        f"(control_mode={control_mode})"
                    )
                else:
                    self.logger.log_warning(f"  Adaptateur: {adapter_type} (echec connexion)")
            else:
                self.adapter.connect()
                self.logger.log_info(f"  Adaptateur: {adapter_type} (simulation)")

        except Exception as e:
            self.logger.log_error("Erreur chargement adaptateur", exception=e)

    async def _setup_database(self):
        # Configure le module base de donnees.
        self.logger.log_info("Chargement base de donnees...")

        try:
            from assistant.database import ProductDatabase

            db_path = self.config.database.db_path
            if Path(db_path).exists():
                self.database = ProductDatabase(db_path)
                product_count = self.database.count_products()
                self.logger.log_info(f"  Base de donnees: {product_count} produits")
            else:
                self.logger.log_warning(f"  Base de donnees non trouvee: {db_path}")

        except ImportError as e:
            self.logger.log_warning(f"Module database non disponible: {e}")
        except Exception as e:
            self.logger.log_error("Erreur chargement database", exception=e)

    async def _setup_security(self):
        # Configure le module securite.
        self.logger.log_info("Chargement module securite...")

        try:
            from assistant.safety import SecurityModule, SecurityConfig as SecConfig

            sec_config = SecConfig(
                strict_mode=self.config.security.strict_mode
            )
            self.security_module = SecurityModule(
                sec_config,
                blacklist_path=self.config.database.blacklist_path
            )

            self.logger.log_info("  Module securite: OK")

        except ImportError as e:
            self.logger.log_warning(f"Module securite non disponible: {e}")
        except Exception as e:
            self.logger.log_error("Erreur chargement securite", exception=e)

    async def _setup_vision(self):
        # Configure le module vision.
        if self.config.mode == RunMode.SIMULATION:
            self.logger.log_info("Vision: mode simulation (desactive)")
            return

        self.logger.log_info("Chargement module vision...")

        try:
            from assistant.vision import VisionModule, VisionConfig as VisConfig

            vision_config = VisConfig(
                camera_index=self.config.vision.camera_index,
                vlm_model=self.config.vision.vlm_model,
                confidence_high=self.config.vision.confidence_high,
                confidence_medium=self.config.vision.confidence_medium
            )

            self.vision_pipeline = VisionModule(
                vision_config,
                database_path=self.config.database.db_path
            )
            self.logger.log_info(f"  Vision: {self.config.vision.vlm_model}")
            try:
                pipeline = getattr(self.vision_pipeline, "pipeline", None)
                detector = getattr(pipeline, "barcode_detector", None) if pipeline else None
                vlm = getattr(pipeline, "vlm", None) if pipeline else None
                pyzbar_ready = bool(getattr(detector, "_pyzbar_available", False))
                openai_barcode_ready = bool(getattr(detector, "_openai_client", None))
                self.logger.log_info(
                    "  Vision status: "
                    f"barcode_pyzbar={'on' if pyzbar_ready else 'off'}, "
                    f"barcode_openai={'on' if openai_barcode_ready else 'off'}, "
                    f"vlm_loaded={'yes' if bool(getattr(vlm, 'is_loaded', False)) else 'no'}"
                )
            except Exception:
                pass
            if self._vlm_disabled:
                self.logger.log_warning(
                    "  Vision: VLM désactivé via PEPPER_VLM_DISABLED, mode scan code-barres prioritaire"
                )
            else:
                eager_load = os.getenv("PEPPER_VLM_EAGER_LOAD", "1").strip().lower() in {"1", "true", "yes", "on"}
                if eager_load and hasattr(self.vision_pipeline, "load"):
                    self.logger.log_info("  Vision: préchargement VLM")
                    try:
                        loaded = await asyncio.to_thread(self.vision_pipeline.load)
                        if loaded:
                            self.logger.log_info("  Vision: préchargement VLM OK")
                            try:
                                pipeline = getattr(self.vision_pipeline, "pipeline", None)
                                vlm = getattr(pipeline, "vlm", None) if pipeline else None
                                backend = str(getattr(vlm, "_backend", "unknown") or "unknown")
                                self.logger.log_info(f"  Vision: backend actif = {backend}")
                            except Exception:
                                pass
                        else:
                            self._vlm_disabled = True
                            self.logger.log_warning(
                                "  Vision: préchargement VLM échoué. Fallback code-barres activé."
                            )
                    except Exception as e:
                        self._vlm_disabled = True
                        self.logger.log_warning(
                            f"  Vision: préchargement VLM indisponible ({e}). Fallback code-barres activé."
                        )

        except ImportError as e:
            self.logger.log_warning(f"Module vision non disponible: {e}")
        except Exception as e:
            self.logger.log_error("Erreur chargement vision", exception=e)

    async def _setup_audio(self):
        # Configure les modules audio (OpenAI + VAD).
        if self.config.mode == RunMode.SIMULATION:
            self.logger.log_info("Audio: mode simulation")
            return

        self.logger.log_info("Chargement module audio...")

        # Préparer le fallback HTTP (même si Realtime est indisponible).
        try:
            from assistant.llm import OpenAIHTTPFallbackClient

            if self.config.openai.api_key:
                fallback_model = (
                    os.getenv("OPENAI_HTTP_FALLBACK_MODEL", "").strip()
                    or self.config.openai.http_fallback_model
                )
                self.http_fallback_client = OpenAIHTTPFallbackClient(
                    api_key=self.config.openai.api_key,
                    model=fallback_model
                )
                self.logger.log_info(f"  OpenAI HTTP fallback: {fallback_model}")
            else:
                self.logger.log_warning("  OpenAI: Pas de cle API configuree")
        except ImportError as e:
            self.logger.log_warning(f"Module fallback HTTP non disponible: {e}")
        except Exception as e:
            self.logger.log_warning(f"  Fallback HTTP non initialisé: {e}")

        # Branche vocale HTTP/Whisper: Realtime volontairement désactivé.
        self.logger.log_info("  OpenAI Realtime: désactivé sur cette branche (HTTP/Whisper uniquement)")
        self.openai_client = None
        return

    def _get_adapter_audio_format(self) -> dict:
        # Format audio actif depuis l'adaptateur (ou configuration par défaut).
        fmt = {
            "sample_rate": int(self.config.audio.input_sample_rate),
            "channels": int(self.config.audio.input_channels),
            "sample_width": 2,
        }

        if not self.adapter:
            return fmt

        getter = None
        if hasattr(self.adapter, "get_audio_active_format"):
            getter = getattr(self.adapter, "get_audio_active_format")
        elif hasattr(self.adapter, "get_audio_capture_format"):
            getter = getattr(self.adapter, "get_audio_capture_format")

        if getter:
            try:
                adapter_fmt = getter() or {}
                if isinstance(adapter_fmt, dict):
                    sr = int(adapter_fmt.get("sample_rate", 0) or 0)
                    ch = int(adapter_fmt.get("channels", 0) or 0)
                    sw = int(adapter_fmt.get("sample_width", 0) or 0)
                    if sr > 0:
                        fmt["sample_rate"] = sr
                    if ch > 0:
                        fmt["channels"] = ch
                    if sw > 0:
                        fmt["sample_width"] = sw
            except Exception:
                pass

        if fmt["sample_rate"] <= 0:
            fmt["sample_rate"] = int(self.config.audio.input_sample_rate)
        if fmt["channels"] <= 0:
            fmt["channels"] = int(self.config.audio.input_channels)

        return fmt

    async def _setup_tablet(self):
        # Configure le serveur tablette.
        self.logger.log_info("Chargement serveur tablette...")

        try:
            # Import depuis le dossier tablet
            tablet_path = Path(__file__).parent.parent.parent / "tablet"
            if tablet_path.exists():
                sys.path.insert(0, str(tablet_path))
                from server import (
                    TabletServer,
                    ServerConfig as TabletServerConfig,
                    Product as TabletProduct,
                    Top3Result as TabletTop3Result,
                )

                server_config = TabletServerConfig(
                    host=self.config.tablet.ws_host,
                    port=self.config.tablet.ws_port
                )
                self.tablet_server = TabletServer(config=server_config)
                self._tablet_product_cls = TabletProduct
                self._tablet_top3_cls = TabletTop3Result

                self.logger.log_info(
                    f"  Tablette WebSocket: ws://{self.config.tablet.ws_host}:"
                    f"{self.config.tablet.ws_port}"
                )
            else:
                self.logger.log_warning("  Dossier tablet/ non trouve")

        except ImportError as e:
            self.logger.log_warning(f"Module tablette non disponible: {e}")
        except Exception as e:
            self.logger.log_error("Erreur chargement tablette", exception=e)

    async def _setup_orchestrator(self):
        # Configure l'orchestrateur.
        self.logger.log_info("Chargement orchestrateur...")

        try:
            from assistant.orchestrator import Orchestrator, OrchestratorConfig as OrchConfig

            orch_config = OrchConfig(
                idle_timeout=self.config.orchestrator.idle_timeout,
                greeting_timeout=self.config.orchestrator.greeting_timeout,
                intent_timeout=self.config.orchestrator.intent_timeout,
                scan_timeout=self.config.orchestrator.scan_timeout,
                confirm_timeout=self.config.orchestrator.confirm_timeout,
                conversation_timeout=self.config.orchestrator.conversation_timeout,
                log_transitions=self.config.orchestrator.log_transitions
            )

            self.orchestrator = Orchestrator(orch_config)

            # Injecter les modules
            if self.database:
                self.orchestrator.set_database_module(self.database)
            if self.security_module:
                self.orchestrator.set_security_module(self.security_module)
            if self.vision_pipeline:
                self.orchestrator.set_vision_module(self.vision_pipeline)
            if self.openai_client:
                self.orchestrator.set_realtime_client(self.openai_client)
            if self.adapter:
                self.orchestrator.set_audio_module(self.adapter)
                self.orchestrator.set_video_module(self.adapter)
                self.orchestrator.set_robot_actions(self.adapter)

            self.logger.log_info("  Orchestrateur: OK")

        except ImportError as e:
            self.logger.log_warning(f"Module orchestrateur non disponible: {e}")
        except Exception as e:
            self.logger.log_error("Erreur chargement orchestrateur", exception=e)

    async def _setup_voice_fallback(self):
        # Configure le fallback vocal HTTP (micro -> transcription -> réponse -> TTS).
        if not self.http_fallback_client or not self.adapter or not self.orchestrator:
            return
        try:
            from assistant.llm import HTTPVoiceFallback, VoiceFallbackConfig

            transcription_model = (
                os.getenv("OPENAI_HTTP_TRANSCRIPTION_MODEL", "").strip()
                or self.config.openai.http_transcription_model
                or "whisper-1"
            )

            detected_fmt = self._get_adapter_audio_format()
            input_sr = int(detected_fmt.get("sample_rate") or self.config.audio.input_sample_rate)
            input_ch = int(detected_fmt.get("channels") or self.config.audio.input_channels)

            mono_channel_index = int(os.getenv("OPENAI_HTTP_MONO_CHANNEL_INDEX", "0") or "0")
            front_only = os.getenv("PEPPER_AUDIO_RECORDER_FRONT_ONLY", "1").strip().lower() in {"1", "true", "yes", "on"}
            if self.adapter.__class__.__name__ == "PepperAdapter" and front_only:
                input_sr = 16000
                input_ch = 1
                mono_channel_index = 0
            if self.adapter.__class__.__name__ == "ChoregrapheAdapter":
                if input_sr <= 0:
                    input_sr = 16000
                if input_ch <= 0:
                    input_ch = 1
            if input_ch <= 1:
                mono_channel_index = 0

            if self.adapter.__class__.__name__ == "ChoregrapheAdapter":
                self.logger.log_info(
                    "  Format fallback audio (pré-démarrage): "
                    f"{input_sr}Hz/{input_ch}ch (choregraphe)"
                )
            else:
                self.logger.log_info(
                    f"  Format fallback audio (pré-démarrage): {input_sr}Hz/{input_ch}ch"
                )

            vf_config = VoiceFallbackConfig(
                input_sample_rate=int(input_sr),
                input_channels=int(input_ch),
                speech_threshold=float(os.getenv("OPENAI_HTTP_SPEECH_THRESHOLD", "0.010") or "0.010"),
                min_utterance_s=float(os.getenv("OPENAI_HTTP_MIN_UTTERANCE_S", "0.35") or "0.35"),
                end_silence_s=float(os.getenv("OPENAI_HTTP_END_SILENCE_S", "0.90") or "0.90"),
                mono_channel_index=mono_channel_index,
                input_gain=float(os.getenv("OPENAI_HTTP_INPUT_GAIN", "1.0") or "1.0"),
                transcription_model=transcription_model,
                manual_trigger=True,
                listen_window_s=9.0,
                manual_buffer_s=float(os.getenv("OPENAI_HTTP_MANUAL_BUFFER_S", "240") or "240"),
                local_stt_enabled=os.getenv("OPENAI_HTTP_LOCAL_STT_ENABLED", "1").strip().lower() in {"1", "true", "yes", "on"},
                local_stt_model=(os.getenv("OPENAI_HTTP_LOCAL_STT_MODEL", "") or "mlx-community/distil-whisper-large-v3").strip(),
                local_stt_path=os.getenv("OPENAI_HTTP_LOCAL_STT_PATH", "").strip(),
                offline_answer_on_error=os.getenv("OPENAI_HTTP_OFFLINE_ANSWER_ON_ERROR", "0").strip().lower() in {"1", "true", "yes", "on"}
            )

            def _speak_blocking(answer_text: str):
                if self.adapter and hasattr(self.adapter, "say"):
                    # Bloquant pour réduire l'auto-capture.
                    self.adapter.say(answer_text, True)

            def _voice_context_provider() -> Dict[str, Any]:
                base_ctx: Dict[str, Any] = {}
                if self.orchestrator:
                    try:
                        base_ctx = self.orchestrator.get_context() or {}
                    except Exception:
                        base_ctx = {}
                if self._voice_use_product_context:
                    return base_ctx
                # Question générique conseil: ne pas biaiser la réponse
                # avec un produit scanné précédemment.
                ctx = dict(base_ctx)
                ctx["current_product"] = ""
                ctx["current_ean"] = ""
                return ctx

            self.voice_fallback = HTTPVoiceFallback(
                api_key=self.config.openai.api_key,
                text_client=self.http_fallback_client,
                speak_callback=_speak_blocking,
                context_provider=_voice_context_provider,
                on_transcript=self._on_voice_fallback_transcript,
                on_answer=self._on_voice_fallback_answer,
                config=vf_config
            )
            self.voice_fallback.start()
            self.orchestrator.set_fallback_audio_handler(self.voice_fallback.ingest)
            self.logger.log_info(
                f"  Fallback vocal HTTP: actif (transcription={transcription_model})"
            )
        except Exception as e:
            self.logger.log_warning(f"  Fallback vocal HTTP non initialisé: {e}")

    def _register_realtime_tablet_callbacks(self):
        # Remonte les états vocaux Realtime vers l'UI tablette.
        if not self.openai_client or self._realtime_tablet_callbacks_registered:
            return
        try:
            self.openai_client.on(
                "on_speech_started",
                lambda: self._push_voice_status_threadsafe(
                    "listening",
                    "Voix détectée. Je vous écoute...",
                    "Question vocale",
                ),
            )
            self.openai_client.on(
                "on_speech_stopped",
                lambda: self._push_voice_status_threadsafe(
                    "processing",
                    "Merci. Je prépare la réponse...",
                    "Question vocale",
                ),
            )
            self.openai_client.on(
                "on_response_start",
                lambda: self._push_voice_status_threadsafe(
                    "processing",
                    "Génération de la réponse en cours...",
                    "Question vocale",
                ),
            )
            self.openai_client.on(
                "on_response_end",
                lambda: self._push_voice_status_threadsafe(
                    "done",
                    "Réponse envoyée.",
                    "Question vocale",
                ),
            )
            self._realtime_tablet_callbacks_registered = True
        except Exception as e:
            self.logger.log_warning(f"  Realtime UI callbacks non initialisés: {e}")

    async def _push_voice_status(
        self,
        status: str,
        message: str,
        title: str = "Question vocale",
        websocket: Any = True,
    ):
        # Publie l'état courant du micro / traitement côté tablette.
        if not self.tablet_server or not hasattr(self.tablet_server, "send_voice_status"):
            return
        try:
            await self.tablet_server.send_voice_status(
                websocket,
                status=status,
                message_text=message,
                title=title,
            )
        except Exception:
            pass

    def _push_voice_status_threadsafe(
        self,
        status: str,
        message: str,
        title: str = "Question vocale",
        websocket: Any = True,
    ):
        # Version thread-safe pour callbacks audio/realtime.
        if not self._main_loop:
            return
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self._push_voice_status(
                    status=status,
                    message=message,
                    title=title,
                    websocket=websocket,
                ),
                self._main_loop
            )
            fut.result(timeout=2.0)
        except Exception:
            pass

    def _on_voice_fallback_transcript(self, transcript: str):
        # Callback thread-safe: transcription utilisateur via fallback vocal.
        if not transcript:
            return
        self._voice_transcript_seq += 1
        text = str(transcript or "").strip()
        if self._voice_trace and text:
            preview = text if len(text) <= 180 else (text[:177] + "...")
            self.logger.log_info(
                "  Voice trace: transcription reçue "
                f"(context_mode={'product' if self._voice_use_product_context else 'general'}): {preview}"
            )
        self._push_voice_status_threadsafe(
            "processing",
            "Question reçue. Je prépare la réponse...",
            "Question vocale",
        )
        if self.orchestrator:
            try:
                from assistant.orchestrator import Event
                self.orchestrator.send_event_sync(
                    Event.QUESTION_ASKED,
                    {"text": transcript, "source": "http_voice_fallback"}
                )
            except Exception:
                pass

    def _on_voice_fallback_answer(self, transcript: str, answer: str):
        # Callback thread-safe: publier la réponse sur tablette (si connectée).
        if not self.tablet_server or not self._main_loop:
            return
        if self._voice_trace:
            preview = str(answer or "").strip()
            if len(preview) > 220:
                preview = preview[:217] + "..."
            self.logger.log_info(f"  Voice trace: réponse OpenAI reçue: {preview}")
        recommendations = []
        if not self._voice_use_product_context:
            try:
                recommendations = self._recommend_products_for_question(transcript, limit=5)
            except Exception:
                recommendations = []
        try:
            if hasattr(self.tablet_server, "send_qa_answer"):
                fut = asyncio.run_coroutine_threadsafe(
                    self.tablet_server.send_qa_answer(
                        True,
                        transcript,
                        answer,
                        recommendations=recommendations,
                        context_mode=("product" if self._voice_use_product_context else "general"),
                    ),
                    self._main_loop
                )
                fut.result(timeout=2.0)
            self._push_voice_status_threadsafe(
                "done",
                "Réponse envoyée.",
                "Question vocale",
            )
        except Exception:
            pass

    async def _setup_tablet_handlers(self):
        # Branche les commandes tablette custom.
        if not self.tablet_server:
            return
        try:
            self.tablet_server.register_handler("ask_question", self._handle_tablet_ask_question)
            self.tablet_server.register_handler("get_products", self._handle_tablet_get_products)
            self.tablet_server.register_handler("start_visual_scan", self._handle_tablet_start_visual_scan)
            self.tablet_server.register_handler("start_barcode_scan", self._handle_tablet_start_barcode_scan)
            self.tablet_server.register_handler("confirm_product", self._handle_tablet_confirm_product)
            self.tablet_server.register_handler("start_voice_question", self._handle_tablet_start_voice_question)
            self.tablet_server.register_handler("stop_voice_question", self._handle_tablet_stop_voice_question)
            if self._tablet_text_question_enabled:
                self.logger.log_info(
                    "  Tablette: handlers get_products/ask_question/start_visual_scan/start_barcode_scan/start_voice_question/stop_voice_question actifs"
                )
            else:
                self.logger.log_info(
                    "  Tablette: handlers get_products/start_visual_scan/start_barcode_scan/start_voice_question/stop_voice_question actifs (ask_question désactivé)"
                )
        except Exception as e:
            self.logger.log_warning(f"  Tablette: handlers indisponibles ({e})")

    def _is_realtime_connected(self) -> bool:
        return bool(self.openai_client and self.openai_client.is_connected())

    @staticmethod
    def _extract_image_from_frame(frame_bytes: bytes):
        # Convertit bytes RGB bruts en image PIL.
        if not frame_bytes:
            return None
        data = bytes(frame_bytes)
        if len(data) < 3 or len(data) % 3 != 0:
            return None

        pixel_count = len(data) // 3
        known_sizes = (
            (640, 480),
            (320, 240),
            (1280, 960),
            (100, 100),
        )

        width = 0
        height = 0
        for w, h in known_sizes:
            if w * h == pixel_count:
                width, height = w, h
                break

        if not width:
            side = int(pixel_count ** 0.5)
            if side * side == pixel_count:
                width, height = side, side
            else:
                return None

        try:
            from PIL import Image
            return Image.frombytes("RGB", (width, height), data[: width * height * 3])
        except Exception:
            return None

    @staticmethod
    def _normalize_hair_type(value: Any) -> str:
        if isinstance(value, list):
            cleaned = [str(v).strip() for v in value if str(v).strip()]
            return ", ".join(cleaned)
        return str(value or "").strip()

    def _build_tablet_product_payload(self, product: Any, fallback: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        data = {}
        if isinstance(product, dict):
            data = dict(product)
        elif hasattr(product, "to_dict"):
            try:
                data = dict(product.to_dict())
            except Exception:
                data = {}

        fallback = fallback or {}
        ean = str(data.get("ean13") or data.get("ean") or fallback.get("ean") or "").strip()
        name = str(data.get("name") or fallback.get("name") or "Produit inconnu").strip()
        brand = str(data.get("brand") or fallback.get("brand") or "").strip()
        usage = str(data.get("usage") or data.get("instructions") or fallback.get("usage") or "").strip()
        image = str(data.get("photo_url") or data.get("image") or fallback.get("image") or "").strip()

        price_value = data.get("price", fallback.get("price", 0.0))
        try:
            price = float(price_value or 0.0)
        except Exception:
            price = 0.0

        return {
            "ean": ean,
            "name": name,
            "brand": brand,
            "price": price,
            "usage": usage,
            "hair_type": self._normalize_hair_type(data.get("hair_type", fallback.get("hair_type", ""))),
            "image": image,
        }

    @staticmethod
    def _normalize_match_text(value: Any) -> str:
        text = unicodedata.normalize("NFKD", str(value or ""))
        text = "".join(ch for ch in text if not unicodedata.combining(ch))
        text = text.lower()
        return re.sub(r"[^a-z0-9]+", " ", text).strip()

    def _recommend_products_for_question(self, question: str, limit: int = 6) -> List[Dict[str, Any]]:
        # Recommandations locales (base produits) pour affichage cartes côté tablette.
        if not self.database or not hasattr(self.database, "get_all_products"):
            return []

        try:
            products = list(self.database.get_all_products() or [])
        except Exception:
            return []
        if not products:
            return []

        q_text = self._normalize_match_text(question)
        q_tokens = {t for t in q_text.split() if len(t) > 2}

        hint_patterns = {
            "pellicules": ("pellicule", "antipellic", "anti pellic", "dandruff", "demangeaison"),
            "gras": ("gras", "sebum", "seborr", "graisse"),
            "secs": ("sec", "deshydrat", "hydrat", "nourr"),
            "colores": ("color", "meche"),
            "sensibles": ("sensible", "irrite", "reactif", "delicat"),
            "abimes": ("abime", "fragile", "casse", "chute", "repar"),
            "normaux": ("normal", "quotidien", "frequent", "doux"),
        }

        active_hints = set()
        for hint, patterns in hint_patterns.items():
            if any(p in q_text for p in patterns):
                active_hints.add(hint)

        scored: List[Tuple[float, Dict[str, Any]]] = []
        for product in products:
            payload = self._build_tablet_product_payload(product)
            hay = self._normalize_match_text(
                " ".join(
                    [
                        payload.get("name", ""),
                        payload.get("brand", ""),
                        payload.get("usage", ""),
                        payload.get("hair_type", ""),
                        str(getattr(product, "category", "") or ""),
                    ]
                )
            )
            hay_tokens = set(hay.split())

            score = 0.0
            for token in q_tokens:
                if token in hay_tokens:
                    score += 2.0
                elif token in hay:
                    score += 0.6

            for hint in active_hints:
                if hint in hay:
                    score += 3.0

            if score > 0:
                scored.append((score, payload))

        if not scored:
            default_items = [self._build_tablet_product_payload(p) for p in products[: max(1, limit)]]
            return default_items[: max(1, limit)]

        scored.sort(key=lambda item: (-item[0], item[1].get("brand", ""), item[1].get("name", "")))
        unique_by_ean: Dict[str, Dict[str, Any]] = {}
        for _, payload in scored:
            ean = str(payload.get("ean", "") or "").strip()
            key = ean or f"{payload.get('brand','')}::{payload.get('name','')}"
            if key not in unique_by_ean:
                unique_by_ean[key] = payload
            if len(unique_by_ean) >= max(1, limit):
                break
        return list(unique_by_ean.values())

    def _to_tablet_product(self, payload: Dict[str, Any]):
        if not self._tablet_product_cls:
            class _ProductWrapper:
                def __init__(self, data: Dict[str, Any]):
                    self._data = dict(data)

                def to_dict(self) -> Dict[str, Any]:
                    return dict(self._data)

            return _ProductWrapper(payload)
        return self._tablet_product_cls(
            ean=payload.get("ean", ""),
            name=payload.get("name", ""),
            brand=payload.get("brand", ""),
            price=float(payload.get("price", 0.0) or 0.0),
            usage=payload.get("usage", ""),
            hair_type=payload.get("hair_type", ""),
            image=payload.get("image", ""),
        )

    def _to_tablet_top3_result(self, payload: Dict[str, Any]):
        if not self._tablet_top3_cls:
            return payload
        return self._tablet_top3_cls(
            ean=payload.get("ean", ""),
            name=payload.get("name", ""),
            confidence=float(payload.get("confidence", 0.0) or 0.0),
            image=payload.get("image", ""),
        )

    def _sync_current_product_context(self, payload: Optional[Dict[str, Any]]) -> None:
        # Synchronise le produit courant pour QA HTTP fallback + Realtime.
        data = dict(payload or {})
        name = str(data.get("name") or "").strip()
        brand = str(data.get("brand") or "").strip()
        ean = str(data.get("ean") or data.get("ean13") or "").strip()
        product_id = str(data.get("id") or data.get("product_id") or "").strip()
        confidence_raw = data.get("confidence")

        full_name = f"{brand} {name}".strip() if brand else name

        if self.orchestrator and hasattr(self.orchestrator, "set_current_product_context"):
            try:
                self.orchestrator.set_current_product_context(
                    product_name=full_name,
                    ean=ean,
                    product_id=product_id,
                    confidence=confidence_raw if confidence_raw is not None else None,
                )
            except Exception:
                pass

        if self.openai_client and hasattr(self.openai_client, "set_product_context"):
            try:
                self.openai_client.set_product_context(product_name=full_name, ean=ean)
            except Exception:
                pass

    def _get_current_product_payload_from_context(self) -> Optional[Dict[str, Any]]:
        # Résout le produit courant depuis le contexte orchestrateur (EAN/nom).
        ctx: Dict[str, Any] = {}
        if self.orchestrator and hasattr(self.orchestrator, "get_context"):
            try:
                ctx = self.orchestrator.get_context() or {}
            except Exception:
                ctx = {}

        ean = str(ctx.get("current_ean") or "").strip()
        name = str(ctx.get("current_product") or "").strip()

        product = self._find_product_by_ean(ean) if ean else None
        if product is None and name:
            product = self._fuzzy_find_product(name)

        if product is None and not (ean or name):
            return None

        payload = self._build_tablet_product_payload(
            product,
            fallback={"ean": ean, "name": name},
        )
        if not str(payload.get("name") or "").strip():
            return None
        return payload

    async def _fallback_product_reply_without_transcript(self, websocket) -> bool:
        # En contexte produit, renvoie une réponse utile même si la transcription est vide.
        if not self._voice_use_product_context:
            return False

        payload = self._get_current_product_payload_from_context()
        if not payload:
            return False

        brand = str(payload.get("brand") or "").strip()
        name = str(payload.get("name") or "").strip() or "ce produit"
        usage = str(payload.get("usage") or "").strip()
        hair_type = str(payload.get("hair_type") or "").strip()
        product_label = f"{brand} {name}".strip() if brand else name

        details = []
        if hair_type:
            details.append(f"Type de cheveux: {hair_type}.")
        if usage:
            details.append(f"Usage: {usage}")

        answer = (
            f"Je n'ai pas bien capté la question, mais vous consultez actuellement {product_label}. "
            "Reposez votre question à l'oral et je répondrai sur ce shampooing."
        )
        if details:
            answer = f"{answer} {' '.join(details)}"

        try:
            if self.tablet_server and hasattr(self.tablet_server, "send_qa_answer"):
                await self.tablet_server.send_qa_answer(
                    websocket,
                    "",
                    answer,
                    recommendations=[],
                    context_mode="product",
                )
            await self._push_voice_status(
                status="done",
                message="Question non captée. Fiche produit affichée.",
                title="Question vocale",
                websocket=websocket,
            )
            if self.adapter and hasattr(self.adapter, "say"):
                short_tts = (
                    f"Je n'ai pas bien entendu. Vous êtes sur {product_label}. "
                    "Pouvez-vous répéter votre question ?"
                )
                await asyncio.to_thread(self.adapter.say, short_tts, False)
            return True
        except Exception:
            return False

    async def _capture_scan_images(
        self,
        num_frames: int,
        interval_s: float = 0.18,
        scan_label: str = "scan",
        log_frames: bool = False,
    ) -> List[Any]:
        if not self.adapter or not hasattr(self.adapter, "capture_image"):
            return []

        captured: List[Any] = []
        frames = max(1, int(num_frames))
        scan_started_at = time.time()
        dump_dir = os.getenv("PEPPER_BARCODE_DUMP_DIR", "").strip()
        resolution_override_raw = os.getenv("PEPPER_BARCODE_CAMERA_RESOLUTION", "").strip()
        resolution_override = None
        if resolution_override_raw:
            try:
                resolution_override = int(resolution_override_raw)
            except Exception:
                resolution_override = None
        if not dump_dir:
            keep_scans = os.getenv("PEPPER_SCAN_KEEP_IMAGES", "1").strip().lower() in {"1", "true", "yes", "on"}
            if keep_scans:
                dump_dir = "logs/pepper_diagnostics/barcode_dump"
        if dump_dir and not self._scan_dump_logged:
            self.logger.log_info(f"  Scan: sauvegarde images activée dans {dump_dir}")
            self._scan_dump_logged = True

        # Chemin optimisé: si l'adaptateur expose une capture burst, on l'utilise.
        if hasattr(self.adapter, "capture_images"):
            try:
                def _capture_burst():
                    if resolution_override is not None:
                        try:
                            return self.adapter.capture_images(
                                frames,
                                max(0.05, float(interval_s)),
                                resolution=resolution_override,
                            )
                        except TypeError:
                            pass
                    return self.adapter.capture_images(
                        frames,
                        max(0.05, float(interval_s)),
                    )
                raw_frames = await asyncio.to_thread(_capture_burst)
            except Exception:
                raw_frames = []

            for idx, raw in enumerate(raw_frames or []):
                image = self._extract_image_from_frame(raw)
                if image is not None:
                    captured.append(image)
                    if log_frames:
                        self.logger.log_info(
                            f"  {scan_label}: photo {idx + 1}/{frames} capturée ({image.width}x{image.height})"
                        )
                    self._dump_scan_image_if_enabled(image, dump_dir, scan_label, idx + 1)
                elif log_frames:
                    raw_size = len(raw) if isinstance(raw, (bytes, bytearray)) else 0
                    self.logger.log_warning(
                        f"  {scan_label}: frame {idx + 1}/{frames} invalide ({raw_size} octets)"
                    )

            if log_frames:
                elapsed = time.time() - scan_started_at
                self.logger.log_info(
                    f"  {scan_label}: burst terminé en {elapsed:.2f}s ({len(captured)}/{frames} images valides)"
                )
                if resolution_override is not None:
                    self.logger.log_info(
                        f"  {scan_label}: résolution caméra forcée={resolution_override}"
                    )
            return captured

        for idx in range(frames):
            raw = await asyncio.to_thread(self.adapter.capture_image)
            image = self._extract_image_from_frame(raw)
            if image is not None:
                captured.append(image)
                if log_frames:
                    self.logger.log_info(
                        f"  {scan_label}: photo {idx + 1}/{frames} capturée ({image.width}x{image.height})"
                    )
                self._dump_scan_image_if_enabled(image, dump_dir, scan_label, idx + 1)
            elif log_frames:
                raw_size = len(raw) if isinstance(raw, (bytes, bytearray)) else 0
                if raw_size:
                    self.logger.log_warning(
                        f"  {scan_label}: frame {idx + 1}/{frames} reçue ({raw_size} octets) "
                        "mais format image non reconnu"
                    )
                else:
                    self.logger.log_warning(f"  {scan_label}: échec capture photo {idx + 1}/{frames}")
            if idx < frames - 1:
                await asyncio.sleep(max(0.05, float(interval_s)))

        if log_frames:
            elapsed = time.time() - scan_started_at
            self.logger.log_info(
                f"  {scan_label}: capture terminée en {elapsed:.2f}s ({len(captured)}/{frames} images valides)"
            )
        return captured

    def _dump_scan_image_if_enabled(self, image: Any, dump_dir: str, scan_label: str, idx: int):
        # Sauvegarde optionnelle des images de scan pour debug terrain.
        if not dump_dir:
            return
        try:
            target_dir = Path(dump_dir)
            target_dir.mkdir(parents=True, exist_ok=True)
            safe_label = "".join(ch if ch.isalnum() else "_" for ch in scan_label).strip("_") or "scan"
            ts_ms = int(time.time() * 1000)
            out_path = target_dir / f"{safe_label}_{idx:02d}_{ts_ms}.jpg"
            image.save(out_path, format="JPEG", quality=95)
        except Exception:
            pass

    def _find_product_by_ean(self, ean: str):
        if not self.database or not hasattr(self.database, "get_by_ean"):
            return None
        try:
            return self.database.get_by_ean(ean)
        except Exception:
            return None

    def _find_product_by_id(self, product_id: str):
        if not self.database or not hasattr(self.database, "get_by_id"):
            return None
        try:
            return self.database.get_by_id(product_id)
        except Exception:
            return None

    def _fuzzy_find_product(self, query: str):
        if not query or not self.database or not hasattr(self.database, "search_fuzzy"):
            return None
        try:
            results = self.database.search_fuzzy(query, limit=1, min_score=0.35)
            if not results:
                return None
            first = results[0]
            return first.product if hasattr(first, "product") else first
        except Exception:
            return None

    async def _set_scan_led(self, color: Tuple[int, int, int]):
        # Met à jour les LEDs Pepper pour indiquer l'état du scan.
        if not self.adapter or not hasattr(self.adapter, "set_led_rgb"):
            return
        try:
            r, g, b = color
            await asyncio.to_thread(self.adapter.set_led_rgb, int(r), int(g), int(b), True)
        except Exception:
            pass

    async def _say_scan_status(self, text: str):
        # Message vocal court pour rendre le scan perceptible.
        if not text or not self.adapter or not hasattr(self.adapter, "say"):
            return
        try:
            await asyncio.to_thread(self.adapter.say, text, False)
        except Exception:
            pass

    async def _detect_barcode_from_images(self, images: List[Any]) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        # Détection code-barres directe (sans dépendre du VLM).
        detector = getattr(getattr(self.vision_pipeline, "pipeline", None), "barcode_detector", None)
        if not detector or not hasattr(detector, "detect_in_images"):
            self.logger.log_warning("  Scan code-barres: détecteur indisponible (pyzbar/libzbar non chargé)")
            return False, "", None

        try:
            barcode = await asyncio.to_thread(detector.detect_in_images, images)
        except Exception as e:
            self.logger.log_warning(f"  Scan code-barres: erreur détecteur ({e})")
            barcode = None

        if not barcode or not getattr(barcode, "ean13", None):
            self.logger.log_info(
                f"  Scan code-barres: aucun EAN trouvé sur {len(images)} image(s)"
            )
            return False, "", None

        ean = str(barcode.ean13 or "").strip()
        confidence = float(getattr(barcode, "confidence", 0.0) or 0.0)
        scan_source = str(getattr(detector, "last_source", "") or "barcode")
        if scan_source == "openai_vision":
            self.logger.log_info(f"  Scan code-barres: EAN détecté via OpenAI Vision ({ean})")
        elif scan_source == "pyzbar":
            self.logger.log_info(f"  Scan code-barres: EAN détecté via pyzbar ({ean})")
        product = self._find_product_by_ean(ean)
        require_in_db = os.getenv("PEPPER_BARCODE_REQUIRE_PRODUCT_IN_DB", "1").strip().lower() in {
            "1", "true", "yes", "on"
        }
        if require_in_db and not product:
            self.logger.log_warning(
                f"  Scan code-barres: EAN {ean} détecté mais absent de la base produits, nouvelle tentative."
            )
            return False, "", None
        payload = self._build_tablet_product_payload(product, fallback={"ean": ean})
        payload["scan_confidence"] = confidence
        payload["scan_source"] = scan_source
        return True, ean, payload

    def _build_barcode_payload_from_ean(
        self,
        ean: str,
        scan_source: str = "barcode",
        confidence: float = 0.0,
    ) -> Tuple[bool, Optional[Dict[str, Any]]]:
        # Construit le payload tablette à partir d'un EAN brut détecté.
        ean_value = str(ean or "").strip()
        if not ean_value:
            return False, None
        product = self._find_product_by_ean(ean_value)
        require_in_db = os.getenv("PEPPER_BARCODE_REQUIRE_PRODUCT_IN_DB", "1").strip().lower() in {
            "1", "true", "yes", "on"
        }
        if require_in_db and not product:
            self.logger.log_warning(
                f"  Scan code-barres: EAN {ean_value} détecté mais absent de la base produits, nouvelle tentative."
            )
            return False, None

        payload = self._build_tablet_product_payload(product, fallback={"ean": ean_value})
        payload["scan_confidence"] = float(confidence or 0.0)
        payload["scan_source"] = scan_source
        return True, payload

    @staticmethod
    def _is_vlm_unavailable_message(message: str) -> bool:
        text = (message or "").lower()
        markers = (
            "chargement du modèle vlm échoué",
            "huggingface",
            "ssl",
            "handshake",
            "httpsconnectionpool",
        )
        return any(marker in text for marker in markers)

    def _resolve_scan_attempts(
        self,
        env_key: str,
        default_attempts: int,
        scan_name: str,
        min_attempts: int = 1,
    ) -> Tuple[bool, int]:
        # Résout le nombre de tentatives de scan avec garde-fou anti-boucle infinie.
        raw = int(os.getenv(env_key, str(default_attempts)) or default_attempts)
        allow_unlimited = os.getenv("PEPPER_SCAN_ALLOW_UNLIMITED", "0").strip().lower() in {
            "1", "true", "yes", "on"
        }
        min_attempts = max(1, int(min_attempts or 1))
        if raw > 0:
            if raw < min_attempts:
                self.logger.log_warning(
                    f"  {scan_name}: {env_key}={raw} trop faible; "
                    f"utilisation de {min_attempts} tentatives minimum."
                )
                return False, min_attempts
            return False, raw
        if allow_unlimited:
            self.logger.log_warning(
                f"  {scan_name}: {env_key}={raw} -> tentatives illimitées activées par PEPPER_SCAN_ALLOW_UNLIMITED"
            )
            return True, 0

        safe_default = max(min_attempts, int(default_attempts or 1))
        self.logger.log_warning(
            f"  {scan_name}: {env_key}={raw} ignoré pour éviter un scan infini. "
            f"Utilisation de {safe_default} tentatives."
        )
        return False, safe_default

    def _get_scan_operation_timeout_s(self) -> float:
        # Timeout global optionnel pour une opération de scan tablette.
        # 0 ou négatif => désactivé (comportement par défaut).
        try:
            timeout_s = float(os.getenv("PEPPER_SCAN_OPERATION_TIMEOUT_S", "0") or 0.0)
        except Exception:
            timeout_s = 0.0
        if timeout_s <= 0:
            return 0.0
        return max(15.0, min(1800.0, timeout_s))

    async def _perform_visual_scan(self) -> Tuple[str, Any]:
        unlimited, attempts = self._resolve_scan_attempts(
            env_key="PEPPER_VISUAL_ATTEMPTS",
            default_attempts=6,
            scan_name="Scan visuel",
            min_attempts=int(os.getenv("PEPPER_VISUAL_MIN_ATTEMPTS", "3") or 3),
        )
        base_frames = max(3, int(getattr(self.config.vision, "num_frames", 3) or 3))
        frames_per_attempt = max(
            1,
            int(os.getenv("PEPPER_VISUAL_FRAMES", str(base_frames)) or base_frames)
        )
        frame_interval_s = max(0.05, float(os.getenv("PEPPER_VISUAL_FRAME_INTERVAL", "0.18") or 0.18))
        between_attempt_s = max(0.05, float(os.getenv("PEPPER_VISUAL_ATTEMPT_COOLDOWN", "0.2") or 0.2))
        if unlimited:
            self.logger.log_info(
                "  Scan visuel: démarrage (tentatives illimitées, "
                f"{frames_per_attempt} photos/tentative)"
            )
        else:
            self.logger.log_info(
                "  Scan visuel: démarrage "
                f"({attempts} tentatives, {frames_per_attempt} photos/tentative)"
            )

        attempt = 0
        while True:
            attempt += 1
            if not unlimited and attempt > attempts:
                self.logger.log_warning("  Scan visuel: échec après toutes les tentatives")
                return "barcode", "Le produit n'a pas pu être reconnu visuellement."

            label_suffix = f"{attempt}/∞" if unlimited else f"{attempt}/{attempts}"
            scan_label = f"Scan visuel tentative {label_suffix}"
            self.logger.log_info(f"  {scan_label}: capture en cours")
            mode, payload = await self._perform_visual_scan_once(
                num_frames=frames_per_attempt,
                interval_s=frame_interval_s,
                scan_label=scan_label,
            )
            if mode in {"product", "top3"}:
                return mode, payload
            if mode == "error":
                # Erreurs transitoires caméra/VLM: retenter au lieu de basculer immédiatement.
                self.logger.log_warning(f"  {scan_label}: erreur transitoire, nouvelle tentative ({payload})")
                if unlimited or attempt < attempts:
                    await asyncio.sleep(between_attempt_s)
                    continue
                return mode, payload

            # mode "barcode": on retente d'abord le visuel (si multi-tentatives),
            # puis on bascule en code-barres une fois la boucle terminée.
            if unlimited:
                await asyncio.sleep(between_attempt_s)
                continue
            if attempt < attempts:
                await asyncio.sleep(between_attempt_s)
                continue

            return mode, payload

    async def _perform_visual_scan_once(
        self,
        num_frames: int,
        interval_s: float,
        scan_label: str,
    ) -> Tuple[str, Any]:
        if not self.vision_pipeline:
            return "error", "Le module vision n'est pas disponible."

        images = await self._capture_scan_images(
            num_frames=max(1, int(num_frames)),
            interval_s=interval_s,
            scan_label=scan_label,
            log_frames=True,
        )
        if not images:
            return "error", "Aucune image capturée. Vérifiez la caméra Pepper."

        if self._vlm_disabled:
            ok, _, payload = await self._detect_barcode_from_images(images)
            if ok and payload:
                return "product", payload
            return "barcode", (
                "Le scan visuel est indisponible pour le moment. "
                "Passez en scan code-barres."
            )

        try:
            result = await asyncio.to_thread(self.vision_pipeline.identify_product, images)
        except Exception as e:
            self._vlm_runtime_failures += 1
            self.logger.log_warning(
                f"  {scan_label}: erreur VLM ({e}), échec #{self._vlm_runtime_failures}"
            )
            if (
                self._is_vlm_unavailable_message(str(e))
                and self._vlm_runtime_failures >= 2
                and not self._vlm_disabled
            ):
                self._vlm_disabled = True
                self.logger.log_warning(
                    "  Vision: trop d'échecs VLM runtime. Bascule en mode code-barres prioritaire."
                )
            ok, _, payload = await self._detect_barcode_from_images(images)
            if ok and payload:
                return "product", payload
            return "barcode", f"Erreur scan visuel: {e}"

        raw = getattr(result, "raw_result", None)
        if not result or not getattr(result, "success", False):
            self._vlm_runtime_failures += 1
            message = getattr(result, "message", "") or "Le produit n'a pas pu être identifié."
            if self._is_vlm_unavailable_message(message):
                if not self._vlm_disabled:
                    self._vlm_disabled = True
                    self.logger.log_warning(
                        "  Vision: VLM indisponible (SSL/HuggingFace). "
                        "Bascule automatique en mode code-barres."
                    )
                ok, _, payload = await self._detect_barcode_from_images(images)
                if ok and payload:
                    return "product", payload
                return "barcode", (
                    "Le modèle visuel n'est pas disponible actuellement. "
                    "Passez en scan code-barres."
                )
            return "error", message
        self._vlm_runtime_failures = 0

        try:
            raw_source = (
                getattr(getattr(raw, "source", None), "value", "")
                if raw else ""
            ) or "unknown"
            top_name = ""
            top_conf = 0.0
            top_prediction = getattr(result, "top_prediction", None)
            if top_prediction:
                top_name = str(getattr(top_prediction, "name", "") or "")
                top_conf = float(getattr(top_prediction, "confidence", 0.0) or 0.0)
            self.logger.log_info(
                "  Scan visuel: résultat VLM "
                f"(source={raw_source}, top='{top_name}', conf={top_conf:.2f}, "
                f"success={bool(getattr(result, 'success', False))})"
            )
        except Exception:
            pass

        if raw and getattr(raw, "source", None) and getattr(raw.source, "value", "") == "vlm_medium":
            top3_payloads = []
            for pred in (getattr(result, "predictions", []) or [])[:3]:
                product = None
                pred_id = str(getattr(pred, "product_id", "") or "").strip()
                pred_name = str(getattr(pred, "name", "") or "").strip()
                if pred_id:
                    product = self._find_product_by_id(pred_id)
                if product is None and pred_name:
                    product = self._fuzzy_find_product(pred_name)
                payload = self._build_tablet_product_payload(
                    product,
                    fallback={
                        "name": pred_name,
                        "brand": str(getattr(pred, "brand", "") or "").strip(),
                        "image": "",
                    }
                )
                payload["confidence"] = float(getattr(pred, "confidence", 0.0) or 0.0)
                top3_payloads.append(payload)

            if top3_payloads:
                self.logger.log_info(
                    "  Scan visuel: top3 candidats = "
                    + ", ".join(
                        f"{str(item.get('name', '') or '').strip()} ({float(item.get('confidence', 0.0) or 0.0):.2f})"
                        for item in top3_payloads[:3]
                    )
                )
                return "top3", top3_payloads

        ean = ""
        fallback_info: Dict[str, Any] = {}
        barcode_result = getattr(raw, "barcode_result", None) if raw else None
        if barcode_result and getattr(barcode_result, "ean13", None):
            ean = str(barcode_result.ean13 or "").strip()
            fallback_info["ean"] = ean

        top_prediction = getattr(result, "top_prediction", None)
        if top_prediction:
            fallback_info["name"] = str(getattr(top_prediction, "name", "") or "")
            fallback_info["brand"] = str(getattr(top_prediction, "brand", "") or "")

        product = self._find_product_by_ean(ean) if ean else None
        if product is None:
            pred_id = str(getattr(top_prediction, "product_id", "") or "").strip() if top_prediction else ""
            if pred_id:
                product = self._find_product_by_id(pred_id)
        if product is None and fallback_info.get("name"):
            product = self._fuzzy_find_product(fallback_info.get("name", ""))

        if product is None and not ean:
            message = getattr(result, "message", "") or "Produit non reconnu visuellement."
            return "barcode", message

        payload = self._build_tablet_product_payload(product, fallback=fallback_info)
        if not payload.get("name"):
            return "error", "Produit détecté mais introuvable en base."

        return "product", payload

    async def _perform_barcode_scan(self) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        if not self.vision_pipeline:
            return False, "", None

        stream_mode = os.getenv("PEPPER_BARCODE_STREAM_MODE", "1").strip().lower() in {
            "1", "true", "yes", "on"
        }
        if stream_mode:
            return await self._perform_barcode_scan_stream()

        unlimited, attempts = self._resolve_scan_attempts(
            env_key="PEPPER_BARCODE_ATTEMPTS",
            default_attempts=20,
            scan_name="Scan code-barres",
            min_attempts=int(os.getenv("PEPPER_BARCODE_MIN_ATTEMPTS", "3") or 3),
        )
        frames_per_attempt = max(1, int(os.getenv("PEPPER_BARCODE_FRAMES", "4") or 4))
        frame_interval_s = max(0.05, float(os.getenv("PEPPER_BARCODE_FRAME_INTERVAL", "0.14") or 0.14))
        between_attempt_s = max(0.05, float(os.getenv("PEPPER_BARCODE_ATTEMPT_COOLDOWN", "0.2") or 0.2))

        if unlimited:
            self.logger.log_info(
                "  Scan code-barres: démarrage (tentatives illimitées, "
                f"{frames_per_attempt} photos/tentative)"
            )
        else:
            self.logger.log_info(
                "  Scan code-barres: démarrage "
                f"({attempts} tentatives, {frames_per_attempt} photos/tentative)"
            )

        attempt = 0
        while True:
            attempt += 1
            if not unlimited and attempt > attempts:
                break

            label_suffix = f"{attempt}/∞" if unlimited else f"{attempt}/{attempts}"
            scan_label = f"Scan code-barres tentative {label_suffix}"
            self.logger.log_info(f"  {scan_label}: capture en cours")
            images = await self._capture_scan_images(
                num_frames=frames_per_attempt,
                interval_s=frame_interval_s,
                scan_label=scan_label,
                log_frames=True,
            )
            if not images:
                self.logger.log_warning(f"  {scan_label}: aucune image exploitable")
                await asyncio.sleep(between_attempt_s)
                continue

            self.logger.log_info(f"  {scan_label}: {len(images)} image(s) analysée(s)")
            ok, ean, payload = await self._detect_barcode_from_images(images)
            if ok:
                confidence = ""
                if isinstance(payload, dict) and "scan_confidence" in payload:
                    try:
                        confidence = f", confiance {float(payload['scan_confidence']):.2f}"
                    except Exception:
                        confidence = ""
                self.logger.log_info(f"  {scan_label}: succès, EAN détecté {ean}{confidence}")
                return True, ean, payload

            self.logger.log_info(f"  {scan_label}: aucun code détecté")
            await asyncio.sleep(between_attempt_s)

        self.logger.log_warning("  Scan code-barres: échec, aucun EAN détecté après toutes les tentatives")
        return False, "", None

    async def _perform_barcode_scan_stream(self) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        # Mode quasi-stream: analyse continue frame par frame.
        if not self.vision_pipeline:
            return False, "", None

        base_attempts = max(1, int(os.getenv("PEPPER_BARCODE_ATTEMPTS", "20") or 20))
        base_frames = max(1, int(os.getenv("PEPPER_BARCODE_FRAMES", "4") or 4))
        default_stream_frames = max(40, base_attempts * base_frames)
        unlimited, max_frames = self._resolve_scan_attempts(
            env_key="PEPPER_BARCODE_STREAM_FRAMES",
            default_attempts=default_stream_frames,
            scan_name="Scan code-barres stream",
            min_attempts=max(20, int(os.getenv("PEPPER_BARCODE_MIN_ATTEMPTS", "3") or 3)),
        )

        frame_interval_s = max(
            0.05,
            float(os.getenv("PEPPER_BARCODE_STREAM_FRAME_INTERVAL", "0.08") or 0.08),
        )
        window_size = max(1, int(os.getenv("PEPPER_BARCODE_STREAM_WINDOW", "3") or 3))
        deep_check_every = max(
            1,
            int(os.getenv("PEPPER_BARCODE_STREAM_DEEP_CHECK_EVERY", "4") or 4),
        )
        log_every = max(1, int(os.getenv("PEPPER_BARCODE_STREAM_LOG_EVERY", "5") or 5))

        detector = getattr(getattr(self.vision_pipeline, "pipeline", None), "barcode_detector", None)
        rolling_images = deque(maxlen=window_size)

        if unlimited:
            self.logger.log_info(
                "  Scan code-barres stream: démarrage (frames illimitées, "
                f"fenêtre={window_size}, vérif approfondie toutes les {deep_check_every} frames)"
            )
        else:
            self.logger.log_info(
                "  Scan code-barres stream: démarrage "
                f"({max_frames} frames max, fenêtre={window_size}, "
                f"vérif approfondie toutes les {deep_check_every} frames)"
            )

        frame_idx = 0
        while True:
            frame_idx += 1
            if not unlimited and frame_idx > max_frames:
                break

            label_suffix = f"{frame_idx}/∞" if unlimited else f"{frame_idx}/{max_frames}"
            scan_label = f"Scan code-barres stream frame {label_suffix}"
            images = await self._capture_scan_images(
                num_frames=1,
                interval_s=frame_interval_s,
                scan_label=scan_label,
                log_frames=False,
            )

            if not images:
                if frame_idx % log_every == 0:
                    self.logger.log_info(f"  {scan_label}: aucune image exploitable")
                await asyncio.sleep(frame_interval_s)
                continue

            image = images[0]
            rolling_images.append(image)
            if frame_idx <= 3 or frame_idx % log_every == 0:
                self.logger.log_info(
                    f"  {scan_label}: image reçue ({image.width}x{image.height}), "
                    f"fenêtre={len(rolling_images)}"
                )

            # 1) Détection rapide locale (pyzbar) sur la frame courante.
            fast_eans: List[str] = []
            if detector and hasattr(detector, "detect_single"):
                try:
                    fast_eans = await asyncio.to_thread(detector.detect_single, image)
                except Exception:
                    fast_eans = []

            for ean in fast_eans:
                ok_payload, payload = self._build_barcode_payload_from_ean(
                    ean=ean,
                    scan_source="pyzbar_stream",
                    confidence=0.95,
                )
                if ok_payload:
                    self.logger.log_info(f"  {scan_label}: succès rapide, EAN détecté {ean}")
                    return True, str(ean), payload

            # 2) Vérification approfondie périodique (OpenAI Vision + pyzbar multi-images).
            if frame_idx % deep_check_every == 0:
                ok, ean, payload = await self._detect_barcode_from_images(list(rolling_images))
                if ok:
                    self.logger.log_info(f"  {scan_label}: succès approfondi, EAN détecté {ean}")
                    return True, ean, payload

            await asyncio.sleep(frame_interval_s)

        self.logger.log_warning("  Scan code-barres stream: échec, aucun EAN détecté après toutes les frames")
        return False, "", None

    async def _run_barcode_scan_sequence(
        self,
        websocket,
        intro_message: str = "",
        show_scan_choice_on_fail: bool = True,
    ) -> bool:
        # Exécute un scan code-barres complet + notifications tablette/robot.
        await self.tablet_server.send_show_screen(websocket, "barcode-scan")
        await self._set_scan_led((255, 140, 0))
        await self._say_scan_status(intro_message or "Je scanne le code barres.")

        ok, ean, payload = await self._perform_barcode_scan()
        if ok:
            await self._set_scan_led((0, 190, 0))
            product_msg = self._to_tablet_product(payload or {"ean": ean})
            if payload and payload.get("name"):
                await self.tablet_server.send_product_identified(websocket, product_msg)
            else:
                await self.tablet_server.send_barcode_detected(websocket, ean, product_msg)
            if payload:
                self._sync_current_product_context(payload)
            else:
                self._sync_current_product_context({"ean": ean})
            self.logger.log_info(f"  Tablette: code-barres détecté ({ean})")
            return True

        await self._set_scan_led((220, 30, 30))
        await self._say_scan_status("Je n'ai pas détecté le code barres.")
        await self.tablet_server.send_barcode_failed(websocket)
        if show_scan_choice_on_fail:
            await self.tablet_server.send_show_screen(websocket, "scan-choice")
        self.logger.log_warning("  Tablette: scan code-barres terminé sans résultat")
        return False

    async def _handle_tablet_start_visual_scan(self, websocket, data):
        # Lance un scan visuel piloté par la tablette.
        if self._scan_lock.locked():
            # Ne pas afficher une erreur bloquante côté tablette: un scan est déjà actif.
            self.logger.log_info("  Tablette: start_visual_scan ignoré (scan déjà en cours)")
            return
        if not self.adapter or not hasattr(self.adapter, "capture_image"):
            await self.tablet_server._send_error(websocket, "Caméra Pepper indisponible.")
            return
        if not self.vision_pipeline:
            await self.tablet_server._send_error(websocket, "Module vision indisponible.")
            return

        async with self._scan_lock:
            try:
                # Réactivité UI: afficher le chargement immédiatement.
                await self.tablet_server.send_show_screen(websocket, "loading")
                self._sync_current_product_context({})
                if self.adapter and hasattr(self.adapter, "freeze_head"):
                    await asyncio.to_thread(self.adapter.freeze_head)
                await self._set_scan_led((140, 0, 255))
                await self._say_scan_status("Je scanne le produit.")

                scan_timeout_s = self._get_scan_operation_timeout_s()
                if scan_timeout_s > 0:
                    mode, payload = await asyncio.wait_for(
                        self._perform_visual_scan(),
                        timeout=scan_timeout_s,
                    )
                else:
                    mode, payload = await self._perform_visual_scan()

                if mode == "product":
                    await self._set_scan_led((0, 190, 0))
                    await self.tablet_server.send_product_identified(
                        websocket,
                        self._to_tablet_product(payload)
                    )
                    self._sync_current_product_context(payload)
                elif mode == "top3":
                    await self._set_scan_led((255, 140, 0))
                    top3 = [self._to_tablet_top3_result(item) for item in payload]
                    await self.tablet_server.send_top3_results(websocket, top3)
                elif mode == "error":
                    self.logger.log_warning(f"  Tablette: scan visuel en erreur ({payload})")
                    fallback_on_error = os.getenv("PEPPER_VISUAL_ERROR_FALLBACK_BARCODE", "1").strip().lower() in {
                        "1", "true", "yes", "on"
                    }
                    if fallback_on_error:
                        if scan_timeout_s > 0:
                            await asyncio.wait_for(
                                self._run_barcode_scan_sequence(
                                    websocket,
                                    intro_message="Le scan visuel a échoué. Je passe au scan du code barres.",
                                ),
                                timeout=scan_timeout_s,
                            )
                        else:
                            await self._run_barcode_scan_sequence(
                                websocket,
                                intro_message="Le scan visuel a échoué. Je passe au scan du code barres.",
                            )
                    else:
                        await self._set_scan_led((220, 30, 30))
                        await self._say_scan_status("Le scan visuel a échoué.")
                        await self.tablet_server._send_error(websocket, str(payload))
                        await self.tablet_server.send_show_screen(websocket, "scan-choice")
                elif mode == "barcode":
                    # VLM confiance insuffisante : demander nouvelle capture, pas fallback barcode
                    await self._set_scan_led((220, 30, 30))
                    await self._say_scan_status(
                        "Je n'ai pas pu identifier le produit. "
                        "Rapprochez-le ou orientez-le différemment et réessayez."
                    )
                    await self.tablet_server.send_show_screen(websocket, "scan-choice")
                else:
                    await self.tablet_server.send_show_screen(websocket, "barcode-scan")
            except asyncio.TimeoutError:
                self.logger.log_warning("  Tablette: scan visuel interrompu (timeout de sécurité)")
                await self._set_scan_led((220, 30, 30))
                await self._say_scan_status("Le scan a pris trop de temps.")
                await self.tablet_server._send_error(
                    websocket,
                    "Le scan a pris trop de temps. Réessayez.",
                )
                await self.tablet_server.send_show_screen(websocket, "scan-choice")
            finally:
                if self.adapter and hasattr(self.adapter, "unfreeze_head"):
                    await asyncio.to_thread(self.adapter.unfreeze_head)
                await self._set_scan_led((255, 255, 255))

    async def _handle_tablet_start_barcode_scan(self, websocket, data):
        # Lance un scan code-barres dédié.
        if self._scan_lock.locked():
            # Ne pas afficher une erreur bloquante côté tablette: un scan est déjà actif.
            self.logger.log_info("  Tablette: start_barcode_scan ignoré (scan déjà en cours)")
            return
        if not self.adapter or not hasattr(self.adapter, "capture_image"):
            await self.tablet_server._send_error(websocket, "Caméra Pepper indisponible.")
            return
        if not self.vision_pipeline:
            await self.tablet_server._send_error(websocket, "Module vision indisponible.")
            return

        async with self._scan_lock:
            try:
                self.logger.log_info("  Tablette: start_barcode_scan reçu")
                self._sync_current_product_context({})
                if self.adapter and hasattr(self.adapter, "freeze_head"):
                    await asyncio.to_thread(self.adapter.freeze_head)
                scan_timeout_s = self._get_scan_operation_timeout_s()

                async def _barcode_then_vlm_fallback():
                    barcode_ok = await self._run_barcode_scan_sequence(
                        websocket, show_scan_choice_on_fail=False
                    )
                    if barcode_ok:
                        return
                    # Barcode échoué : fallback automatique vers scan VLM
                    self.logger.log_info("  Tablette: barcode échoué, bascule scan visuel (VLM)")
                    await self._say_scan_status(
                        "Je n'ai pas réussi à lire le code barres. Je passe au scan visuel."
                    )
                    await asyncio.sleep(0.4)
                    await self._set_scan_led((140, 0, 255))
                    mode, payload = await self._perform_visual_scan()
                    if mode == "product":
                        await self._set_scan_led((0, 190, 0))
                        await self.tablet_server.send_product_identified(
                            websocket, self._to_tablet_product(payload)
                        )
                        self._sync_current_product_context(payload)
                    elif mode == "top3":
                        await self._set_scan_led((255, 140, 0))
                        top3 = [self._to_tablet_top3_result(item) for item in payload]
                        await self.tablet_server.send_top3_results(websocket, top3)
                    else:
                        # VLM aussi échoué : demander une nouvelle capture
                        await self._set_scan_led((220, 30, 30))
                        await self._say_scan_status(
                            "Je n'ai pas pu identifier le produit. "
                            "Rapprochez-le ou orientez-le différemment et réessayez."
                        )
                        await self.tablet_server.send_show_screen(websocket, "scan-choice")

                if scan_timeout_s > 0:
                    await asyncio.wait_for(_barcode_then_vlm_fallback(), timeout=scan_timeout_s)
                else:
                    await _barcode_then_vlm_fallback()
            except asyncio.TimeoutError:
                self.logger.log_warning("  Tablette: scan code-barres interrompu (timeout de sécurité)")
                await self._set_scan_led((220, 30, 30))
                await self._say_scan_status("Le scan a pris trop de temps.")
                await self.tablet_server._send_error(
                    websocket,
                    "Le scan a pris trop de temps. Réessayez.",
                )
                await self.tablet_server.send_show_screen(websocket, "scan-choice")
            finally:
                if self.adapter and hasattr(self.adapter, "unfreeze_head"):
                    await asyncio.to_thread(self.adapter.unfreeze_head)
                await self._set_scan_led((255, 255, 255))

    async def _handle_tablet_confirm_product(self, websocket, data):
        # Confirmation d'un produit Top-3 côté tablette.
        ean = str((data or {}).get("ean") or "").strip()
        if not ean:
            await self.tablet_server._send_error(websocket, "EAN manquant pour la confirmation.")
            return

        product = self._find_product_by_ean(ean)
        if not product:
            await self.tablet_server._send_error(websocket, "Produit introuvable en base.")
            return

        payload = self._build_tablet_product_payload(product)
        await self.tablet_server.send_product_identified(
            websocket,
            self._to_tablet_product(payload)
        )
        self._sync_current_product_context(payload)

    async def _handle_tablet_get_products(self, websocket, data):
        # Retourne la liste produits pour l'écran conseil.
        if not self.database or not hasattr(self.database, "get_all_products"):
            await self.tablet_server._send_error(websocket, "Base produits indisponible.")
            return

        payload_data = data or {}
        shampoo_only = bool(payload_data.get("shampoo_only", True))
        try:
            limit = int(payload_data.get("limit", 120))
        except Exception:
            limit = 120
        limit = max(1, min(500, limit))

        try:
            all_products = self.database.get_all_products()
        except Exception as e:
            await self.tablet_server._send_error(websocket, f"Erreur lecture produits: {e}")
            return

        items: List[Dict[str, Any]] = []
        for product in all_products:
            category = str(getattr(product, "category", "") or "").strip().lower()
            is_shampoo = category.startswith("shampoo")
            if shampoo_only and not is_shampoo:
                continue

            payload = self._build_tablet_product_payload(product)
            if not payload.get("image"):
                payload["image"] = "placeholder.svg"
            items.append(payload)

        items.sort(key=lambda p: (str(p.get("brand", "")).lower(), str(p.get("name", "")).lower()))
        await self.tablet_server.send_products_list(websocket, items[:limit])

    async def _handle_tablet_start_voice_question(self, websocket, data):
        # Déclenche une fenêtre d'écoute vocale fallback via bouton tablette.
        payload_data = data or {}
        manual_send = str(payload_data.get("manual_send", "1")).strip().lower() in {"1", "true", "yes", "on"}
        use_product_context = str(payload_data.get("use_product_context", "1")).strip().lower() in {"1", "true", "yes", "on"}
        self._voice_use_product_context = use_product_context

        if self._is_realtime_connected():
            await self._push_voice_status(
                status="listening",
                message="Realtime actif. Parlez maintenant, Pepper vous écoute.",
                title="Question vocale",
                websocket=websocket,
            )
            return

        if not self.voice_fallback:
            await self.tablet_server._send_error(
                websocket,
                "Fallback vocal indisponible (vérifier OpenAI et micro Pepper)."
            )
            return

        duration = float(os.getenv("PEPPER_VOICE_LISTEN_DURATION_S", "12") or 12.0)
        try:
            requested = float(payload_data.get("duration_s", duration))
            duration = max(3.0, min(180.0, requested))
        except Exception:
            pass
        if manual_send:
            # Mode manuel: pas de timeout court, l'utilisateur envoie explicitement.
            duration = max(duration, float(os.getenv("PEPPER_VOICE_MANUAL_MAX_S", "3600") or 3600.0))

        # Réactivité UI: signaler l'écoute avant les appels robot potentiellement lents.
        await self._push_voice_status(
            status="listening",
            message=(
                "Micro activé. Parlez maintenant puis appuyez sur « Envoyer la question »."
                if manual_send else f"Micro activé. Parlez maintenant ({int(duration)} s)."
            ),
            title="Question vocale",
            websocket=websocket,
        )

        # Ajuster dynamiquement le fallback avec le format réellement observé.
        vf_config = getattr(self.voice_fallback, "config", None)
        if vf_config:
            detected = self._get_adapter_audio_format()
            detected_channels = int(detected.get("channels", 0) or 0)
            detected_rate = int(detected.get("sample_rate", 0) or 0)

            if detected_channels > 0 and vf_config.input_channels != detected_channels:
                vf_config.input_channels = detected_channels
            self.logger.log_info(
                f"  Fallback vocal: canaux audio ajustés à {detected_channels}"
            )
            if detected_rate > 0 and vf_config.input_sample_rate != detected_rate:
                vf_config.input_sample_rate = detected_rate
                self.logger.log_info(
                    f"  Fallback vocal: fréquence audio ajustée à {detected_rate} Hz"
                )
            if detected_channels <= 1:
                vf_config.mono_channel_index = 0
            elif vf_config.input_channels > 1 and vf_config.mono_channel_index >= vf_config.input_channels:
                vf_config.mono_channel_index = 0
            self.logger.log_info(
                f"  Fallback vocal: index mono = {vf_config.mono_channel_index}"
            )

        if hasattr(self.voice_fallback, "arm_listen_window"):
            await asyncio.to_thread(self.voice_fallback.arm_listen_window, duration)

        if self.adapter and hasattr(self.adapter, "say"):
            await asyncio.to_thread(self.adapter.say, "Je vous écoute.", False)

        self._voice_request_seq += 1

        self.logger.log_info(
            "  Tablette: question vocale démarrée "
            f"(durée={duration:.1f}s, contexte_produit={'on' if use_product_context else 'off'})"
        )

    async def _handle_tablet_stop_voice_question(self, websocket, data):
        # Arrêt manuel de la fenêtre d'écoute vocale et envoi immédiat.
        if self._is_realtime_connected():
            await self._push_voice_status(
                status="processing",
                message="Question envoyée. Je prépare la réponse...",
                title="Question vocale",
                websocket=websocket,
            )
            return

        if not self.voice_fallback:
            await self.tablet_server._send_error(
                websocket,
                "Fallback vocal indisponible (vérifier OpenAI et micro Pepper)."
            )
            return

        await self._push_voice_status(
            status="processing",
            message="Question envoyée. Analyse en cours...",
            title="Question vocale",
            websocket=websocket,
        )

        transcript_seq_before = self._voice_transcript_seq
        if hasattr(self.voice_fallback, "finalize_listen_window"):
            await asyncio.to_thread(self.voice_fallback.finalize_listen_window)
        elif hasattr(self.voice_fallback, "arm_listen_window"):
            await asyncio.to_thread(self.voice_fallback.arm_listen_window, 0.5)

        # Si aucune transcription n'arrive après l'envoi manuel, prévenir clairement.
        self._voice_request_seq += 1
        request_id = self._voice_request_seq
        asyncio.create_task(
            self._notify_voice_no_transcript_after_stop(
                websocket=websocket,
                request_id=request_id,
                transcript_seq_before=transcript_seq_before,
            )
        )

    async def _notify_voice_no_transcript_after_stop(
        self,
        websocket,
        request_id: int,
        transcript_seq_before: int,
    ):
        # Feedback explicite si "Envoyer la question" est appuyé sans audio exploitable.
        delay_s = max(
            2.2,
            float(os.getenv("PEPPER_VOICE_NO_TRANSCRIPT_DELAY_S", "7.5") or 7.5),
        )
        await asyncio.sleep(delay_s)

        if request_id != self._voice_request_seq:
            return
        if self._voice_transcript_seq > transcript_seq_before:
            return
        if not self.tablet_server:
            return

        if await self._fallback_product_reply_without_transcript(websocket):
            return

        await self._push_voice_status(
            status="timeout",
            message="Aucune question captée. Rapprochez-vous et reparlez, puis appuyez sur envoyer.",
            title="Question vocale",
            websocket=websocket,
        )

    async def _notify_voice_timeout_if_silent(
        self,
        websocket,
        request_id: int,
        duration_s: float,
        transcript_seq_before: int,
    ):
        # Message explicite côté tablette si aucune question n'a été captée.
        await asyncio.sleep(max(1.0, float(duration_s)) + 2.5)

        if request_id != self._voice_request_seq:
            return
        if self._voice_transcript_seq > transcript_seq_before:
            return
        if not self.tablet_server:
            return

        await self._push_voice_status(
            status="processing",
            message="Fenêtre d'écoute terminée. Analyse de votre question...",
            title="Question vocale",
            websocket=websocket,
        )

        try:
            await self._push_voice_status(
                status="timeout",
                message="Je n'ai pas bien entendu. Rapprochez-vous et parlez clairement, puis réessayez.",
                title="Question vocale",
                websocket=websocket,
            )
            if self.adapter and hasattr(self.adapter, "say"):
                await asyncio.to_thread(
                    self.adapter.say,
                    "Je n'ai pas bien entendu. Pouvez-vous répéter, s'il vous plaît ?",
                    False
                )
        except Exception:
            pass

    async def _handle_tablet_ask_question(self, websocket, data):
        # Mode secours: question texte envoyée à OpenAI HTTP puis réponse vocale Pepper.
        if not self._tablet_text_question_enabled:
            await self.tablet_server._send_error(
                websocket,
                "Question texte désactivée. Utilisez le bouton de question vocale."
            )
            return

        question = str((data or {}).get("question") or "").strip()
        if not question:
            await self.tablet_server._send_error(websocket, "Question vide.")
            return

        if not self.http_fallback_client:
            await self.tablet_server._send_error(
                websocket,
                "Module OpenAI HTTP indisponible. Vérifiez la clé API et le réseau."
            )
            return

        self.logger.log_info(f"  Tablette: ask_question reçu ({len(question)} caractères)")
        context = {}
        if self.orchestrator and hasattr(self.orchestrator, "get_context"):
            try:
                context = self.orchestrator.get_context() or {}
            except Exception:
                context = {}

        try:
            answer = await asyncio.to_thread(
                self.http_fallback_client.ask,
                question,
                context
            )
        except Exception as e:
            self.logger.log_warning(f"  ask_question: erreur OpenAI HTTP ({e})")
            answer = ""

        if not answer:
            answer = "Je n'arrive pas à répondre pour le moment. Vérifiez le réseau puis réessayez."

        if self.adapter and hasattr(self.adapter, "say"):
            try:
                await asyncio.to_thread(self.adapter.say, answer, False)
            except Exception:
                pass

        recommendations = self._recommend_products_for_question(question, limit=5)
        if self.tablet_server and hasattr(self.tablet_server, "send_qa_answer"):
            await self.tablet_server.send_qa_answer(
                websocket,
                question,
                answer,
                recommendations=recommendations,
                context_mode="general",
            )

    def _get_loaded_modules(self) -> list:
        # Retourne la liste des modules charges.
        modules = []
        if self.adapter:
            modules.append(f"adapter:{type(self.adapter).__name__}")
        if self.database:
            modules.append("database")
        if self.security_module:
            modules.append("security")
        if self.vision_pipeline:
            modules.append("vision")
        if self.openai_client:
            modules.append("openai")
        if self.http_fallback_client:
            modules.append("openai_http_fallback")
        if self.voice_fallback:
            modules.append("voice_http_fallback")
        if self.tablet_server:
            modules.append("tablet")
        if self.orchestrator:
            modules.append("orchestrator")
        return modules

    async def run(self):
        # Lance le systeme complet.
        self._running = True
        self._main_loop = asyncio.get_running_loop()
        session_id = self.logger.start_session()

        self.logger.log_info("=" * 50)
        self.logger.log_info("ASSISTANT PARAPHARMACIE PEPPER")
        self.logger.log_info(f"   Mode: {self.config.mode.value}")
        self.logger.log_info(f"   Session: {session_id}")
        self.logger.log_info("=" * 50)

        try:
            # Demarrer les taches paralleles
            self._tasks = []

            # Serveur tablette
            if self.tablet_server:
                self._tasks.append(asyncio.create_task(
                    self._run_tablet_server(),
                    name="tablet_server"
                ))

            # Orchestrateur
            if self.orchestrator:
                self._tasks.append(asyncio.create_task(
                    self._run_orchestrator(),
                    name="orchestrator"
                ))

            # Client OpenAI
            if self.openai_client:
                self._tasks.append(asyncio.create_task(
                    self._run_openai_client(),
                    name="openai_client"
                ))

            # Afficher la tablette Pepper automatiquement.
            self._tasks.append(asyncio.create_task(
                self._auto_show_tablet(),
                name="auto_show_tablet"
            ))

            self.logger.log_info(f"Demarrage de {len(self._tasks)} taches paralleles")

            # Attendre l'arret
            await self._shutdown_event.wait()

        except asyncio.CancelledError:
            self.logger.log_info("Arret demande")
        except Exception as e:
            self.logger.log_critical("Erreur critique", exception=e)
        finally:
            await self.shutdown()

    async def _run_tablet_server(self):
        # Lance le serveur tablette.
        try:
            self.logger.log_event(LogEvent.TABLET_CONNECTED, {"status": "starting"})
            await self.tablet_server.start()
        except Exception as e:
            self.logger.log_error("Erreur serveur tablette", exception=e)

    async def _run_orchestrator(self):
        # Lance l'orchestrateur.
        try:
            await self.orchestrator.run()
        except Exception as e:
            self.logger.log_error("Erreur orchestrateur", exception=e)

    async def _run_openai_client(self):
        # Lance le client OpenAI.
        try:
            if self.openai_client.connect():
                self.logger.log_info("Client OpenAI connecte")
                # Boucle de maintien connexion
                while self._running:
                    await asyncio.sleep(1)
            else:
                self.logger.log_warning("Client OpenAI Realtime indisponible")
                if self.voice_fallback:
                    self.logger.log_warning(
                        "Fallback vocal HTTP actif (micro Pepper + transcription HTTP)"
                    )
                elif self.http_fallback_client:
                    self.logger.log_warning(
                        "Fallback HTTP texte disponible mais désactivé côté tablette"
                    )
        except Exception as e:
            self.logger.log_error("Erreur client OpenAI", exception=e)

    def _resolve_local_ip_for_pepper(self) -> str:
        # Déduit l'IP locale utile pour joindre Pepper.
        pepper_ip = ""
        try:
            pepper_ip = str(getattr(self.adapter, "ip", "") or "").strip()
        except Exception:
            pepper_ip = ""
        if not pepper_ip:
            pepper_ip = (self.config.pepper.ip or "").strip()
        if not pepper_ip:
            return "127.0.0.1"
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.connect((pepper_ip, int(self.config.pepper.port or 9559)))
            local_ip = sock.getsockname()[0]
            sock.close()
            if local_ip:
                return local_ip
        except Exception:
            pass
        return "127.0.0.1"

    def _resolve_tablet_url(self) -> str:
        # URL tablette à afficher sur Pepper.
        env_url = (os.getenv("PEPPER_TABLET_URL", "") or os.getenv("TABLET_URL", "")).strip()
        if self._tablet_url_override:
            return self._tablet_url_override
        if env_url:
            return env_url
        local_ip = self._resolve_local_ip_for_pepper()
        return f"http://{local_ip}:8080/index.html"

    @staticmethod
    def _with_cache_buster(url: str) -> str:
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}cb={int(time.time())}"

    async def _auto_show_tablet(self):
        # Tente d'afficher la webview tablette sur Pepper.
        if not self.adapter or not hasattr(self.adapter, "show_on_tablet"):
            return
        if self.config.mode == RunMode.SIMULATION:
            return

        url = self._resolve_tablet_url()
        ws_host = self.config.tablet.ws_host
        ws_port = self.config.tablet.ws_port
        if ws_host in ("0.0.0.0", "", None):
            ws_host = self._resolve_local_ip_for_pepper()
        self.logger.log_info(f"  URL tablette Pepper: {url}")
        self.logger.log_info(f"  WS tablette attendu: ws://{ws_host}:{ws_port}")

        # Diagnostic réseau simple: si Pepper et l'hôte URL sont en IP privées
        # mais dans des sous-réseaux différents, la tablette ne pourra souvent
        # pas atteindre le serveur HTTP du Mac.
        try:
            pepper_ip = str(getattr(self.adapter, "ip", "") or "")
            tablet_host = (urlparse(url).hostname or "").strip()
            if "xxx" in tablet_host.lower():
                self.logger.log_warning(
                    "  URL tablette invalide (placeholder détecté): "
                    f"{tablet_host}. Remplace par une vraie IP (ex: 192.168.13.42)."
                )
            if pepper_ip and tablet_host:
                pepper_addr = ipaddress.ip_address(pepper_ip)
                tablet_addr = ipaddress.ip_address(tablet_host)
                if (
                    pepper_addr.is_private
                    and tablet_addr.is_private
                    and pepper_addr.packed[:3] != tablet_addr.packed[:3]
                ):
                    self.logger.log_warning(
                        "  Réseau potentiellement incompatible: Pepper="
                        f"{pepper_ip}, URL tablette={tablet_host}. "
                        "Mets Pepper et Mac sur le meme réseau local."
                    )
        except Exception:
            pass

        # Vérifier localement que le serveur HTTP tablette est bien lancé.
        try:
            await asyncio.to_thread(urllib.request.urlopen, url, None, 1.5)
        except Exception:
            self.logger.log_warning(
                "  Tablette HTTP non joignable localement. "
                "Lance: cd tablet && python3 -m http.server 8080 --bind 0.0.0.0"
            )

        # Affichage Pepper avec retries courts.
        for attempt in range(1, 4):
            try:
                target_url = self._with_cache_buster(url)
                ok = await asyncio.to_thread(self.adapter.show_on_tablet, target_url)
                if ok:
                    self.logger.log_info(f"  Tablette Pepper affichee (tentative {attempt})")
                    return
            except Exception:
                pass
            await asyncio.sleep(1.0)

        self.logger.log_warning(
            "  Impossible d'afficher la tablette sur Pepper automatiquement."
        )

    async def shutdown(self):
        # Arrete proprement le systeme.
        if not self._running:
            return

        self._running = False
        self.logger.log_info("Arret du systeme...")

        # Annuler toutes les taches
        for task in self._tasks:
            if not task.done():
                task.cancel()
                try:
                    await asyncio.wait_for(task, timeout=5.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass

        # Arreter les modules
        if self.orchestrator:
            try:
                await self.orchestrator.stop()
            except:
                pass

        if self.tablet_server:
            try:
                await self.tablet_server.stop()
            except:
                pass

        if self.openai_client:
            try:
                self.openai_client.disconnect()
            except:
                pass

        if self.voice_fallback:
            try:
                self.voice_fallback.stop()
            except:
                pass
            self.voice_fallback = None

        if self.adapter:
            try:
                self.adapter.disconnect()
            except:
                pass

        # Fermer la base de donnees
        if self.database:
            try:
                self.database.close()
            except:
                pass

        # Finaliser le logging
        stats = self.logger.get_stats()
        self.logger.end_session(stats)

        self.logger.log_event(LogEvent.SYSTEM_STOP, {
            "stats": stats
        })

        self.logger.log_info("Systeme arrete proprement")
        self._main_loop = None
        self.logger.close()

    def request_shutdown(self):
        # Demande l'arret du systeme.
        self._shutdown_event.set()



def setup_signal_handlers(assistant: PepperAssistant):
    # Configure les handlers de signaux.
    def signal_handler(sig, frame):
        # Gere handler.
        print("\nSignal d'arret recu, arret en cours...")
        assistant.request_shutdown()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)


def parse_args():
    # Parse les arguments de ligne de commande.
    parser = argparse.ArgumentParser(
        description="Assistant Parapharmacie Pepper",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemples:
  python -m assistant.main                    # Mode developpement
  python -m assistant.main --simulation       # Mode simulation (sans robot)
  python -m assistant.main --production       # Mode production
  python -m assistant.main --pepper-ip 192.168.1.100  # Connexion a Pepper
        """
    )

    parser.add_argument("--production", action="store_true",
                        help="Mode production")
    parser.add_argument("--simulation", action="store_true",
                        help="Mode simulation (sans materiel)")
    parser.add_argument("--test", action="store_true",
                        help="Mode test (timeouts courts)")
    parser.add_argument("--pepper-ip", type=str, default="",
                        help="Adresse IP du robot Pepper")
    parser.add_argument("--tablet-url", type=str, default="",
                        help="URL web a afficher sur la tablette Pepper (optionnel)")
    parser.add_argument("--config", type=str,
                        help="Fichier de configuration YAML/JSON")
    parser.add_argument("--debug", action="store_true",
                        help="Activer le mode debug")

    return parser.parse_args()


async def main():
    # Point d'entree principal.
    args = parse_args()
    default_config_path = Path(__file__).resolve().parents[2] / "config" / "config.yaml"

    # Determiner la configuration
    if args.config:
        config = Config.load(args.config)
    elif args.production:
        config = get_production_config()
    elif args.simulation:
        config = get_simulation_config()
    elif args.test:
        from assistant.config import get_test_config
        config = get_test_config()
    elif default_config_path.exists():
        config = Config.load(str(default_config_path))
    else:
        config = get_development_config()

    # Override IP Pepper si fournie
    if args.pepper_ip:
        config.pepper.ip = args.pepper_ip

    # Mode debug
    if args.debug:
        config.logging.console_level = "DEBUG"
        config.logging.file_level = "DEBUG"

    # Creer et demarrer l'assistant
    assistant = PepperAssistant(config, tablet_url_override=args.tablet_url)

    # Configurer les handlers de signaux
    setup_signal_handlers(assistant)

    # Initialiser et lancer
    await assistant.setup()
    await assistant.run()


def run():
    # Point d'entree pour le script console.
    asyncio.run(main())


if __name__ == "__main__":
    run()
