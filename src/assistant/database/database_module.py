#!/usr/bin/env python3
# Phase 6 - Module Base de Données Produits

import os
import re
import sqlite3
import json
import time
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict, Any, Tuple
from contextlib import contextmanager
from difflib import SequenceMatcher


# STRUCTURES DE DONNÉES

@dataclass
class Product:
    # Produit capillaire.
    id: str
    ean13: str
    name: str
    brand: str
    category: str
    hair_type: List[str] = field(default_factory=list)
    price: float = 0.0
    currency: str = "EUR"
    volume: str = ""
    usage: str = ""
    instructions: str = ""
    precautions: List[str] = field(default_factory=list)
    benefits: List[str] = field(default_factory=list)
    key_ingredients: List[Dict[str, str]] = field(default_factory=list)
    certifications: List[str] = field(default_factory=list)
    photo_url: str = ""
    natural_origin_percentage: Optional[int] = None
    age_restriction: str = ""
    keywords: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        # Convertit en dictionnaire.
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Product':
        # Crée depuis un dictionnaire.
        return cls(
            id=data.get('id', ''),
            ean13=data.get('ean13', ''),
            name=data.get('name', ''),
            brand=data.get('brand', ''),
            category=data.get('category', ''),
            hair_type=data.get('hair_type', []),
            price=data.get('price', 0.0),
            currency=data.get('currency', 'EUR'),
            volume=data.get('volume', ''),
            usage=data.get('usage', ''),
            instructions=data.get('instructions', ''),
            precautions=data.get('precautions', []),
            benefits=data.get('benefits', []),
            key_ingredients=data.get('key_ingredients', []),
            certifications=data.get('certifications', []),
            photo_url=data.get('photo_url', ''),
            natural_origin_percentage=data.get('natural_origin_percentage'),
            age_restriction=data.get('age_restriction', ''),
            keywords=data.get('keywords', [])
        )


@dataclass
class SearchResult:
    # Résultat de recherche.
    product: Product
    score: float  # Score de pertinence (0-1)
    match_type: str  # "exact", "fuzzy", "keyword"


# BASE DE DONNÉES

class ProductDatabase:
    # Base de données SQLite pour les produits capillaires.

    # Schéma de la base de données
    SCHEMA = """
    -- Table principale des produits
    CREATE TABLE IF NOT EXISTS products (
        id TEXT PRIMARY KEY,
        ean13 TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL,
        brand TEXT NOT NULL,
        category TEXT NOT NULL,
        hair_type TEXT,  -- JSON array
        price REAL DEFAULT 0.0,
        currency TEXT DEFAULT 'EUR',
        volume TEXT,
        usage TEXT,
        instructions TEXT,
        precautions TEXT,  -- JSON array
        benefits TEXT,  -- JSON array
        key_ingredients TEXT,  -- JSON array
        certifications TEXT,  -- JSON array
        photo_url TEXT,
        natural_origin_percentage INTEGER,
        age_restriction TEXT,
        keywords TEXT,  -- JSON array
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    -- Index pour recherche rapide
    CREATE INDEX IF NOT EXISTS idx_products_ean13 ON products(ean13);
    CREATE INDEX IF NOT EXISTS idx_products_category ON products(category);
    CREATE INDEX IF NOT EXISTS idx_products_brand ON products(brand);

    -- Table de liste noire EAN (médicaments)
    CREATE TABLE IF NOT EXISTS blacklist_ean (
        ean13 TEXT PRIMARY KEY,
        description TEXT,
        reason TEXT DEFAULT 'medicament',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    -- Index pour blacklist
    CREATE INDEX IF NOT EXISTS idx_blacklist_ean ON blacklist_ean(ean13);

    -- Table des préfixes blacklistés
    CREATE TABLE IF NOT EXISTS blacklist_prefixes (
        prefix TEXT PRIMARY KEY,
        description TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    -- Vue pour recherche full-text (simulation)
    CREATE TABLE IF NOT EXISTS search_index (
        product_id TEXT PRIMARY KEY,
        search_text TEXT,  -- Texte combiné pour recherche
        FOREIGN KEY (product_id) REFERENCES products(id)
    );

    CREATE INDEX IF NOT EXISTS idx_search_text ON search_index(search_text);
    """

    def __init__(self, db_path: str = "products.db"):
        # Initialise la base de données.
        self.db_path = db_path
        self._init_database()

    def _init_database(self):
        # Initialise le schéma de la base.
        with self._get_connection() as conn:
            conn.executescript(self.SCHEMA)
            conn.commit()

    @contextmanager
    def _get_connection(self):
        # Context manager pour connexion SQLite.
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    # CRUD PRODUITS

    def insert_product(self, product: Product) -> bool:
        # Insère un produit dans la base.
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()

                # Insérer produit
                cursor.execute("""
                    INSERT OR REPLACE INTO products (
                        id, ean13, name, brand, category, hair_type, price,
                        currency, volume, usage, instructions, precautions,
                        benefits, key_ingredients, certifications, photo_url,
                        natural_origin_percentage, age_restriction, keywords,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """, (
                    product.id,
                    product.ean13,
                    product.name,
                    product.brand,
                    product.category,
                    json.dumps(product.hair_type),
                    product.price,
                    product.currency,
                    product.volume,
                    product.usage,
                    product.instructions,
                    json.dumps(product.precautions),
                    json.dumps(product.benefits),
                    json.dumps(product.key_ingredients),
                    json.dumps(product.certifications),
                    product.photo_url,
                    product.natural_origin_percentage,
                    product.age_restriction,
                    json.dumps(product.keywords)
                ))

                # Mettre à jour l'index de recherche
                search_text = self._build_search_text(product)
                cursor.execute("""
                    INSERT OR REPLACE INTO search_index (product_id, search_text)
                    VALUES (?, ?)
                """, (product.id, search_text))

                conn.commit()
                return True

        except Exception as e:
            print(f"[DB] Erreur insertion produit: {e}")
            return False

    def _build_search_text(self, product: Product) -> str:
        # Construit le texte de recherche pour un produit.
        parts = [
            product.name.lower(),
            product.brand.lower(),
            product.category.lower(),
            ' '.join(product.hair_type).lower(),
            ' '.join(product.keywords).lower(),
            product.usage.lower() if product.usage else ''
        ]
        return ' '.join(parts)

    def get_by_id(self, product_id: str) -> Optional[Product]:
        # Récupère un produit par son ID.
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM products WHERE id = ?", (product_id,))
            row = cursor.fetchone()

            if row:
                return self._row_to_product(row)
            return None

    def get_by_ean(self, ean13: str) -> Optional[Product]:
        # Récupère un produit par son code EAN-13.
        start_time = time.time()

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM products WHERE ean13 = ?", (ean13,))
            row = cursor.fetchone()

            elapsed_ms = (time.time() - start_time) * 1000

            if elapsed_ms > 10:
                print(f"[DB] ATTENTION: Recherche EAN lente ({elapsed_ms:.1f}ms > 10ms)")

            if row:
                return self._row_to_product(row)
            return None

    def get_all_products(self) -> List[Product]:
        # Récupère tous les produits.
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM products ORDER BY brand, name")
            rows = cursor.fetchall()
            return [self._row_to_product(row) for row in rows]

    def _row_to_product(self, row: sqlite3.Row) -> Product:
        # Convertit une ligne SQLite en Product.
        return Product(
            id=row['id'],
            ean13=row['ean13'],
            name=row['name'],
            brand=row['brand'],
            category=row['category'],
            hair_type=json.loads(row['hair_type'] or '[]'),
            price=row['price'] or 0.0,
            currency=row['currency'] or 'EUR',
            volume=row['volume'] or '',
            usage=row['usage'] or '',
            instructions=row['instructions'] or '',
            precautions=json.loads(row['precautions'] or '[]'),
            benefits=json.loads(row['benefits'] or '[]'),
            key_ingredients=json.loads(row['key_ingredients'] or '[]'),
            certifications=json.loads(row['certifications'] or '[]'),
            photo_url=row['photo_url'] or '',
            natural_origin_percentage=row['natural_origin_percentage'],
            age_restriction=row['age_restriction'] or '',
            keywords=json.loads(row['keywords'] or '[]')
        )

    def delete_product(self, product_id: str) -> bool:
        # Supprime un produit.
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM products WHERE id = ?", (product_id,))
                cursor.execute("DELETE FROM search_index WHERE product_id = ?", (product_id,))
                conn.commit()
                return cursor.rowcount > 0
        except:
            return False

    def count_products(self) -> int:
        # Compte le nombre de produits.
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM products")
            return cursor.fetchone()[0]

    # RECHERCHE

    def search_fuzzy(
        # Gere fuzzy.
        self,
        query: str,
        limit: int = 10,
        min_score: float = 0.3
    ) -> List[SearchResult]:
        """
        Recherche fuzzy par nom/marque.

        Args:
            query: Texte de recherche
            limit: Nombre max de résultats
            min_score: Score minimum (0-1)

        Returns:
            Liste de SearchResult triée par score
        """
        query_lower = query.lower().strip()
        if not query_lower:
            return []

        results = []

        with self._get_connection() as conn:
            cursor = conn.cursor()

            # Récupérer tous les produits avec leur texte de recherche
            cursor.execute("""
                SELECT p.*, s.search_text
                FROM products p
                LEFT JOIN search_index s ON p.id = s.product_id
            """)

            for row in cursor.fetchall():
                product = self._row_to_product(row)
                search_text = row['search_text'] or ''

                # Calculer le score
                score = self._calculate_fuzzy_score(query_lower, product, search_text)

                if score >= min_score:
                    match_type = "exact" if score > 0.9 else "fuzzy"
                    results.append(SearchResult(
                        product=product,
                        score=score,
                        match_type=match_type
                    ))

        # Trier par score décroissant
        results.sort(key=lambda x: x.score, reverse=True)

        return results[:limit]

    def _calculate_fuzzy_score(
        # Gere fuzzy score.
        self,
        query: str,
        product: Product,
        search_text: str
    ) -> float:
        """Calcule le score de pertinence fuzzy."""
        scores = []

        # Score sur nom complet
        full_name = f"{product.brand} {product.name}".lower()
        scores.append(SequenceMatcher(None, query, full_name).ratio() * 1.5)

        # Score sur nom seul
        scores.append(SequenceMatcher(None, query, product.name.lower()).ratio() * 1.2)

        # Score sur marque
        scores.append(SequenceMatcher(None, query, product.brand.lower()).ratio())

        # Score sur mots-clés
        for keyword in product.keywords:
            if query in keyword.lower() or keyword.lower() in query:
                scores.append(0.8)

        # Score sur texte de recherche complet
        if search_text:
            if query in search_text:
                scores.append(0.7)

        # Bonus si tous les mots de la requête sont présents
        query_words = set(query.split())
        search_words = set(search_text.split()) if search_text else set()
        if query_words and query_words.issubset(search_words):
            scores.append(0.9)

        return min(1.0, max(scores) if scores else 0.0)

    def search_by_category(self, category: str) -> List[Product]:
        # Filtre par catégorie.
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM products WHERE LOWER(category) = LOWER(?) ORDER BY brand, name",
                (category,)
            )
            return [self._row_to_product(row) for row in cursor.fetchall()]

    def search_by_hair_type(self, hair_type: str) -> List[Product]:
        # Filtre par type de cheveux.
        with self._get_connection() as conn:
            cursor = conn.cursor()
            # Recherche dans le JSON array
            cursor.execute(
                "SELECT * FROM products WHERE hair_type LIKE ? ORDER BY brand, name",
                (f'%{hair_type}%',)
            )
            return [self._row_to_product(row) for row in cursor.fetchall()]

    def search_by_brand(self, brand: str) -> List[Product]:
        # Filtre par marque.
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM products WHERE LOWER(brand) = LOWER(?) ORDER BY name",
                (brand,)
            )
            return [self._row_to_product(row) for row in cursor.fetchall()]

    def get_categories(self) -> List[str]:
        # Retourne toutes les catégories distinctes.
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT DISTINCT category FROM products ORDER BY category")
            return [row[0] for row in cursor.fetchall()]

    def get_brands(self) -> List[str]:
        # Retourne toutes les marques distinctes.
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT DISTINCT brand FROM products ORDER BY brand")
            return [row[0] for row in cursor.fetchall()]

    # BLACKLIST

    def is_blacklisted(self, ean13: str) -> Tuple[bool, str]:
        # Vérifie si un EAN est blacklisté.
        with self._get_connection() as conn:
            cursor = conn.cursor()

            # Vérifier EAN exact
            cursor.execute(
                "SELECT description, reason FROM blacklist_ean WHERE ean13 = ?",
                (ean13,)
            )
            row = cursor.fetchone()
            if row:
                return True, row['reason'] or 'medicament'

            # Vérifier préfixes
            cursor.execute("SELECT prefix, description FROM blacklist_prefixes")
            for prefix_row in cursor.fetchall():
                if ean13.startswith(prefix_row['prefix']):
                    return True, f"Préfixe médicament ({prefix_row['prefix']})"

            return False, ""

    def add_to_blacklist(self, ean13: str, description: str = "", reason: str = "medicament") -> bool:
        # Ajoute un EAN à la liste noire.
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT OR REPLACE INTO blacklist_ean (ean13, description, reason) VALUES (?, ?, ?)",
                    (ean13, description, reason)
                )
                conn.commit()
                return True
        except:
            return False

    def add_blacklist_prefix(self, prefix: str, description: str = "") -> bool:
        # Ajoute un préfixe à la liste noire.
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT OR REPLACE INTO blacklist_prefixes (prefix, description) VALUES (?, ?)",
                    (prefix, description)
                )
                conn.commit()
                return True
        except:
            return False

    def load_blacklist_from_json(self, json_path: str) -> int:
        # Charge la blacklist depuis un fichier JSON.
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            count = 0

            # Charger les EAN
            for item in data.get('ean_codes', []):
                if self.add_to_blacklist(
                    item.get('ean13', ''),
                    item.get('description', ''),
                    item.get('reason', 'medicament')
                ):
                    count += 1

            # Charger les préfixes
            for item in data.get('prefixes', []):
                if self.add_blacklist_prefix(
                    item.get('prefix', ''),
                    item.get('description', '')
                ):
                    count += 1

            return count

        except Exception as e:
            print(f"[DB] Erreur chargement blacklist: {e}")
            return 0

    def count_blacklist(self) -> int:
        # Compte les entrées dans la blacklist.
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM blacklist_ean")
            ean_count = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM blacklist_prefixes")
            prefix_count = cursor.fetchone()[0]
            return ean_count + prefix_count

    # IMPORT/EXPORT

    def import_from_json(self, json_path: str) -> int:
        # Importe des produits depuis un fichier JSON.
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            count = 0
            products = data.get('products', [])

            for p_data in products:
                product = Product.from_dict(p_data)
                if self.insert_product(product):
                    count += 1

            return count

        except Exception as e:
            print(f"[DB] Erreur import JSON: {e}")
            return 0

    def export_to_json(self, json_path: str) -> bool:
        # Exporte tous les produits vers un fichier JSON.
        try:
            products = self.get_all_products()
            data = {
                "metadata": {
                    "version": "2.0",
                    "total_products": len(products),
                    "exported_at": time.strftime("%Y-%m-%d %H:%M:%S")
                },
                "products": [p.to_dict() for p in products]
            }

            with open(json_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

            return True

        except Exception as e:
            print(f"[DB] Erreur export JSON: {e}")
            return False

    # STATISTIQUES

    def get_stats(self) -> Dict[str, Any]:
        # Retourne les statistiques de la base.
        with self._get_connection() as conn:
            cursor = conn.cursor()

            # Nombre de produits
            cursor.execute("SELECT COUNT(*) FROM products")
            total_products = cursor.fetchone()[0]

            # Par catégorie
            cursor.execute("""
                SELECT category, COUNT(*) as count
                FROM products GROUP BY category ORDER BY count DESC
            """)
            by_category = {row['category']: row['count'] for row in cursor.fetchall()}

            # Par marque
            cursor.execute("""
                SELECT brand, COUNT(*) as count
                FROM products GROUP BY brand ORDER BY count DESC
            """)
            by_brand = {row['brand']: row['count'] for row in cursor.fetchall()}

            # Prix moyen
            cursor.execute("SELECT AVG(price) FROM products WHERE price > 0")
            avg_price = cursor.fetchone()[0] or 0

            # Blacklist
            cursor.execute("SELECT COUNT(*) FROM blacklist_ean")
            blacklist_count = cursor.fetchone()[0]

            return {
                "total_products": total_products,
                "by_category": by_category,
                "by_brand": by_brand,
                "average_price": round(avg_price, 2),
                "blacklist_count": blacklist_count
            }


# TEST

if __name__ == "__main__":
    import tempfile

    print("=" * 70)
    print("TEST MODULE DATABASE - PHASE 6")
    print("=" * 70)

    # Créer base temporaire pour test
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        test_db_path = f.name

    db = ProductDatabase(test_db_path)

    # Test insertion
    print("\n[1] TEST INSERTION")
    print("-" * 50)

    test_product = Product(
        id="TEST-001",
        ean13="3282770149272",
        name="Shampooing Test Camomille",
        brand="Klorane",
        category="Shampooing",
        hair_type=["Cheveux blonds", "Cheveux méchés"],
        price=12.90,
        volume="400ml",
        usage="Shampooing éclaircissant",
        keywords=["klorane", "camomille", "blond"]
    )

    success = db.insert_product(test_product)
    print(f"  Insertion: {'OK' if success else 'ECHEC'}")

    # Test recherche EAN
    print("\n[2] TEST RECHERCHE EAN")
    print("-" * 50)

    start = time.time()
    found = db.get_by_ean("3282770149272")
    elapsed = (time.time() - start) * 1000

    print(f"  Trouvé: {found.name if found else 'Non'}")
    print(f"  Temps: {elapsed:.2f}ms")
    print(f"  Objectif (<10ms): {'OK' if elapsed < 10 else 'ECHEC'}")

    # Test recherche fuzzy
    print("\n[3] TEST RECHERCHE FUZZY")
    print("-" * 50)

    results = db.search_fuzzy("klorane camomille")
    print(f"  Résultats: {len(results)}")
    for r in results:
        print(f"    - {r.product.name} (score: {r.score:.2f})")

    # Test blacklist
    print("\n[4] TEST BLACKLIST")
    print("-" * 50)

    db.add_to_blacklist("3400930000000", "Doliprane", "medicament")
    db.add_blacklist_prefix("340", "Médicaments France CIP-13")

    is_bl, reason = db.is_blacklisted("3400930000000")
    print(f"  EAN blacklisté: {is_bl} ({reason})")

    is_bl, reason = db.is_blacklisted("3400999999999")
    print(f"  Préfixe blacklisté: {is_bl} ({reason})")

    is_bl, reason = db.is_blacklisted("3282770149272")
    print(f"  EAN produit: {is_bl}")

    # Stats
    print("\n[5] STATISTIQUES")
    print("-" * 50)

    stats = db.get_stats()
    for key, value in stats.items():
        print(f"  {key}: {value}")

    # Nettoyage
    os.unlink(test_db_path)

    print("\n" + "=" * 70)
