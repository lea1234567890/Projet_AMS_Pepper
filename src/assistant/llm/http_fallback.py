#!/usr/bin/env python3
# Fallback OpenAI HTTP (sans Realtime WebSocket)

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = (
    "Tu es un assistant vocal de parapharmacie spécialisé cheveux. "
    "Réponds en français, 2 à 4 phrases max, ton clair et professionnel. "
    "Tu peux répondre aux questions capillaires courantes (cheveux gras, secs, "
    "pellicules, usage d'un shampooing, fréquence d'utilisation, comparaison de produits). "
    "Tu dois répondre DIRECTEMENT à la question posée; ne récite pas une fiche produit complète "
    "sauf si l'utilisateur le demande explicitement. "
    "Tu ne dois PAS faire de diagnostic, ni de prescription, ni de recommandation de traitement médical. "
    "Tu refuses uniquement les questions médicales explicites (maladie, médicament, ordonnance, posologie, traitement, interaction). "
    "En cas de question médicale explicite, réponds exactement: "
    "'Je ne peux pas répondre à cette question. Je vous invite à consulter le pharmacien.'"
)


class OpenAIHTTPFallbackClient:
    # Client de secours via API HTTP (Responses/Chat Completions).

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o-mini",
        temperature: float = 0.2,
    ):
        self.api_key = api_key
        self.model = model
        self.temperature = temperature
        self._trace = os.getenv("PEPPER_VOICE_TRACE", "1").strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _build_user_prompt(question: str, context: Optional[Dict[str, Any]]) -> str:
        ctx = context or {}
        current_product = (ctx.get("current_product") or "").strip()
        current_ean = (ctx.get("current_ean") or "").strip()
        lines = [f"Question utilisateur: {question.strip()}"]
        if current_product:
            lines.append(f"Produit courant identifié: {current_product}")
        if current_ean:
            lines.append(f"EAN courant: {current_ean}")
        lines.append(
            "Réponds brièvement pour une restitution vocale Pepper, "
            "et réponds précisément à la question avant tout."
        )
        return "\n".join(lines)

    @staticmethod
    def _extract_text_from_response(resp: Any) -> str:
        # SDK récent
        text = getattr(resp, "output_text", "") or ""
        if text:
            return text.strip()

        # Fallback parsing générique
        output = getattr(resp, "output", None)
        if not output:
            return ""
        chunks = []
        for item in output:
            for content in getattr(item, "content", []) or []:
                ctype = getattr(content, "type", "")
                if ctype in ("output_text", "text"):
                    value = getattr(content, "text", "") or ""
                    if value:
                        chunks.append(value)
        return " ".join(chunks).strip()

    @staticmethod
    def _is_medical_question(question: str) -> bool:
        q = str(question or "").lower()
        markers = (
            "médicament", "medicament", "ordonnance", "posologie", "traitement",
            "maladie", "psoriasis", "eczéma", "eczema", "dermatite",
            "diagnostic", "interaction", "contre-indication", "effet secondaire",
            "allergie", "allergique", "médecin", "medecin",
        )
        return any(m in q for m in markers)

    @staticmethod
    def _is_pharmacist_redirect(answer: str) -> bool:
        a = str(answer or "").lower()
        markers = (
            "consulter le pharmacien",
            "je ne peux pas répondre",
            "je ne suis pas habilité",
        )
        return any(m in a for m in markers)

    @staticmethod
    def _fallback_non_medical_answer(question: str) -> str:
        q = str(question or "").strip()
        if not q:
            return "Pouvez-vous reformuler votre question sur les cheveux ou un shampooing précis ?"
        return (
            "Je peux vous aider sur les questions capillaires non médicales. "
            "Pouvez-vous reformuler en précisant votre besoin (pellicules, cheveux gras, secs, "
            "ou le nom du shampooing) ?"
        )

    def ask(self, question: str, context: Optional[Dict[str, Any]] = None) -> str:
        question = (question or "").strip()
        if not question:
            return "Je n'ai pas reçu de question."

        if self._trace:
            preview = question if len(question) <= 180 else (question[:177] + "...")
            ctx = context or {}
            logger.info(
                "Voice trace: OpenAIHTTPFallback.ask "
                f"(model={self.model}, context_product={'on' if bool(ctx.get('current_product')) else 'off'}) "
                f"question={preview}"
            )

        from openai import OpenAI

        client = OpenAI(api_key=self.api_key)
        user_prompt = self._build_user_prompt(question, context)

        def _responses_call(extra_user_note: str = "") -> str:
            note = (extra_user_note or "").strip()
            user_content = user_prompt if not note else f"{user_prompt}\n{note}"
            resp = client.responses.create(
                model=self.model,
                temperature=self.temperature,
                max_output_tokens=220,
                input=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
            )
            return self._extract_text_from_response(resp)

        # Chemin principal: Responses API
        try:
            answer = _responses_call()
            if answer:
                # Anti-faux-refus: si le modèle redirige vers pharmacien sur une question non médicale,
                # on force une reformulation une seule fois.
                if self._is_pharmacist_redirect(answer) and not self._is_medical_question(question):
                    if self._trace:
                        logger.info(
                            "Voice trace: faux refus détecté (pharmacien) sur question non médicale, retry forcé"
                        )
                    retry = _responses_call(
                        "Important: la question est capillaire (non médicale). "
                        "Ne redirige pas vers pharmacien; réponds avec des conseils produit concrets."
                    )
                    if retry:
                        if self._trace:
                            preview = retry if len(retry) <= 220 else (retry[:217] + "...")
                            logger.info(f"Voice trace: réponse OpenAIHTTPFallback retry={preview}")
                        if self._is_pharmacist_redirect(retry):
                            return self._fallback_non_medical_answer(question)
                        return retry.strip()
                if self._trace:
                    preview = answer if len(answer) <= 220 else (answer[:217] + "...")
                    logger.info(f"Voice trace: réponse OpenAIHTTPFallback={preview}")
                if self._is_pharmacist_redirect(answer) and not self._is_medical_question(question):
                    return self._fallback_non_medical_answer(question)
                return answer
        except Exception as e:
            if self._trace:
                logger.warning(f"Voice trace: erreur Responses API: {e}")
            pass

        # Fallback legacy: Chat Completions
        try:
            resp = client.chat.completions.create(
                model=self.model,
                temperature=self.temperature,
                max_tokens=220,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
            )
            choices = getattr(resp, "choices", []) or []
            if choices:
                content = getattr(choices[0].message, "content", "") or ""
                if content:
                    if self._is_pharmacist_redirect(content) and not self._is_medical_question(question):
                        retry_resp = client.chat.completions.create(
                            model=self.model,
                            temperature=self.temperature,
                            max_tokens=220,
                            messages=[
                                {"role": "system", "content": SYSTEM_PROMPT},
                                {
                                    "role": "user",
                                    "content": (
                                        f"{user_prompt}\n"
                                        "Important: la question est capillaire (non médicale). "
                                        "Ne redirige pas vers pharmacien; réponds avec des conseils produit concrets."
                                    ),
                                },
                            ],
                        )
                        retry_choices = getattr(retry_resp, "choices", []) or []
                        if retry_choices:
                            retry_content = getattr(retry_choices[0].message, "content", "") or ""
                            if retry_content:
                                if self._trace:
                                    preview = retry_content if len(retry_content) <= 220 else (retry_content[:217] + "...")
                                    logger.info(f"Voice trace: réponse ChatCompletions retry={preview}")
                                if self._is_pharmacist_redirect(retry_content):
                                    return self._fallback_non_medical_answer(question)
                                return retry_content.strip()
                    if self._trace:
                        preview = content if len(content) <= 220 else (content[:217] + "...")
                        logger.info(f"Voice trace: réponse ChatCompletions={preview}")
                    if self._is_pharmacist_redirect(content) and not self._is_medical_question(question):
                        return self._fallback_non_medical_answer(question)
                    return content.strip()
        except Exception as e:
            if self._trace:
                logger.warning(f"Voice trace: erreur ChatCompletions: {e}")
            pass

        return (
            "Je n'arrive pas à générer une réponse pour le moment. "
            "Veuillez réessayer dans quelques instants."
        )
