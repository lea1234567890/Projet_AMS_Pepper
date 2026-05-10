# Phase 10 - Serveur WebSocket pour Interface Tablette Pepper

import asyncio
import json
import logging
import argparse
from dataclasses import dataclass, asdict
from datetime import datetime
from enum import Enum
from typing import Optional, Dict, Any, Set, Callable
from pathlib import Path

# WebSocket server
try:
    import websockets
    from websockets.server import WebSocketServerProtocol
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False
    print("⚠️  websockets non installé: pip install websockets")


@dataclass
class ServerConfig:
    # Configuration du serveur WebSocket.
    host: str = "0.0.0.0"
    port: int = 8765
    ping_interval: float = 30.0
    ping_timeout: float = 10.0
    max_message_size: int = 1024 * 1024  # 1 MB
    log_level: str = "INFO"



class MessageType(Enum):
    # Types de messages échangés.
    # Tablette → Serveur
    COMMAND = "command"

    # Serveur → Tablette
    PRODUCT_IDENTIFIED = "product_identified"
    TOP3_RESULTS = "top3_results"
    BARCODE_DETECTED = "barcode_detected"
    BARCODE_FAILED = "barcode_failed"
    SECURITY_ALERT = "security_alert"
    ERROR = "error"
    SHOW_SCREEN = "show_screen"
    PRODUCTS_LIST = "products_list"
    STATUS = "status"
    QA_ANSWER = "qa_answer"
    VOICE_STATUS = "voice_status"


class TabletCommand(Enum):
    # Commandes envoyées par la tablette.
    START_VISUAL_SCAN = "start_visual_scan"
    START_BARCODE_SCAN = "start_barcode_scan"
    CONFIRM_PRODUCT = "confirm_product"
    DENY_PRODUCT = "deny_product"
    SELECT_TOP3 = "select_top3"
    GET_PRODUCTS = "get_products"
    GO_HOME = "go_home"
    ASK_QUESTION = "ask_question"



@dataclass
class Product:
    # Représentation d'un produit.
    ean: str
    name: str
    brand: str = ""
    price: float = 0.0
    usage: str = ""
    hair_type: str = ""
    image: str = ""

    def to_dict(self) -> Dict[str, Any]:
        # Gere dict.
        return asdict(self)


@dataclass
class Top3Result:
    # Résultat de classification Top-3.
    ean: str
    name: str
    confidence: float
    image: str = ""

    def to_dict(self) -> Dict[str, Any]:
        # Gere dict.
        return asdict(self)



class TabletServer:
    # Serveur WebSocket pour la communication avec la tablette Pepper.

    def __init__(self, config: Optional[ServerConfig] = None):
        # Initialise l'objet.
        self.config = config or ServerConfig()
        self.clients: Set[WebSocketServerProtocol] = set()
        self.handlers: Dict[str, Callable] = {}
        self.running = False

        # Logging
        logging.basicConfig(
            level=getattr(logging, self.config.log_level),
            format='%(asctime)s [%(levelname)s] %(message)s'
        )
        self.logger = logging.getLogger(__name__)

        # Enregistrer les handlers par défaut
        self._register_default_handlers()

    def _register_default_handlers(self):
        # Enregistre les handlers par défaut pour les commandes.
        self.handlers = {
            TabletCommand.START_VISUAL_SCAN.value: self._handle_visual_scan,
            TabletCommand.START_BARCODE_SCAN.value: self._handle_barcode_scan,
            TabletCommand.CONFIRM_PRODUCT.value: self._handle_confirm_product,
            TabletCommand.DENY_PRODUCT.value: self._handle_deny_product,
            TabletCommand.SELECT_TOP3.value: self._handle_select_top3,
            TabletCommand.GET_PRODUCTS.value: self._handle_get_products,
            TabletCommand.GO_HOME.value: self._handle_go_home,
            TabletCommand.ASK_QUESTION.value: self._handle_ask_question,
        }

    def register_handler(self, command: str, handler: Callable):
        # Enregistre un handler personnalisé pour une commande.
        self.handlers[command] = handler
        self.logger.info(f"Handler enregistré pour: {command}")

    async def start(self):
        # Démarre le serveur WebSocket.
        if not WEBSOCKETS_AVAILABLE:
            self.logger.error("websockets non disponible")
            return

        self.running = True
        self.logger.info(f"Démarrage serveur WebSocket sur {self.config.host}:{self.config.port}")

        async with websockets.serve(
            self._handle_connection,
            self.config.host,
            self.config.port,
            ping_interval=self.config.ping_interval,
            ping_timeout=self.config.ping_timeout,
            max_size=self.config.max_message_size
        ):
            self.logger.info("Serveur WebSocket démarré ✓")
            await asyncio.Future()  # Run forever

    async def stop(self):
        # Arrête le serveur.
        self.running = False
        # Fermer toutes les connexions
        for client in self.clients:
            await client.close()
        self.clients.clear()
        self.logger.info("Serveur arrêté")

    async def _handle_connection(self, websocket: WebSocketServerProtocol):
        # Gère une nouvelle connexion WebSocket.
        self.clients.add(websocket)
        client_addr = websocket.remote_address
        self.logger.info(f"Nouvelle connexion: {client_addr}")

        try:
            # Envoyer un message de bienvenue
            await self._send_to_client(websocket, {
                "type": MessageType.STATUS.value,
                "status": "connected",
                "message": "Connecté au serveur Pepper"
            })

            # Écouter les messages
            async for message in websocket:
                await self._handle_message(websocket, message)

        except websockets.exceptions.ConnectionClosed:
            self.logger.info(f"Connexion fermée: {client_addr}")
        except Exception as e:
            self.logger.error(f"Erreur connexion {client_addr}: {e}")
        finally:
            self.clients.discard(websocket)

    async def _handle_message(self, websocket: WebSocketServerProtocol, raw_message: str):
        # Traite un message reçu de la tablette.
        try:
            message = json.loads(raw_message)
            self.logger.debug(f"Message reçu: {message}")

            if message.get("type") != MessageType.COMMAND.value:
                return

            command = message.get("command")
            data = message.get("data", {})

            # Appeler le handler approprié
            handler = self.handlers.get(command)
            if handler:
                await handler(websocket, data)
            else:
                self.logger.warning(f"Commande inconnue: {command}")
                await self._send_error(websocket, f"Commande inconnue: {command}")

        except json.JSONDecodeError:
            self.logger.error("Message JSON invalide")
            await self._send_error(websocket, "Format de message invalide")
        except Exception as e:
            self.logger.error(f"Erreur traitement message: {e}")
            await self._send_error(websocket, str(e))


    async def _handle_visual_scan(self, websocket, data: Dict):
        # Handler pour démarrage du scan visuel.
        self.logger.info("Démarrage scan visuel demandé")
        # Ici, déclencher le pipeline de vision
        # Pour la démo, on simule un résultat après 2s
        await asyncio.sleep(2.0)

        # Simuler un résultat Top-3
        await self.send_top3_results(websocket, [
            Top3Result("3282770149272", "Shampooing Extra-Doux Klorane", 0.82),
            Top3Result("3600523735501", "Elseve Color-Vive", 0.68),
            Top3Result("3600542154796", "Ultra Doux Miel Garnier", 0.54),
        ])

    async def _handle_barcode_scan(self, websocket, data: Dict):
        # Handler pour démarrage du scan code-barres.
        self.logger.info("Démarrage scan code-barres demandé")
        # Ici, activer le scanner code-barres

    async def _handle_confirm_product(self, websocket, data: Dict):
        # Handler pour confirmation d'un produit.
        ean = data.get("ean")
        self.logger.info(f"Produit confirmé: {ean}")
        # Ici, enregistrer la confirmation et récupérer les détails

    async def _handle_deny_product(self, websocket, data: Dict):
        # Handler pour refus d'un produit.
        self.logger.info("Produit refusé, passage au scan code-barres")
        await self._send_to_client(websocket, {
            "type": MessageType.SHOW_SCREEN.value,
            "screen": "barcode-scan"
        })

    async def _handle_select_top3(self, websocket, data: Dict):
        # Handler pour sélection dans le Top-3.
        index = data.get("index", 0)
        self.logger.info(f"Sélection Top-3: index {index}")

    async def _handle_get_products(self, websocket, data: Dict):
        # Handler pour récupération de la liste des produits.
        self.logger.info("Liste des produits demandée")
        # Ici, récupérer depuis la base de données

    async def _handle_go_home(self, websocket, data: Dict):
        # Handler pour retour à l'accueil.
        self.logger.info("Retour à l'accueil")
        await self._send_to_client(websocket, {
            "type": MessageType.SHOW_SCREEN.value,
            "screen": "home"
        })

    async def _handle_ask_question(self, websocket, data: Dict):
        # Handler par défaut: surchargé par l'assistant principal si fallback activé.
        self.logger.info("Question fallback reçue (handler par défaut)")
        await self._send_error(
            websocket,
            "Mode question fallback non configuré côté assistant."
        )


    async def _send_to_client(self, websocket: WebSocketServerProtocol, message: Dict):
        # Envoie un message à un client spécifique.
        try:
            message["timestamp"] = datetime.now().isoformat()
            await websocket.send(json.dumps(message))
        except Exception as e:
            self.logger.error(f"Erreur envoi: {e}")

    async def _send_error(self, websocket: WebSocketServerProtocol, message: str):
        # Envoie un message d'erreur.
        await self._send_to_client(websocket, {
            "type": MessageType.ERROR.value,
            "message": message
        })

    async def broadcast(self, message: Dict):
        # Envoie un message à tous les clients connectés.
        message["timestamp"] = datetime.now().isoformat()
        data = json.dumps(message)

        for client in self.clients:
            try:
                await client.send(data)
            except Exception as e:
                self.logger.error(f"Erreur broadcast: {e}")


    async def send_product_identified(self, websocket_or_broadcast, product: Product):
        # Envoie les informations d'un produit identifié.
        message = {
            "type": MessageType.PRODUCT_IDENTIFIED.value,
            "product": product.to_dict()
        }

        if websocket_or_broadcast is True:
            await self.broadcast(message)
        else:
            await self._send_to_client(websocket_or_broadcast, message)

    async def send_top3_results(self, websocket_or_broadcast, results: list):
        # Envoie les résultats Top-3.
        message = {
            "type": MessageType.TOP3_RESULTS.value,
            "results": [r.to_dict() if isinstance(r, Top3Result) else r for r in results]
        }

        if websocket_or_broadcast is True:
            await self.broadcast(message)
        else:
            await self._send_to_client(websocket_or_broadcast, message)

    async def send_barcode_detected(self, websocket_or_broadcast, ean: str, product: Optional[Product] = None):
        # Envoie la notification de code-barres détecté.
        message = {
            "type": MessageType.BARCODE_DETECTED.value,
            "ean": ean,
            "product": product.to_dict() if product else None
        }

        if websocket_or_broadcast is True:
            await self.broadcast(message)
        else:
            await self._send_to_client(websocket_or_broadcast, message)

    async def send_barcode_failed(self, websocket_or_broadcast):
        # Envoie la notification d'échec de scan code-barres.
        message = {
            "type": MessageType.BARCODE_FAILED.value
        }

        if websocket_or_broadcast is True:
            await self.broadcast(message)
        else:
            await self._send_to_client(websocket_or_broadcast, message)

    async def send_security_alert(self, websocket_or_broadcast, title: str, alert_message: str):
        # Envoie une alerte de sécurité.
        message = {
            "type": MessageType.SECURITY_ALERT.value,
            "title": title,
            "message": alert_message
        }

        if websocket_or_broadcast is True:
            await self.broadcast(message)
        else:
            await self._send_to_client(websocket_or_broadcast, message)

    async def send_show_screen(self, websocket_or_broadcast, screen: str):
        # Envoie une commande pour afficher un écran.
        message = {
            "type": MessageType.SHOW_SCREEN.value,
            "screen": screen
        }

        if websocket_or_broadcast is True:
            await self.broadcast(message)
        else:
            await self._send_to_client(websocket_or_broadcast, message)

    async def send_products_list(self, websocket_or_broadcast, products: list):
        # Envoie la liste des produits.
        message = {
            "type": MessageType.PRODUCTS_LIST.value,
            "products": [p.to_dict() if isinstance(p, Product) else p for p in products]
        }

        if websocket_or_broadcast is True:
            await self.broadcast(message)
        else:
            await self._send_to_client(websocket_or_broadcast, message)

    async def send_qa_answer(
        self,
        websocket_or_broadcast,
        question: str,
        answer: str,
        recommendations: Optional[list] = None,
        context_mode: str = "general",
    ):
        # Envoie une réponse au mode question fallback.
        message = {
            "type": MessageType.QA_ANSWER.value,
            "question": question,
            "answer": answer,
            "recommendations": recommendations or [],
            "context_mode": str(context_mode or "general"),
        }
        if websocket_or_broadcast is True:
            await self.broadcast(message)
        else:
            await self._send_to_client(websocket_or_broadcast, message)

    async def send_voice_status(
        self,
        websocket_or_broadcast,
        status: str,
        message_text: str,
        title: str = "Question vocale",
    ):
        # Envoie l'état du mode vocal (écoute, traitement, terminé, erreur).
        message = {
            "type": MessageType.VOICE_STATUS.value,
            "status": str(status or "").strip().lower(),
            "title": title,
            "message": message_text,
        }
        if websocket_or_broadcast is True:
            await self.broadcast(message)
        else:
            await self._send_to_client(websocket_or_broadcast, message)



class TabletBridge:
    # Pont entre l'orchestrateur et la tablette.

    def __init__(self, server: TabletServer):
        # Initialise l'objet.
        self.server = server

    async def show_product(self, product: Product):
        # Affiche un produit sur la tablette.
        await self.server.send_product_identified(True, product)

    async def show_top3(self, results: list):
        # Affiche les résultats Top-3 sur la tablette.
        await self.server.send_top3_results(True, results)

    async def show_security_alert(self, title: str, message: str):
        # Affiche une alerte de sécurité sur la tablette.
        await self.server.send_security_alert(True, title, message)

    async def show_screen(self, screen: str):
        # Change l'écran affiché sur la tablette.
        await self.server.send_show_screen(True, screen)

    async def notify_barcode(self, ean: str, product: Optional[Product] = None):
        # Notifie la détection d'un code-barres.
        await self.server.send_barcode_detected(True, ean, product)



async def test_server(config: Optional[ServerConfig] = None):
    # Test du serveur WebSocket.
    print("=" * 60)
    print("TEST SERVEUR WEBSOCKET TABLETTE")
    print("=" * 60)

    config = config or ServerConfig(
        host="localhost",
        port=8765,
        log_level="DEBUG"
    )

    server = TabletServer(config)

    print(f"\n[1] Démarrage serveur sur ws://{config.host}:{config.port}")
    print("    Connectez la tablette ou ouvrez index.html dans un navigateur")
    print("    Appuyez sur Ctrl+C pour arrêter\n")

    try:
        await server.start()
    except KeyboardInterrupt:
        print("\n\nArrêt du serveur...")
        await server.stop()

    print("\n✅ Test terminé")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Serveur WebSocket pour tablette Pepper")
    parser.add_argument("--host", default="0.0.0.0", help="Adresse d'écoute")
    parser.add_argument("--port", type=int, default=8765, help="Port d'écoute")
    parser.add_argument("--debug", action="store_true", help="Mode debug")

    args = parser.parse_args()

    config = ServerConfig(
        host=args.host,
        port=args.port,
        log_level="DEBUG" if args.debug else "INFO"
    )

    asyncio.run(test_server(config))
