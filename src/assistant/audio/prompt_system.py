#!/usr/bin/env python3
# Phase 4 - Prompt Système Parapharmacie

from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any
from enum import Enum


# CONTEXTE PRODUIT

@dataclass
class ProductContext:
    # Contexte du produit actuellement présenté.
    name: str = ""
    brand: str = ""
    category: str = ""
    price: str = ""
    volume: str = ""
    ean13: str = ""

    # Détails
    description: str = ""
    usage: str = ""
    hair_type: List[str] = field(default_factory=list)
    benefits: List[str] = field(default_factory=list)
    key_ingredients: List[str] = field(default_factory=list)
    precautions: List[str] = field(default_factory=list)

    def to_prompt_text(self) -> str:
        # Génère le texte à injecter dans le prompt.
        if not self.name:
            return "Aucun produit sélectionné actuellement."

        lines = [
            f"PRODUIT ACTUEL: {self.name}",
            f"Marque: {self.brand}" if self.brand else None,
            f"Catégorie: {self.category}" if self.category else None,
            f"Prix: {self.price}" if self.price else None,
            f"Volume: {self.volume}" if self.volume else None,
        ]

        if self.description:
            lines.append(f"Description: {self.description}")

        if self.usage:
            lines.append(f"Utilisation: {self.usage}")

        if self.hair_type:
            lines.append(f"Types de cheveux: {', '.join(self.hair_type)}")

        if self.benefits:
            lines.append(f"Bénéfices: {', '.join(self.benefits)}")

        if self.key_ingredients:
            lines.append(f"Ingrédients clés: {', '.join(self.key_ingredients)}")

        if self.precautions:
            lines.append(f"Précautions: {', '.join(self.precautions)}")

        return "\n".join(line for line in lines if line)

    @classmethod
    def from_database(cls, product_dict: Dict[str, Any]) -> 'ProductContext':
        # Crée un contexte depuis un dict de la base de données.
        return cls(
            name=product_dict.get('name', ''),
            brand=product_dict.get('brand', ''),
            category=product_dict.get('category', ''),
            price=f"{product_dict.get('price', '')} {product_dict.get('currency', 'EUR')}",
            volume=product_dict.get('volume', ''),
            ean13=product_dict.get('ean13', ''),
            description=product_dict.get('usage', ''),
            usage=product_dict.get('instructions', ''),
            hair_type=product_dict.get('hair_type', []),
            benefits=product_dict.get('benefits', []),
            key_ingredients=[i.get('name', '') for i in product_dict.get('key_ingredients', [])],
            precautions=product_dict.get('precautions', [])
        )


# PROMPTS SYSTÈME

class PromptStyle(Enum):
    # Styles de réponse.
    CONCISE = "concise"        # 2-3 phrases max
    DETAILED = "detailed"      # Réponses plus longues
    FRIENDLY = "friendly"      # Ton amical
    PROFESSIONAL = "professional"  # Ton professionnel


# Prompt principal parapharmacie
PROMPT_BASE = """Tu es un assistant vocal pour une parapharmacie, spécialisé dans les produits capillaires.
Tu es incarné par le robot Pepper et tu parles directement aux clients du magasin.

RÈGLES STRICTES:
1. Tu réponds UNIQUEMENT aux questions sur les produits capillaires et la parapharmacie.
2. Tu NE DONNES JAMAIS de conseil médical, diagnostic, ou recommandation de traitement.
3. Si on te pose une question médicale (symptômes, maladies, médicaments), réponds UNIQUEMENT:
   "Je ne suis pas habilité à répondre à cette question. Je vous invite à consulter le pharmacien."
4. Tes réponses doivent être COURTES et CONCISES car tu es un assistant vocal.
5. Tu parles en français avec un ton {tone}.
6. Tu tutoies le client pour créer une relation de proximité.

CE QUE TU PEUX FAIRE:
- Donner des informations sur le produit présenté (prix, utilisation, ingrédients)
- Expliquer comment utiliser un produit capillaire
- Indiquer le type de cheveux adapté
- Comparer avec d'autres produits de la gamme

CE QUE TU NE DOIS PAS FAIRE:
- Donner des conseils médicaux
- Parler de symptômes, maladies, allergies
- Recommander des médicaments
- Faire des diagnostics
- Parler de sujets hors parapharmacie

FORMAT DES RÉPONSES:
- Maximum {max_sentences} phrases
- Langage simple et accessible
- Pas de termes techniques complexes
- Vouvoiement poli mais accessible"""


PROMPT_MEDICAL_FILTER = """
FILTRE MÉDICAL STRICT:
Si la question contient l'un de ces termes, refuse poliment et redirige vers le pharmacien:
- Symptômes: douleur, mal, démangeaison, irritation, rougeur, chute, perte
- Conditions: allergie, eczéma, psoriasis, pellicules sévères, alopécie
- Médical: traitement, médicament, ordonnance, diagnostic, maladie
- Urgence: urgence, grave, SAMU, médecin

Réponse type: "Cette question dépasse mes compétences. Je te conseille de demander au pharmacien qui pourra t'aider."
"""


PROMPT_GREETING = """
SALUTATIONS:
- Au premier contact: "Bonjour ! Je suis Pepper, l'assistant du rayon capillaire. Comment puis-je t'aider ?"
- Si produit détecté: "Je vois que tu as un {product_category}. Tu veux des informations dessus ?"
"""


PROMPT_PRODUCT_CONTEXT = """
{product_info}

Utilise ces informations pour répondre aux questions sur ce produit.
Si on te demande des infos non disponibles, dis-le honnêtement.
"""


PROMPT_NO_PRODUCT = """
AUCUN PRODUIT SÉLECTIONNÉ:
Si aucun produit n'est présenté, propose ton aide:
"Je ne vois pas de produit pour l'instant. Tu peux me présenter un article et je te donnerai des informations dessus !"
"""


# GESTIONNAIRE DE PROMPT

class PromptManager:
    # Gestionnaire de prompt avec injection de contexte dynamique.

    def __init__(self, style: PromptStyle = PromptStyle.CONCISE):
        # Initialise l'objet.
        self.style = style
        self.product_context: Optional[ProductContext] = None
        self._custom_instructions: List[str] = []

        # Configuration selon le style
        self._style_config = {
            PromptStyle.CONCISE: {"tone": "professionnel mais accessible", "max_sentences": 2},
            PromptStyle.DETAILED: {"tone": "professionnel et informatif", "max_sentences": 4},
            PromptStyle.FRIENDLY: {"tone": "amical et chaleureux", "max_sentences": 3},
            PromptStyle.PROFESSIONAL: {"tone": "professionnel et formel", "max_sentences": 3}
        }

    def set_product_context(self, context: Optional[ProductContext]):
        # Définit le contexte produit actuel.
        self.product_context = context

    def clear_product_context(self):
        # Efface le contexte produit.
        self.product_context = None

    def add_custom_instruction(self, instruction: str):
        # Ajoute une instruction personnalisée.
        self._custom_instructions.append(instruction)

    def clear_custom_instructions(self):
        # Efface les instructions personnalisées.
        self._custom_instructions.clear()

    def get_full_prompt(self) -> str:
        # Génère le prompt complet avec contexte.
        config = self._style_config[self.style]

        # Construire le prompt
        parts = []

        # 1. Prompt de base
        parts.append(PROMPT_BASE.format(
            tone=config["tone"],
            max_sentences=config["max_sentences"]
        ))

        # 2. Filtre médical
        parts.append(PROMPT_MEDICAL_FILTER)

        # 3. Contexte produit
        if self.product_context and self.product_context.name:
            product_info = self.product_context.to_prompt_text()
            parts.append(PROMPT_PRODUCT_CONTEXT.format(product_info=product_info))
        else:
            parts.append(PROMPT_NO_PRODUCT)

        # 4. Instructions personnalisées
        if self._custom_instructions:
            parts.append("\nINSTRUCTIONS ADDITIONNELLES:")
            for instruction in self._custom_instructions:
                parts.append(f"- {instruction}")

        return "\n\n".join(parts)

    def get_greeting_prompt(self) -> str:
        # Retourne le prompt pour les salutations.
        if self.product_context and self.product_context.category:
            return f"Bonjour ! Je vois que tu as un {self.product_context.category}. Tu veux des informations dessus ?"
        return "Bonjour ! Je suis Pepper, l'assistant du rayon capillaire. Comment puis-je t'aider ?"


# PROMPTS PRÉDÉFINIS

def get_parapharmacie_prompt(product: Optional[Dict[str, Any]] = None) -> str:
    # Crée un prompt parapharmacie prêt à l'emploi.
    manager = PromptManager(style=PromptStyle.CONCISE)

    if product:
        context = ProductContext.from_database(product)
        manager.set_product_context(context)

    return manager.get_full_prompt()


def get_demo_prompt() -> str:
    # Retourne un prompt pour la démo avec un produit fictif.
    manager = PromptManager(style=PromptStyle.FRIENDLY)

    # Produit exemple
    demo_product = ProductContext(
        name="Shampooing Illuminateur à la Camomille",
        brand="Klorane",
        category="Shampooing",
        price="12.90€",
        volume="400ml",
        description="Shampooing éclaircissant pour raviver les reflets dorés",
        hair_type=["Cheveux blonds", "Cheveux méchés"],
        benefits=["Reflets dorés naturels", "Illumine les cheveux blonds"],
        key_ingredients=["Extrait de Camomille"]
    )

    manager.set_product_context(demo_product)
    return manager.get_full_prompt()


# RÉPONSES TYPES

RESPONSE_TEMPLATES = {
    "medical_redirect": "Je ne suis pas habilité à répondre à cette question. Je te conseille de demander au pharmacien.",

    "no_product": "Je ne vois pas de produit pour l'instant. Présente-moi un article et je te donnerai des infos !",

    "unknown_info": "Je n'ai pas cette information pour ce produit. Tu peux regarder sur l'emballage ou demander au pharmacien.",

    "price_info": "Ce produit est à {price}.",

    "usage_info": "Pour l'utiliser : {usage}",

    "hair_type_info": "Ce produit est adapté pour {hair_types}.",

    "goodbye": "Au revoir ! N'hésite pas si tu as d'autres questions sur nos produits capillaires."
}


def get_response_template(template_key: str, **kwargs) -> str:
    # Retourne une réponse type formatée.
    template = RESPONSE_TEMPLATES.get(template_key, "")
    if kwargs:
        return template.format(**kwargs)
    return template


# TEST

if __name__ == "__main__":
    print("=" * 70)
    print("PROMPT SYSTÈME PARAPHARMACIE - PHASE 4")
    print("=" * 70)

    # Test sans produit
    print("\n[1] PROMPT SANS PRODUIT\n")
    print("-" * 50)
    manager = PromptManager()
    prompt = manager.get_full_prompt()
    print(prompt[:500] + "...\n")

    # Test avec produit
    print("\n[2] PROMPT AVEC PRODUIT\n")
    print("-" * 50)

    product = ProductContext(
        name="Elsève Hyaluron Repulp",
        brand="L'Oréal Paris",
        category="Shampooing",
        price="6.90€",
        volume="250ml",
        description="Shampooing re-hydratant pour cheveux déshydratés",
        hair_type=["Cheveux déshydratés", "Cheveux fatigués"],
        benefits=["Hydratation 72h", "Cheveux repulpés"],
        key_ingredients=["Acide Hyaluronique"]
    )

    manager.set_product_context(product)
    prompt = manager.get_full_prompt()
    print(prompt[:800] + "...\n")

    # Test greeting
    print("\n[3] SALUTATION\n")
    print("-" * 50)
    print(manager.get_greeting_prompt())

    # Test réponses types
    print("\n[4] RÉPONSES TYPES\n")
    print("-" * 50)
    print(f"Redirection médicale: {get_response_template('medical_redirect')}")
    print(f"Prix: {get_response_template('price_info', price='12.90€')}")

    print("\n" + "=" * 70)
