#!/usr/bin/env python3
# Phase 5 - Module Vision (VLM)

import os
import sys
import time
import asyncio
import tempfile
import struct
import subprocess
import platform
import base64
import re
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any, Tuple, Union
from enum import Enum
from concurrent.futures import ThreadPoolExecutor
from PIL import Image, ImageOps, ImageFilter, ImageEnhance
import io

# Constantes
PEPPER_CAMERA_WIDTH = 640
PEPPER_CAMERA_HEIGHT = 480
VLM_TARGET_SIZE = 448  # Taille optimale pour VLM

# Seuils de confiance
CONFIDENCE_HIGH = 0.85  # Affichage direct
CONFIDENCE_MEDIUM = 0.60  # Top-3 avec confirmation
CONFIDENCE_LOW = 0.60  # Fallback code-barres

# Modèle VLM
MODEL_NAME = "mlx-community/Qwen2-VL-2B-Instruct-4bit"

# API PUBLIQUE (COMPAT INTEGRATION)

try:
    from assistant.config import VisionConfig as VisionConfig
except Exception:
    @dataclass
    class VisionConfig:
        # Configuration module vision (fallback si assistant.config absent).
        camera_index: int = 0
        camera_width: int = 640
        camera_height: int = 480
        camera_fps: int = 30
        num_frames: int = 3
        capture_interval_ms: int = 200
        vlm_model: str = MODEL_NAME
        vlm_max_tokens: int = 100
        confidence_high: float = CONFIDENCE_HIGH
        confidence_medium: float = CONFIDENCE_MEDIUM
        confidence_low: float = CONFIDENCE_LOW
        barcode_min_detections: int = 2


class ConfidenceLevel(Enum):
    # Niveau de confiance simplifié pour l'API publique.
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    FAILED = "failed"


@dataclass
class ProductPrediction:
    # Prédiction produit pour l'API publique.
    product_id: Optional[str]
    name: str
    brand: str
    confidence: float
    source: str


@dataclass
class VisionResult:
    # Résultat simplifié pour l'API publique.
    success: bool
    confidence_level: ConfidenceLevel
    top_prediction: Optional[ProductPrediction] = None
    predictions: List[ProductPrediction] = field(default_factory=list)
    message: str = ""
    raw_result: Optional["IdentificationResult"] = None


# STRUCTURES DE DONNÉES

class IdentificationSource(Enum):
    # Source de l'identification.
    BARCODE = "barcode"          # Code-barres détecté
    VLM_HIGH = "vlm_high"        # VLM confiance >= 85%
    VLM_MEDIUM = "vlm_medium"    # VLM confiance 60-85%
    VLM_LOW = "vlm_low"          # VLM confiance < 60%
    FALLBACK = "fallback"        # Fallback code-barres dédié
    FAILED = "failed"            # Échec identification


@dataclass
class BarcodeResult:
    # Résultat de détection code-barres.
    ean13: str
    confidence: float  # Nombre d'images où le code a été trouvé / total
    positions: List[Tuple[int, int, int, int]]  # Bounding boxes
    image_indices: List[int]  # Indices des images où trouvé


@dataclass
class VLMResult:
    # Résultat d'analyse VLM.
    product_name: str
    brand: str
    confidence: float
    product_type: str
    raw_response: str
    inference_time_ms: float


@dataclass
class ProductCandidate:
    # Candidat produit avec score.
    product_id: str
    name: str
    brand: str
    score: float
    source: str  # "vlm" ou "barcode"


@dataclass
class IdentificationResult:
    # Résultat final d'identification.
    success: bool
    source: IdentificationSource
    product_id: Optional[str] = None
    product_name: Optional[str] = None
    brand: Optional[str] = None
    confidence: float = 0.0

    # Top-3 candidats (si VLM_MEDIUM)
    candidates: List[ProductCandidate] = field(default_factory=list)

    # Détails
    barcode_result: Optional[BarcodeResult] = None
    vlm_result: Optional[VLMResult] = None

    # Métriques
    total_time_ms: float = 0.0
    vlm_time_ms: float = 0.0
    barcode_time_ms: float = 0.0

    # Message pour l'utilisateur
    message: str = ""


@dataclass
class CaptureConfig:
    # Configuration de capture.
    num_frames: int = 3
    frame_interval_ms: int = 100  # Intervalle entre frames (rafale)
    use_burst: bool = True  # True = rafale rapide, False = espacé
    resize_to_vlm: bool = True  # Redimensionner à 448x448


# CAPTURE D'IMAGES

class ImageCapture:
    # Capture d'images depuis la caméra Pepper.

    def __init__(self, config: Optional[CaptureConfig] = None):
        # Initialise l'objet.
        self.config = config or CaptureConfig()
        self._naoqi_video = None
        self._subscriber_id = None

    def connect_pepper(self, pepper_ip: str, port: int = 9559) -> bool:
        # Connexion à la caméra Pepper via NAOqi.
        try:
            import qi
            session = qi.Session()
            session.connect(f"tcp://{pepper_ip}:{port}")
            self._naoqi_video = session.service("ALVideoDevice")

            # S'abonner à la caméra (top camera = 0, VGA = 1, RGB = 11)
            self._subscriber_id = self._naoqi_video.subscribeCamera(
                "VisionModule",
                0,  # Top camera
                1,  # VGA (640x480)
                11,  # RGB
                30  # 30 FPS
            )
            print(f"[ImageCapture] Connecté à Pepper {pepper_ip}")
            return True

        except ImportError:
            print("[ImageCapture] NAOqi non disponible - mode simulation")
            return False
        except Exception as e:
            print(f"[ImageCapture] Erreur connexion Pepper: {e}")
            return False

    def capture_frames(self) -> List[Image.Image]:
        # Capture plusieurs frames selon la configuration.
        frames = []

        if self._naoqi_video and self._subscriber_id:
            # Capture depuis Pepper
            frames = self._capture_from_pepper()
        else:
            # Mode simulation - génère des images de test
            print("[ImageCapture] Mode simulation - pas de vraies captures")
            return frames

        return frames

    def _capture_from_pepper(self) -> List[Image.Image]:
        # Capture depuis la caméra Pepper.
        frames = []

        for i in range(self.config.num_frames):
            try:
                # Capturer image
                image_data = self._naoqi_video.getImageRemote(self._subscriber_id)

                if image_data:
                    width = image_data[0]
                    height = image_data[1]
                    array = image_data[6]

                    # Convertir en PIL Image
                    img = Image.frombytes("RGB", (width, height), bytes(array))
                    frames.append(img)

                # Attendre entre les frames
                if i < self.config.num_frames - 1:
                    time.sleep(self.config.frame_interval_ms / 1000.0)

            except Exception as e:
                print(f"[ImageCapture] Erreur capture frame {i}: {e}")
                
        return frames

    def load_images_from_paths(self, paths: List[str]) -> List[Image.Image]:
        # Charge des images depuis des chemins de fichiers.
        images = []
        for path in paths:
            try:
                img = Image.open(path)
                if img.mode != 'RGB':
                    img = img.convert('RGB')
                images.append(img)
            except Exception as e:
                print(f"[ImageCapture] Erreur chargement {path}: {e}")
        return images

    def preprocess_for_vlm(self, image: Image.Image) -> Image.Image:
        # Prétraite une image pour le VLM.
        # Redimensionner en gardant le ratio puis centrer
        target_size = VLM_TARGET_SIZE

        # Calculer le ratio
        ratio = min(target_size / image.width, target_size / image.height)
        new_size = (int(image.width * ratio), int(image.height * ratio))

        # Redimensionner
        resized = image.resize(new_size, Image.Resampling.LANCZOS)

        # Créer image carrée avec padding noir
        result = Image.new('RGB', (target_size, target_size), (0, 0, 0))
        offset = ((target_size - new_size[0]) // 2, (target_size - new_size[1]) // 2)
        result.paste(resized, offset)

        return result

    def disconnect(self):
        # Déconnexion de Pepper.
        if self._naoqi_video and self._subscriber_id:
            try:
                self._naoqi_video.unsubscribe(self._subscriber_id)
            except:
                pass
            self._naoqi_video = None
            self._subscriber_id = None


# DÉTECTION CODE-BARRES

class BarcodeDetector:
    # Détection de codes-barres avec pyzbar.

    def __init__(self):
        # Initialise l'objet.
        self._pyzbar_available = False
        self._debug = os.getenv("PEPPER_BARCODE_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}
        self._deep_search = os.getenv("PEPPER_BARCODE_DEEP_SEARCH", "").strip().lower() in {"1", "true", "yes", "on"}
        self._openai_first_enabled = os.getenv("PEPPER_BARCODE_OPENAI_FIRST", "1").strip().lower() in {
            "1", "true", "yes", "on"
        }
        self._openai_model = (
            os.getenv("OPENAI_VISION_BARCODE_MODEL", "").strip()
            or os.getenv("OPENAI_VISION_MODEL", "").strip()
            or "gpt-4o-mini"
        )
        self._openai_client = None
        self.last_source = "none"
        self._setup_openai_client()
        try:
            from pyzbar import pyzbar
            self._pyzbar = pyzbar
            self._pyzbar_available = True
        except ImportError:
            # Sur macOS/Homebrew, libzbar peut être installée hors chemins dynamiques par défaut.
            brew_lib = "/opt/homebrew/lib"
            libzbar = os.path.join(brew_lib, "libzbar.dylib")
            current = os.getenv("DYLD_FALLBACK_LIBRARY_PATH", "")
            needs_retry = os.path.exists(libzbar) and brew_lib not in current.split(":")
            if needs_retry:
                os.environ["DYLD_FALLBACK_LIBRARY_PATH"] = (
                    f"{brew_lib}:{current}" if current else brew_lib
                )
                try:
                    from pyzbar import pyzbar
                    self._pyzbar = pyzbar
                    self._pyzbar_available = True
                except ImportError:
                    pass
            if not self._pyzbar_available:
                print("[BarcodeDetector] pyzbar non disponible - pip install pyzbar")

    def _setup_openai_client(self):
        if not self._openai_first_enabled:
            return
        api_key = (os.getenv("OPENAI_API_KEY", "") or "").strip()
        if not api_key:
            return
        try:
            from openai import OpenAI
            timeout_s = float(os.getenv("OPENAI_BARCODE_TIMEOUT_S", "14") or 14)
            max_retries = int(os.getenv("OPENAI_BARCODE_MAX_RETRIES", "1") or 1)
            self._openai_client = OpenAI(
                api_key=api_key,
                timeout=timeout_s,
                max_retries=max_retries,
            )
        except Exception as e:
            if self._debug:
                print(f"[BarcodeDetector] OpenAI indisponible: {e}")

    @staticmethod
    def _is_valid_ean13(ean: str) -> bool:
        value = str(ean or "").strip()
        if len(value) != 13 or not value.isdigit():
            return False
        digits = [int(ch) for ch in value]
        base = digits[:12]
        checksum = digits[12]
        odd_sum = sum(base[0::2])
        even_sum = sum(base[1::2])
        expected = (10 - ((odd_sum + 3 * even_sum) % 10)) % 10
        return checksum == expected

    @staticmethod
    def _extract_ean13_from_text(text: str) -> Optional[str]:
        for match in re.finditer(r"(?<!\d)(\d{13})(?!\d)", str(text or "")):
            candidate = match.group(1)
            if BarcodeDetector._is_valid_ean13(candidate):
                return candidate
        return None

    @staticmethod
    def _image_to_data_url(image: Image.Image, max_side: int = 1024, quality: int = 86) -> str:
        img = image.convert("RGB")
        width, height = img.size
        biggest = max(width, height)
        if biggest > max_side:
            scale = max_side / float(biggest)
            img = img.resize(
                (max(1, int(width * scale)), max(1, int(height * scale))),
                Image.BILINEAR,
            )
        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=quality, optimize=True)
        payload = base64.b64encode(buffer.getvalue()).decode("ascii")
        return f"data:image/jpeg;base64,{payload}"

    @staticmethod
    def _extract_text_from_openai_response(resp: Any) -> str:
        text = getattr(resp, "output_text", "") or ""
        if text:
            return text.strip()
        output = getattr(resp, "output", None) or []
        chunks: List[str] = []
        for item in output:
            for content in getattr(item, "content", []) or []:
                if getattr(content, "type", "") in {"output_text", "text"}:
                    value = getattr(content, "text", "") or ""
                    if value:
                        chunks.append(value)
        return " ".join(chunks).strip()

    def _detect_single_openai(self, image: Image.Image) -> Optional[str]:
        if not self._openai_client:
            return None
        prompt = (
            "Lis uniquement le code-barres visible sur l'image.\n"
            "Retourne STRICTEMENT un seul format:\n"
            "EAN13: <13_chiffres>\n"
            "ou\n"
            "EAN13: NONE\n"
            "N'invente rien."
        )
        image_url = self._image_to_data_url(image)
        attempts = max(1, int(os.getenv("OPENAI_BARCODE_INFERENCE_ATTEMPTS", "2") or 2))
        retry_delay_s = max(0.0, float(os.getenv("OPENAI_BARCODE_RETRY_DELAY_S", "0.3") or 0.3))

        for attempt in range(1, attempts + 1):
            text = ""
            try:
                resp = self._openai_client.responses.create(
                    model=self._openai_model,
                    temperature=0,
                    max_output_tokens=28,
                    input=[
                        {
                            "role": "user",
                            "content": [
                                {"type": "input_text", "text": prompt},
                                {"type": "input_image", "image_url": image_url},
                            ],
                        }
                    ],
                )
                text = self._extract_text_from_openai_response(resp)
            except Exception as e:
                if self._debug:
                    print(
                        f"[BarcodeDetector] OpenAI responses erreur "
                        f"(tentative {attempt}/{attempts}): {e}"
                    )

            if not text:
                try:
                    resp = self._openai_client.chat.completions.create(
                        model=self._openai_model,
                        temperature=0,
                        max_tokens=28,
                        messages=[
                            {
                                "role": "user",
                                "content": [
                                    {"type": "text", "text": prompt},
                                    {"type": "image_url", "image_url": {"url": image_url}},
                                ],
                            }
                        ],
                    )
                    choices = getattr(resp, "choices", []) or []
                    if choices:
                        text = (getattr(choices[0].message, "content", "") or "").strip()
                except Exception as e:
                    if self._debug:
                        print(
                            f"[BarcodeDetector] OpenAI chat erreur "
                            f"(tentative {attempt}/{attempts}): {e}"
                        )

            ean = self._extract_ean13_from_text(text)
            if ean:
                return ean

            if attempt < attempts:
                time.sleep(retry_delay_s)

        return None

    def _detect_in_images_openai(self, images: List[Image.Image]) -> Optional[BarcodeResult]:
        if not self._openai_client or not images:
            return None
        max_images = max(1, int(os.getenv("OPENAI_BARCODE_MAX_IMAGES", "3") or 3))
        required = max(1, int(os.getenv("OPENAI_BARCODE_REQUIRED_DETECTIONS", "1") or 1))
        max_views = max(1, int(os.getenv("OPENAI_BARCODE_MAX_VIEWS_PER_IMAGE", "3") or 3))

        detections: Dict[str, List[int]] = {}
        candidates = list(images[:max_images])
        for idx, image in enumerate(candidates):
            try:
                views: List[Image.Image] = [image]
                try:
                    for _, crop in self._build_search_crops(image):
                        views.append(crop)
                        if len(views) >= max_views:
                            break
                except Exception:
                    views = [image]

                for view_idx, view in enumerate(views):
                    ean = self._detect_single_openai(view)
                    if not ean:
                        continue
                    if ean not in detections:
                        detections[ean] = []
                    detections[ean].append(idx)
                    if self._debug:
                        print(f"[BarcodeDetector] openai image={idx} view={view_idx} ean={ean}")
                    # Un EAN pour une image suffit.
                    break
            except Exception as e:
                if self._debug:
                    print(f"[BarcodeDetector] openai image={idx} erreur: {e}")

        if not detections:
            return None

        best_ean = ""
        best_hits: List[int] = []
        for ean, hits in detections.items():
            if len(hits) > len(best_hits):
                best_ean = ean
                best_hits = hits

        if not best_ean or len(best_hits) < required:
            return None

        return BarcodeResult(
            ean13=best_ean,
            confidence=len(best_hits) / max(1, len(candidates)),
            positions=[],
            image_indices=best_hits,
        )

    @staticmethod
    def _normalize_barcode_value(code_type: str, raw_value: str) -> Optional[str]:
        value = str(raw_value or "").strip()
        code_type = str(code_type or "").strip().upper()
        if not value:
            return None
        if code_type == "UPCA" and len(value) == 12 and value.isdigit():
            return f"0{value}"
        if code_type == "EAN13" and len(value) == 13 and value.isdigit():
            return value
        return None

    def _build_decode_variants(self, image: Image.Image) -> List[Tuple[str, Image.Image]]:
        gray = image.convert("L")
        w, h = gray.size
        upscaled = gray.resize((max(2, w * 2), max(2, h * 2)), Image.Resampling.BICUBIC)
        threshold = gray.point(lambda p: 255 if p > 120 else 0)
        bright = ImageEnhance.Brightness(gray).enhance(1.35)
        contrast = ImageEnhance.Contrast(gray).enhance(1.45)
        variants: List[Tuple[str, Image.Image]] = [
            ("gray", gray),
            ("autocontrast", ImageOps.autocontrast(gray)),
            ("sharpen", gray.filter(ImageFilter.SHARPEN)),
            ("bright", bright),
            ("contrast", contrast),
            ("upscale_2x", upscaled),
            ("threshold", threshold),
        ]
        if self._deep_search:
            darker = ImageEnhance.Brightness(gray).enhance(0.78)
            strong = ImageEnhance.Contrast(bright).enhance(1.35)
            variants.extend([
                ("equalize", ImageOps.equalize(gray)),
                ("darker", darker),
                ("bright_contrast", strong),
            ])
        return variants

    def _build_search_crops(self, image: Image.Image) -> List[Tuple[str, Image.Image]]:
        # Le code-barres peut être petit dans l'image: on balaie plusieurs sous-zones.
        w, h = image.size
        regions = [
            ("full", (0.0, 0.0, 1.0, 1.0)),
            ("center_80", (0.1, 0.1, 0.9, 0.9)),
            ("left_focus", (0.0, 0.0, 0.65, 0.7)),
        ]

        if self._deep_search:
            regions.extend([
                ("left_top_75", (0.0, 0.0, 0.75, 0.75)),
                ("right_top_75", (0.25, 0.0, 1.0, 0.75)),
                ("left_bottom_75", (0.0, 0.25, 0.75, 1.0)),
                ("right_bottom_75", (0.25, 0.25, 1.0, 1.0)),
            ])
            # Fenêtres glissantes (3x3) pour remonter un code-barres petit/hors-centre.
            win_w = 0.6
            win_h = 0.6
            for gy in range(3):
                for gx in range(3):
                    x1 = min(0.4, gx * 0.2)
                    y1 = min(0.4, gy * 0.2)
                    x2 = min(1.0, x1 + win_w)
                    y2 = min(1.0, y1 + win_h)
                    regions.append((f"grid_{gx}_{gy}", (x1, y1, x2, y2)))

        crops: List[Tuple[str, Image.Image]] = []
        seen_boxes = set()
        for name, (rx1, ry1, rx2, ry2) in regions:
            x1 = max(0, min(w - 2, int(w * rx1)))
            y1 = max(0, min(h - 2, int(h * ry1)))
            x2 = max(x1 + 2, min(w, int(w * rx2)))
            y2 = max(y1 + 2, min(h, int(h * ry2)))
            box_key = (x1, y1, x2, y2)
            if box_key in seen_boxes:
                continue
            seen_boxes.add(box_key)
            crops.append((name, image.crop((x1, y1, x2, y2))))
        return crops

    def detect_in_images(self, images: List[Image.Image]) -> Optional[BarcodeResult]:
        # Détecte les codes-barres dans plusieurs images.
        if not images:
            self.last_source = "none"
            return None

        if self._openai_first_enabled:
            openai_barcode = self._detect_in_images_openai(images)
            if openai_barcode and getattr(openai_barcode, "ean13", None):
                self.last_source = "openai_vision"
                return openai_barcode

        if not self._pyzbar_available:
            self.last_source = "none"
            return None

        # Détecter dans chaque image
        detections: Dict[str, List[Tuple[int, Tuple]]] = {}  # EAN -> [(image_idx, bbox), ...]

        for idx, image in enumerate(images):
            try:
                seen_on_this_image = set()
                crops = self._build_search_crops(image)
                for crop_name, crop_img in crops:
                    scales = (1, 2, 3) if self._deep_search else (1, 2)
                    for scale in scales:
                        if scale == 1:
                            scaled = crop_img
                        else:
                            scaled = crop_img.resize(
                                (crop_img.width * scale, crop_img.height * scale),
                                Image.Resampling.BICUBIC,
                            )

                        variants = self._build_decode_variants(scaled)
                        for variant_name, variant_image in variants:
                            barcodes = self._pyzbar.decode(variant_image)
                            for barcode in barcodes:
                                try:
                                    raw_value = barcode.data.decode("utf-8")
                                except Exception:
                                    continue
                                ean = self._normalize_barcode_value(barcode.type, raw_value)
                                if not ean or ean in seen_on_this_image:
                                    continue
                                seen_on_this_image.add(ean)

                                bbox = barcode.rect  # (x, y, w, h)
                                if ean not in detections:
                                    detections[ean] = []
                                detections[ean].append((idx, (bbox.left, bbox.top, bbox.width, bbox.height)))

                                if self._debug:
                                    print(
                                        f"[BarcodeDetector] image={idx} crop={crop_name} scale={scale} "
                                        f"variant={variant_name} type={barcode.type} ean={ean}"
                                    )
                    if seen_on_this_image and not self._deep_search:
                        break

            except Exception as e:
                print(f"[BarcodeDetector] Erreur image {idx}: {e}")

        # Trouver l'EAN le plus fréquent avec validation
        best_ean = None
        best_count = 0

        for ean, occurrences in detections.items():
            if len(occurrences) >= 2 and len(occurrences) > best_count:
                best_ean = ean
                best_count = len(occurrences)

        if best_ean:
            occurrences = detections[best_ean]
            self.last_source = "pyzbar"
            return BarcodeResult(
                ean13=best_ean,
                confidence=len(occurrences) / len(images),
                positions=[occ[1] for occ in occurrences],
                image_indices=[occ[0] for occ in occurrences]
            )

        # Si un seul EAN trouvé dans une seule image, le retourner quand même avec confiance basse
        if detections:
            ean = list(detections.keys())[0]
            occurrences = detections[ean]
            self.last_source = "pyzbar"
            return BarcodeResult(
                ean13=ean,
                confidence=len(occurrences) / len(images),
                positions=[occ[1] for occ in occurrences],
                image_indices=[occ[0] for occ in occurrences]
            )

        if self._debug:
            print(f"[BarcodeDetector] Aucun code détecté sur {len(images)} image(s).")
        self.last_source = "none"
        return None

    def detect_single(self, image: Image.Image) -> List[str]:
        # Détecte les codes-barres dans une seule image.
        if not self._pyzbar_available:
            return []

        try:
            values: List[str] = []
            for _, crop in self._build_search_crops(image):
                scales = (1, 2, 3) if self._deep_search else (1, 2)
                for scale in scales:
                    if scale == 1:
                        scaled = crop
                    else:
                        scaled = crop.resize(
                            (crop.width * scale, crop.height * scale),
                            Image.Resampling.BICUBIC,
                        )
                    for _, variant in self._build_decode_variants(scaled):
                        barcodes = self._pyzbar.decode(variant)
                        for barcode in barcodes:
                            try:
                                raw_value = barcode.data.decode("utf-8")
                            except Exception:
                                continue
                            normalized = self._normalize_barcode_value(barcode.type, raw_value)
                            if normalized and normalized not in values:
                                values.append(normalized)
                        if values and not self._deep_search:
                            return values
                if values and not self._deep_search:
                    return values
            return values
        except Exception:
            return []


# MODULE VLM

class VLMModule:
    # Module VLM pour identification visuelle.

    def __init__(self, product_database: Optional[Dict[str, Any]] = None):
        # Initialise l'objet.
        self.model = None
        self.processor = None
        self.config = None
        self.is_loaded = False
        self._load_time_ms = 0
        self._backend = "none"  # none | mlx | openai
        self._openai_client = None
        self._openai_model = (
            os.getenv("OPENAI_VISION_MODEL", "").strip() or "gpt-4o-mini"
        )
        self._openai_fallback_enabled = (
            os.getenv("OPENAI_VISION_FALLBACK_ENABLED", "1").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        self._backend_preference = (
            os.getenv("PEPPER_VLM_BACKEND", "auto").strip().lower() or "auto"
        )

        # Base de produits pour matching
        self.product_database = product_database or {}
        self._product_names = self._build_product_names()

    def _build_product_names(self) -> str:
        # Construit la liste des produits pour le prompt.
        if not self.product_database:
            return ""

        products = self.product_database.get("products", [])
        lines = []
        for p in products:
            lines.append(f"- {p.get('brand', '')} {p.get('name', '')} ({p.get('ean13', '')})")
        return "\n".join(lines)

    @staticmethod
    def _resolve_local_model_path(model_name: str) -> Optional[str]:
        # Résout un snapshot local HF pour éviter les appels réseau.
        env_path = os.getenv("PEPPER_VLM_LOCAL_PATH", "").strip()
        if env_path and Path(env_path).expanduser().exists():
            return str(Path(env_path).expanduser())

        if Path(model_name).expanduser().exists():
            return str(Path(model_name).expanduser())

        model_dir_name = f"models--{model_name.replace('/', '--')}"
        hf_home = os.getenv("HF_HOME", "").strip()
        if hf_home:
            cache_root = Path(hf_home).expanduser() / "hub"
        else:
            cache_root = Path.home() / ".cache" / "huggingface" / "hub"

        model_cache_dir = cache_root / model_dir_name
        snapshots_dir = model_cache_dir / "snapshots"
        if not snapshots_dir.exists():
            return None

        snapshot_dirs = [p for p in snapshots_dir.iterdir() if p.is_dir()]
        if not snapshot_dirs:
            return None

        # Prendre le snapshot le plus récent.
        snapshot_dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return str(snapshot_dirs[0])

    @staticmethod
    def _mlx_healthcheck() -> Tuple[bool, str]:
        # Vérifie MLX dans un sous-processus pour éviter un crash hard du process.
        if os.getenv("PEPPER_SKIP_MLX_HEALTHCHECK", "").strip().lower() in {"1", "true", "yes", "on"}:
            return True, "healthcheck ignoré"

        # MLX VLM est supporté principalement sur macOS Apple Silicon.
        if platform.system() == "Darwin" and platform.machine() != "arm64":
            return False, "MLX VLM non supporté sur macOS Intel"

        code = (
            "import mlx.core as mx\n"
            "x = mx.array([1,2,3])\n"
            "print(int(x.sum().item()))\n"
        )
        try:
            proc = subprocess.run(
                [sys.executable, "-c", code],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10.0,
                check=False,
            )
        except Exception as e:
            return False, str(e)

        if proc.returncode == 0:
            return True, proc.stdout.strip() or "ok"

        err = (proc.stderr or proc.stdout or "").strip()
        if not err:
            err = f"returncode={proc.returncode}"
        if os.getenv("PEPPER_VLM_DEBUG", "").strip().lower() not in {"1", "true", "yes", "on"}:
            err = err.splitlines()[0] if err.splitlines() else err
        if (
            "Traceback (most recent call last):" in err
            or "NSRangeException" in err
            or "Terminating app due to uncaught exception" in err
        ):
            err = "mlx.core indisponible (Metal non accessible dans cette session)"
        return False, err

    def _setup_openai_fallback(self) -> bool:
        if not self._openai_fallback_enabled:
            return False
        api_key = (os.getenv("OPENAI_API_KEY", "") or "").strip()
        if not api_key:
            return False
        try:
            from openai import OpenAI
            timeout_s = float(os.getenv("OPENAI_VISION_TIMEOUT_S", "25") or 25)
            max_retries = int(os.getenv("OPENAI_VISION_MAX_RETRIES", "1") or 1)
            self._openai_client = OpenAI(
                api_key=api_key,
                timeout=timeout_s,
                max_retries=max_retries,
            )
            self._backend = "openai"
            self.is_loaded = True
            return True
        except Exception as e:
            print(f"[VLM] Fallback OpenAI indisponible: {e}")
            return False

    def load_model(self) -> bool:
        # Charge le modèle VLM.
        print("[VLM] Chargement du modèle...")
        start_time = time.time()

        if self._backend_preference == "openai":
            print("[VLM] Backend forcé: OpenAI")
            if self._setup_openai_fallback():
                self._load_time_ms = (time.time() - start_time) * 1000
                print(f"[VLM] Fallback vision OpenAI actif ({self._openai_model})")
                return True
            return False

        mlx_ok, mlx_msg = self._mlx_healthcheck()
        if not mlx_ok:
            print(f"[VLM] MLX indisponible: {mlx_msg}")
            if self._setup_openai_fallback():
                self._load_time_ms = (time.time() - start_time) * 1000
                print(f"[VLM] Fallback vision OpenAI actif ({self._openai_model})")
                return True
            return False

        try:
            from mlx_vlm import load, generate
            from mlx_vlm.prompt_utils import apply_chat_template
            from mlx_vlm.utils import load_config

            model_ref = MODEL_NAME
            local_model_path = self._resolve_local_model_path(MODEL_NAME)
            if local_model_path:
                model_ref = local_model_path
                # Si le snapshot local est trouvé, on force l'offline pour éviter
                # toute requête metadata vers HuggingFace (SSL réseau instable).
                os.environ.setdefault("HF_HUB_OFFLINE", "1")
                print(f"[VLM] Chargement local depuis: {model_ref}")

            self.model, self.processor = load(model_ref)
            self.config = load_config(model_ref)
            self._generate_fn = generate
            self._apply_chat_template = apply_chat_template

            self._load_time_ms = (time.time() - start_time) * 1000
            self.is_loaded = True
            self._backend = "mlx"
            print(f"[VLM] Modèle chargé en {self._load_time_ms:.0f}ms")
            return True

        except ImportError as e:
            print(f"[VLM] mlx-vlm non installé: {e}")
            if self._setup_openai_fallback():
                self._load_time_ms = (time.time() - start_time) * 1000
                print(f"[VLM] Fallback vision OpenAI actif ({self._openai_model})")
                return True
            return False
        except Exception as e:
            print(f"[VLM] Erreur chargement: {e}")
            if self._setup_openai_fallback():
                self._load_time_ms = (time.time() - start_time) * 1000
                print(f"[VLM] Fallback vision OpenAI actif ({self._openai_model})")
                return True
            return False

    def classify_hair_product(self, image: Image.Image) -> Tuple[bool, float]:
        # Classification préalable : est-ce un produit capillaire ?
        if not self.is_loaded:
            return False, 0.0

        prompt = """Regarde cette image et réponds uniquement par OUI ou NON:
Est-ce un produit capillaire (shampooing, après-shampooing, masque, huile, sérum pour cheveux) ?

Réponds au format:
REPONSE: OUI ou NON
CONFIANCE: 0-100"""

        if self._backend == "openai":
            result = self._run_inference_openai(image, prompt, max_tokens=40)
        else:
            result = self._run_inference(image, prompt, max_tokens=20)

        if result:
            response = result.lower()
            is_hair = "oui" in response and "non" not in response.split("oui")[0]

            # Extraire confiance de manière robuste (ex: "confiance 73%")
            confidence = 0.5
            for line in response.splitlines():
                if "confiance" not in line:
                    continue
                m = re.search(r"(-?\d+(?:[.,]\d+)?)", line)
                if not m:
                    continue
                try:
                    value = float(m.group(1).replace(",", "."))
                    confidence = value / 100.0 if value > 1.0 else value
                    confidence = max(0.0, min(1.0, confidence))
                    break
                except Exception:
                    pass

            return is_hair, confidence

        return False, 0.0

    def identify_product(self, image: Image.Image) -> VLMResult:
        # Identifie un produit capillaire.
        if not self.is_loaded:
            return VLMResult(
                product_name="",
                brand="",
                confidence=0.0,
                product_type="",
                raw_response="Modèle non chargé",
                inference_time_ms=0
            )

        # Prompt avec liste des produits
        prompt = f"""Tu es un expert en identification de produits capillaires.
Regarde attentivement cette image et LIS LE TEXTE visible sur l'emballage.

PRODUITS CONNUS:
{self._product_names}

INSTRUCTIONS:
1. Identifie la MARQUE sur le flacon
2. Lis le NOM COMPLET du produit
3. Donne ton niveau de confiance

Réponds UNIQUEMENT au format:
PRODUIT: [MARQUE] [Nom du produit]
CONFIANCE: [0-100]
TYPE: [shampooing/apres-shampooing/masque/huile/serum/autre]

Si tu ne peux pas lire le texte, indique CONFIANCE: 0."""

        start_time = time.time()
        if self._backend == "openai":
            result = self._run_inference_openai(image, prompt, max_tokens=120)
        else:
            result = self._run_inference(image, prompt, max_tokens=50)
        inference_time = (time.time() - start_time) * 1000

        if result:
            return self._parse_identification_response(result, inference_time)

        return VLMResult(
            product_name="",
            brand="",
            confidence=0.0,
            product_type="",
            raw_response="Erreur inférence",
            inference_time_ms=inference_time
        )

    def identify_with_top3(self, image: Image.Image) -> List[VLMResult]:
        # Identifie un produit et retourne Top-3 candidats.
        if not self.is_loaded:
            return []

        prompt = f"""Tu es un expert en identification de produits capillaires.
Regarde cette image et propose les 3 produits les plus probables.

PRODUITS CONNUS:
{self._product_names}

Réponds avec 3 propositions au format:
1. PRODUIT: [nom] | CONFIANCE: [0-100]
2. PRODUIT: [nom] | CONFIANCE: [0-100]
3. PRODUIT: [nom] | CONFIANCE: [0-100]"""

        start_time = time.time()
        if self._backend == "openai":
            result = self._run_inference_openai(image, prompt, max_tokens=220)
        else:
            result = self._run_inference(image, prompt, max_tokens=100)
        inference_time = (time.time() - start_time) * 1000

        results = []
        if result:
            lines = result.strip().split('\n')
            for line in lines:
                if 'produit:' in line.lower():
                    try:
                        # Parser "1. PRODUIT: xxx | CONFIANCE: 90"
                        parts = line.split('|')
                        if len(parts) >= 2:
                            product_part = parts[0].split(':')[1].strip() if ':' in parts[0] else ""
                            conf_part = parts[1].split(':')[1].strip() if ':' in parts[1] else "0"
                            conf_part = conf_part.replace('%', '').strip()

                            # Extraire marque et nom
                            words = product_part.split()
                            brand = words[0] if words else ""
                            name = ' '.join(words[1:]) if len(words) > 1 else product_part

                            results.append(VLMResult(
                                product_name=product_part,
                                brand=brand,
                                confidence=float(conf_part) / 100.0,
                                product_type="",
                                raw_response=line,
                                inference_time_ms=inference_time / 3
                            ))
                    except:
                        pass

        return results[:3]

    @staticmethod
    def _image_to_data_url(image: Image.Image, max_side: int = 1024, quality: int = 88) -> str:
        # Convertit une image PIL en data URL JPEG.
        img = image.convert("RGB")
        w, h = img.size
        biggest = max(w, h)
        if biggest > max_side:
            scale = max_side / float(biggest)
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.BILINEAR)

        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=quality, optimize=True)
        payload = base64.b64encode(buffer.getvalue()).decode("ascii")
        return f"data:image/jpeg;base64,{payload}"

    @staticmethod
    def _extract_text_from_openai_response(resp: Any) -> str:
        text = getattr(resp, "output_text", "") or ""
        if text:
            return text.strip()
        output = getattr(resp, "output", None) or []
        chunks: List[str] = []
        for item in output:
            for content in getattr(item, "content", []) or []:
                if getattr(content, "type", "") in {"output_text", "text"}:
                    value = getattr(content, "text", "") or ""
                    if value:
                        chunks.append(value)
        return " ".join(chunks).strip()

    def _run_inference_openai(
        self,
        image: Image.Image,
        prompt: str,
        max_tokens: int = 120,
    ) -> Optional[str]:
        # Exécute une inférence vision via OpenAI HTTP.
        if not self._openai_client:
            return None
        image_url = self._image_to_data_url(image)
        attempts = max(1, int(os.getenv("OPENAI_VISION_INFERENCE_ATTEMPTS", "2") or 2))
        retry_delay_s = max(0.0, float(os.getenv("OPENAI_VISION_RETRY_DELAY_S", "0.4") or 0.4))

        for i in range(1, attempts + 1):
            # Chemin principal: Responses API.
            try:
                resp = self._openai_client.responses.create(
                    model=self._openai_model,
                    temperature=0.2,
                    max_output_tokens=max_tokens,
                    input=[
                        {
                            "role": "user",
                            "content": [
                                {"type": "input_text", "text": prompt},
                                {"type": "input_image", "image_url": image_url},
                            ],
                        }
                    ],
                )
                text = self._extract_text_from_openai_response(resp)
                if text:
                    return text
            except Exception as e:
                print(f"[VLM] OpenAI Responses erreur (tentative {i}/{attempts}): {e}")

            # Fallback legacy: Chat Completions.
            try:
                resp = self._openai_client.chat.completions.create(
                    model=self._openai_model,
                    temperature=0.2,
                    max_tokens=max_tokens,
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": prompt},
                                {"type": "image_url", "image_url": {"url": image_url}},
                            ],
                        }
                    ],
                )
                choices = getattr(resp, "choices", []) or []
                if choices:
                    content = getattr(choices[0].message, "content", "") or ""
                    if content:
                        return content.strip()
            except Exception as e:
                print(f"[VLM] OpenAI Chat erreur (tentative {i}/{attempts}): {e}")

            if i < attempts:
                time.sleep(retry_delay_s)

        return None

    def _run_inference(self, image: Image.Image, prompt: str, max_tokens: int = 50) -> Optional[str]:
        # Exécute une inférence VLM.
        try:
            # Sauvegarder image temporairement
            with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as f:
                image.save(f, 'JPEG', quality=90)
                temp_path = f.name

            try:
                # Appliquer template
                formatted_prompt = self._apply_chat_template(
                    self.processor,
                    self.config,
                    prompt,
                    num_images=1
                )

                # Générer
                response = self._generate_fn(
                    self.model,
                    self.processor,
                    formatted_prompt,
                    image=[temp_path],
                    max_tokens=max_tokens,
                    verbose=False
                )

                if hasattr(response, 'text'):
                    return response.text
                return str(response)

            finally:
                os.unlink(temp_path)

        except Exception as e:
            print(f"[VLM] Erreur inférence: {e}")
            return None

    def _parse_identification_response(self, response: str, inference_time: float) -> VLMResult:
        # Parse la réponse d'identification.
        product_name = ""
        brand = ""
        confidence = 0.0
        product_type = ""

        lines = response.strip().split('\n')
        for line in lines:
            line_lower = line.lower().strip()

            if ('produit' in line_lower or 'product' in line_lower) and ':' in line:
                product_name = line.split(':', 1)[1].strip().strip("-• ")
                # Extraire marque (premier mot)
                words = product_name.split()
                if words:
                    brand = words[0]

            elif 'confiance' in line_lower or 'confidence' in line_lower:
                try:
                    m = re.search(r"(-?\d+(?:[.,]\d+)?)", line)
                    if not m:
                        continue
                    value = float(m.group(1).replace(",", "."))
                    confidence = value / 100.0 if value > 1.0 else value
                    confidence = max(0.0, min(1.0, confidence))
                except:
                    pass

            elif ('type' in line_lower or 'categorie' in line_lower) and ':' in line:
                product_type = line.split(':', 1)[1].strip()

        # Fallback: si le modèle ne suit pas strictement le format,
        # prendre la première ligne informative comme nom produit.
        if not product_name:
            for line in lines:
                candidate = line.strip().strip("-• ")
                if not candidate:
                    continue
                low = candidate.lower()
                if any(tag in low for tag in ("confiance", "confidence", "type", "produit", "product")):
                    continue
                if len(candidate.split()) >= 2:
                    product_name = candidate
                    words = product_name.split()
                    brand = words[0] if words else ""
                    break

        return VLMResult(
            product_name=product_name,
            brand=brand,
            confidence=confidence,
            product_type=product_type,
            raw_response=response,
            inference_time_ms=inference_time
        )


# MODULE VLM SIMULÉ

class VLMModuleSimulated:
    # Version simulée du VLM pour tests.

    def __init__(self, product_database: Optional[Dict[str, Any]] = None):
        # Initialise l'objet.
        self.is_loaded = False
        self.product_database = product_database or {}

        # Produits simulés
        self._simulated = {
            "klorane": ("Klorane Shampooing Illuminateur Camomille", "Klorane", 0.92),
            "elseve": ("L'Oréal Elsève Hyaluron Repulp", "L'Oréal Paris", 0.89),
            "garnier": ("Garnier Ultra Doux Trésors de Miel", "Garnier", 0.91),
            "furterer": ("René Furterer Naturia Micellaire", "René Furterer", 0.93),
            "ducray": ("Ducray Extra-Doux Dermo-Protecteur", "Ducray", 0.90),
        }

    def load_model(self) -> bool:
        # Simule le chargement.
        print("[VLM-SIM] Chargement simulé...")
        time.sleep(0.3)
        self.is_loaded = True
        print("[VLM-SIM] Modèle simulé prêt")
        return True

    def classify_hair_product(self, image: Image.Image) -> Tuple[bool, float]:
        # Simule la classification.
        time.sleep(0.1)
        return True, 0.95

    def identify_product(self, image: Image.Image) -> VLMResult:
        # Simule l'identification.
        import random
        time.sleep(random.uniform(0.2, 0.4))

        # Choisir un produit aléatoire
        key = random.choice(list(self._simulated.keys()))
        name, brand, conf = self._simulated[key]

        # Ajouter variation
        conf += random.uniform(-0.1, 0.1)
        conf = max(0.0, min(1.0, conf))

        return VLMResult(
            product_name=name,
            brand=brand,
            confidence=conf,
            product_type="shampooing",
            raw_response=f"PRODUIT: {name}\nCONFIANCE: {int(conf*100)}%\nTYPE: shampooing",
            inference_time_ms=random.uniform(200, 400)
        )

    def identify_with_top3(self, image: Image.Image) -> List[VLMResult]:
        # Simule Top-3.
        import random
        time.sleep(random.uniform(0.3, 0.5))

        results = []
        keys = list(self._simulated.keys())
        random.shuffle(keys)

        for i, key in enumerate(keys[:3]):
            name, brand, conf = self._simulated[key]
            conf = conf - (i * 0.15) + random.uniform(-0.05, 0.05)
            conf = max(0.0, min(1.0, conf))

            results.append(VLMResult(
                product_name=name,
                brand=brand,
                confidence=conf,
                product_type="shampooing",
                raw_response=f"{i+1}. {name} ({int(conf*100)}%)",
                inference_time_ms=100
            ))

        return results


# PIPELINE COMPLET

class VisionPipeline:
    # Pipeline complet d'identification visuelle.

    def __init__(
        # Initialise l'objet.
        self,
        product_database: Dict[str, Any],
        use_simulation: bool = False
    ):
        self.product_database = product_database
        self.use_simulation = use_simulation

        # Composants
        self.capture = ImageCapture()
        self.barcode_detector = BarcodeDetector()

        if use_simulation:
            self.vlm = VLMModuleSimulated(product_database)
        else:
            self.vlm = VLMModule(product_database)

        # Index EAN -> produit
        self._ean_index = self._build_ean_index()

        # Thread pool pour parallélisation
        self._executor = ThreadPoolExecutor(max_workers=2)

    def _build_ean_index(self) -> Dict[str, Dict]:
        # Construit l'index EAN -> produit.
        index = {}
        for product in self.product_database.get("products", []):
            ean = product.get("ean13")
            if ean:
                index[ean] = product
        return index

    def load(self) -> bool:
        # Charge le modèle VLM.
        return self.vlm.load_model()

    def identify_from_images(self, images: List[Image.Image]) -> IdentificationResult:
        # Identifie un produit à partir de plusieurs images.
        start_time = time.time()

        if not images:
            return IdentificationResult(
                success=False,
                source=IdentificationSource.FAILED,
                message="Aucune image fournie"
            )

        # 1. Détection code-barres en parallèle
        barcode_start = time.time()
        barcode_result = self.barcode_detector.detect_in_images(images)
        barcode_time = (time.time() - barcode_start) * 1000

        # 2. Si code-barres fiable (2+ images), utiliser directement
        if barcode_result and barcode_result.confidence >= 0.66:
            product = self._ean_index.get(barcode_result.ean13)
            if product:
                return IdentificationResult(
                    success=True,
                    source=IdentificationSource.BARCODE,
                    product_id=product.get("id"),
                    product_name=product.get("name"),
                    brand=product.get("brand"),
                    confidence=barcode_result.confidence,
                    barcode_result=barcode_result,
                    total_time_ms=(time.time() - start_time) * 1000,
                    barcode_time_ms=barcode_time,
                    message=f"Produit identifié par code-barres: {product.get('name')}"
                )

        # 3. Classification préalable (produit capillaire ?)
        best_image = images[len(images) // 2]  # Image du milieu
        preprocessed = self.capture.preprocess_for_vlm(best_image)

        is_hair_product, class_conf = self.vlm.classify_hair_product(preprocessed)
        vlm_backend = getattr(self.vlm, "_backend", "")
        skip_hair_classifier = (
            os.getenv("PEPPER_VLM_SKIP_HAIR_CLASSIFIER", "0").strip().lower()
            in {"1", "true", "yes", "on"}
        ) or vlm_backend == "openai"

        if not skip_hair_classifier and not is_hair_product:
            return IdentificationResult(
                success=False,
                source=IdentificationSource.FAILED,
                total_time_ms=(time.time() - start_time) * 1000,
                message="Ce n'est pas un produit capillaire. Je ne peux t'aider que pour les produits du rayon cheveux."
            )

        # 4. Identification VLM
        vlm_start = time.time()
        vlm_result = self.vlm.identify_product(preprocessed)
        vlm_time = (time.time() - vlm_start) * 1000

        # 5. Arbitrage selon confiance
        total_time = (time.time() - start_time) * 1000
        matched_product, match_score = self._match_product_by_name_with_score(vlm_result.product_name)
        allow_direct_high = os.getenv("PEPPER_VLM_ALLOW_DIRECT_HIGH", "0").strip().lower() in {"1", "true", "yes", "on"}
        high_min_match = max(0.0, min(1.0, float(os.getenv("PEPPER_VLM_HIGH_MIN_MATCH", "0.72") or 0.72)))

        # Confiance >= 85% : affichage direct uniquement si explicitement autorisé
        # ET si le matching en base est solide.
        if vlm_result.confidence >= CONFIDENCE_HIGH:
            if allow_direct_high and matched_product and match_score >= high_min_match:
                return IdentificationResult(
                    success=True,
                    source=IdentificationSource.VLM_HIGH,
                    product_id=matched_product.get("id") if matched_product else None,
                    product_name=matched_product.get("name") if matched_product else vlm_result.product_name,
                    brand=matched_product.get("brand") if matched_product else vlm_result.brand,
                    confidence=min(vlm_result.confidence, match_score),
                    vlm_result=vlm_result,
                    barcode_result=barcode_result,
                    total_time_ms=total_time,
                    vlm_time_ms=vlm_time,
                    barcode_time_ms=barcode_time,
                    message=(
                        f"J'ai identifié {matched_product.get('brand')} {matched_product.get('name')} "
                        f"avec une confiance de {min(vlm_result.confidence, match_score)*100:.0f}%"
                    ),
                )
            # Par défaut, un vlm_high devient une proposition à confirmer.
            # On évite les faux positifs "très confiants" sur image ambiguë.
            top3 = self.vlm.identify_with_top3(preprocessed)
            candidates = []
            for vlm_r in top3:
                matched, score = self._match_product_by_name_with_score(vlm_r.product_name)
                candidates.append(ProductCandidate(
                    product_id=matched.get("id") if matched else "",
                    name=(matched.get("name") if matched else vlm_r.product_name),
                    brand=(matched.get("brand") if matched else vlm_r.brand),
                    score=max(0.0, min(1.0, min(vlm_r.confidence, score if score > 0 else vlm_r.confidence))),
                    source="vlm"
                ))
            if not candidates:
                candidates.append(ProductCandidate(
                    product_id=matched_product.get("id") if matched_product else "",
                    name=(matched_product.get("name") if matched_product else vlm_result.product_name),
                    brand=(matched_product.get("brand") if matched_product else vlm_result.brand),
                    score=max(0.0, min(1.0, min(vlm_result.confidence, match_score if match_score > 0 else 0.6))),
                    source="vlm"
                ))
            return IdentificationResult(
                success=True,
                source=IdentificationSource.VLM_MEDIUM,
                product_id=matched_product.get("id") if matched_product else None,
                product_name=(matched_product.get("name") if matched_product else vlm_result.product_name),
                brand=(matched_product.get("brand") if matched_product else vlm_result.brand),
                confidence=max(0.0, min(1.0, min(vlm_result.confidence, match_score if match_score > 0 else 0.7))),
                candidates=candidates[:3],
                vlm_result=vlm_result,
                barcode_result=barcode_result,
                total_time_ms=total_time,
                vlm_time_ms=vlm_time,
                barcode_time_ms=barcode_time,
                message=(
                    "J'ai une proposition visuelle, mais je préfère une confirmation. "
                    "Choisis le bon produit ou passe au code-barres."
                ),
            )

        # Confiance 60-85% : Top-3 avec confirmation
        if vlm_result.confidence >= CONFIDENCE_MEDIUM:
            top3 = self.vlm.identify_with_top3(preprocessed)
            candidates = []

            for vlm_r in top3:
                matched, score = self._match_product_by_name_with_score(vlm_r.product_name)
                candidates.append(ProductCandidate(
                    product_id=matched.get("id") if matched else "",
                    name=(matched.get("name") if matched else vlm_r.product_name),
                    brand=(matched.get("brand") if matched else vlm_r.brand),
                    score=max(0.0, min(1.0, min(vlm_r.confidence, score if score > 0 else vlm_r.confidence))),
                    source="vlm"
                ))

            return IdentificationResult(
                success=True,
                source=IdentificationSource.VLM_MEDIUM,
                product_name=(matched_product.get("name") if matched_product else vlm_result.product_name),
                brand=(matched_product.get("brand") if matched_product else vlm_result.brand),
                confidence=max(0.0, min(1.0, min(vlm_result.confidence, match_score if match_score > 0 else 0.6))),
                candidates=candidates,
                vlm_result=vlm_result,
                barcode_result=barcode_result,
                total_time_ms=total_time,
                vlm_time_ms=vlm_time,
                barcode_time_ms=barcode_time,
                message=f"Je pense qu'il s'agit de {vlm_result.product_name}, mais je ne suis pas sûr. Peux-tu me montrer le code-barres ?"
            )

        # Confiance < 60% : fallback code-barres
        if barcode_result:
            product = self._ean_index.get(barcode_result.ean13)
            if product:
                return IdentificationResult(
                    success=True,
                    source=IdentificationSource.FALLBACK,
                    product_id=product.get("id"),
                    product_name=product.get("name"),
                    brand=product.get("brand"),
                    confidence=barcode_result.confidence,
                    barcode_result=barcode_result,
                    vlm_result=vlm_result,
                    total_time_ms=total_time,
                    vlm_time_ms=vlm_time,
                    barcode_time_ms=barcode_time,
                    message=f"Produit identifié par code-barres: {product.get('name')}"
                )

        # Échec complet
        return IdentificationResult(
            success=False,
            source=IdentificationSource.FAILED,
            vlm_result=vlm_result,
            barcode_result=barcode_result,
            total_time_ms=total_time,
            vlm_time_ms=vlm_time,
            barcode_time_ms=barcode_time,
            message="Je n'ai pas réussi à identifier ce produit. Peux-tu me montrer le code-barres ou l'étiquette plus clairement ?"
        )

    def _match_product_by_name_with_score(self, name: str) -> Tuple[Optional[Dict], float]:
        # Trouve un produit par son nom (fuzzy matching simple) et retourne le score.
        if not name:
            return None, 0.0

        name_lower = name.lower()
        best_match = None
        best_score = 0.0

        for product in self.product_database.get("products", []):
            product_name = f"{product.get('brand', '')} {product.get('name', '')}".lower()

            # Score simple basé sur mots communs
            name_words = set(name_lower.split())
            product_words = set(product_name.split())
            common = len(name_words & product_words)
            score = common / max(len(name_words), len(product_words))

            if score > best_score:
                best_score = score
                best_match = product

        if best_score > 0.3:
            return best_match, float(best_score)
        return None, float(best_score)

    def _match_product_by_name(self, name: str) -> Optional[Dict]:
        # Compat: garde l'ancienne API.
        match, _ = self._match_product_by_name_with_score(name)
        return match

    def identify_from_paths(self, image_paths: List[str]) -> IdentificationResult:
        # Identifie à partir de chemins d'images.
        images = self.capture.load_images_from_paths(image_paths)
        return self.identify_from_images(images)

    def shutdown(self):
        # Arrête proprement le pipeline.
        self.capture.disconnect()
        self._executor.shutdown(wait=False)


# FACTORY

def create_vision_pipeline(
    # Cree vision pipeline.
    database_path: Optional[str] = None,
    use_simulation: bool = False
) -> VisionPipeline:
    """
    Crée un pipeline de vision.

    Args:
        database_path: Chemin vers la base de données produits (JSON)
        use_simulation: Utiliser le mode simulation

    Returns:
        VisionPipeline configuré
    """
    import json

    # Charger la base de données (JSON export ou SQLite)
    database: Dict[str, Any] = {}
    resolved_path: Optional[Path] = Path(database_path) if database_path else None

    if resolved_path and resolved_path.exists():
        if resolved_path.suffix.lower() == ".db":
            try:
                from assistant.database import ProductDatabase
                db = ProductDatabase(str(resolved_path))
                database = {"products": [p.to_dict() for p in db.get_all_products()]}
            except Exception as e:
                print(f"[Vision] Erreur chargement SQLite {resolved_path}: {e}")
        else:
            with open(resolved_path, 'r', encoding='utf-8') as f:
                database = json.load(f)
    else:
        # Fallback principal: export JSON du projet
        project_root = Path(__file__).resolve().parents[3]
        default_candidates = [
            project_root / "data" / "products_export.json",
            Path(__file__).parent.parent / "poc" / "mock_database.json",
        ]
        for candidate in default_candidates:
            if candidate.exists():
                with open(candidate, 'r', encoding='utf-8') as f:
                    database = json.load(f)
                break

    return VisionPipeline(database, use_simulation=use_simulation)


# WRAPPER VISION MODULE (API PUBLIQUE)

class VisionModule:
    # Wrapper public pour intégration avec l'orchestrateur.

    def __init__(
        # Initialise l'objet.
        self,
        config: Optional[VisionConfig] = None,
        database_path: Optional[str] = None,
        use_simulation: Optional[bool] = None
    ):
        self.config = config or VisionConfig()
        self.use_simulation = bool(use_simulation) if use_simulation is not None else False

        # Appliquer les paramètres globaux avant création du pipeline
        self._apply_globals_from_config()

        self.pipeline = create_vision_pipeline(
            database_path=database_path,
            use_simulation=self.use_simulation
        )
        self._apply_capture_config()

    def _apply_globals_from_config(self) -> None:
        # Applique les seuils et le modèle VLM depuis la config.
        global MODEL_NAME, CONFIDENCE_HIGH, CONFIDENCE_MEDIUM, CONFIDENCE_LOW

        if getattr(self.config, "vlm_model", None):
            MODEL_NAME = self.config.vlm_model
        if getattr(self.config, "confidence_high", None) is not None:
            CONFIDENCE_HIGH = self.config.confidence_high
        if getattr(self.config, "confidence_medium", None) is not None:
            CONFIDENCE_MEDIUM = self.config.confidence_medium
        if getattr(self.config, "confidence_low", None) is not None:
            CONFIDENCE_LOW = self.config.confidence_low

    def _apply_capture_config(self) -> None:
        # Configure la capture multi-frames depuis la config.
        capture_cfg = self.pipeline.capture.config
        if getattr(self.config, "num_frames", None) is not None:
            capture_cfg.num_frames = self.config.num_frames
        if getattr(self.config, "capture_interval_ms", None) is not None:
            capture_cfg.frame_interval_ms = self.config.capture_interval_ms

    def load(self) -> bool:
        # Charge le modèle VLM (si nécessaire).
        return self.pipeline.load()

    def identify_product(
        # Gere product.
        self,
        images: Optional[Union[Image.Image, List[Image.Image]]] = None,
        image_paths: Optional[List[str]] = None
    ) -> VisionResult:
        """Identifie un produit à partir d'images ou de chemins."""
        if images is None and not image_paths:
            return VisionResult(
                success=False,
                confidence_level=ConfidenceLevel.FAILED,
                message="Aucune image fournie"
            )

        if isinstance(images, Image.Image):
            images = [images]

        if not self.pipeline.vlm.is_loaded:
            if not self.pipeline.load():
                return VisionResult(
                    success=False,
                    confidence_level=ConfidenceLevel.FAILED,
                    message="Chargement du modèle VLM échoué"
                )

        if image_paths:
            raw_result = self.pipeline.identify_from_paths(image_paths)
        else:
            raw_result = self.pipeline.identify_from_images(images or [])

        return self._to_vision_result(raw_result)

    def shutdown(self) -> None:
        # Arrête proprement le pipeline.
        self.pipeline.shutdown()

    def _to_vision_result(self, result: IdentificationResult) -> VisionResult:
        # Convertit IdentificationResult vers l'API publique.
        if not result.success:
            return VisionResult(
                success=False,
                confidence_level=ConfidenceLevel.FAILED,
                message=result.message,
                raw_result=result
            )

        predictions: List[ProductPrediction] = []

        if result.source == IdentificationSource.VLM_MEDIUM:
            for candidate in result.candidates:
                predictions.append(ProductPrediction(
                    product_id=candidate.product_id or None,
                    name=candidate.name,
                    brand=candidate.brand,
                    confidence=candidate.score,
                    source=candidate.source
                ))
            confidence_level = ConfidenceLevel.MEDIUM
        else:
            predictions.append(ProductPrediction(
                product_id=result.product_id or None,
                name=result.product_name or "",
                brand=result.brand or "",
                confidence=result.confidence,
                source=result.source.value
            ))
            if result.source == IdentificationSource.VLM_HIGH:
                confidence_level = ConfidenceLevel.HIGH
            elif result.source in (IdentificationSource.BARCODE, IdentificationSource.FALLBACK):
                confidence_level = ConfidenceLevel.HIGH
            else:
                confidence_level = ConfidenceLevel.LOW

        top_prediction = predictions[0] if predictions else None

        return VisionResult(
            success=True,
            confidence_level=confidence_level,
            top_prediction=top_prediction,
            predictions=predictions,
            message=result.message,
            raw_result=result
        )


# TEST

if __name__ == "__main__":
    print("=" * 70)
    print("TEST MODULE VISION - PHASE 5")
    print("=" * 70)

    # Créer pipeline en mode simulation
    pipeline = create_vision_pipeline(use_simulation=True)

    # Charger le modèle
    if pipeline.load():
        print("\n[1] TEST IDENTIFICATION SIMULÉE")
        print("-" * 50)

        # Simuler des images (en production, viendraient de Pepper)
        from PIL import Image
        test_images = [Image.new('RGB', (640, 480), color='white') for _ in range(3)]

        result = pipeline.identify_from_images(test_images)

        print(f"  Source: {result.source.value}")
        print(f"  Succès: {result.success}")
        print(f"  Produit: {result.product_name}")
        print(f"  Marque: {result.brand}")
        print(f"  Confiance: {result.confidence*100:.0f}%")
        print(f"  Temps total: {result.total_time_ms:.0f}ms")
        print(f"  Message: {result.message}")

        if result.candidates:
            print(f"\n  Top-3 candidats:")
            for i, c in enumerate(result.candidates, 1):
                print(f"    {i}. {c.name} ({c.score*100:.0f}%)")

        print("\n[2] TEST LOGIQUE D'ARBITRAGE")
        print("-" * 50)

        print(f"  BARCODE priorité: code-barres fiable (2+ images)")
        print(f"  VLM_HIGH: confiance >= {CONFIDENCE_HIGH*100:.0f}%")
        print(f"  VLM_MEDIUM: confiance {CONFIDENCE_MEDIUM*100:.0f}-{CONFIDENCE_HIGH*100:.0f}%")
        print(f"  FALLBACK: confiance < {CONFIDENCE_MEDIUM*100:.0f}% + code-barres")

    pipeline.shutdown()
    print("\n" + "=" * 70)
