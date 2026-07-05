# Projet AMS Pepper - Assistant IA en parapharmacie

Projet réalisé en Master, dans le cadre du projet AMS autour du robot Pepper.

Ce projet est un assistant conversationnel pour un contexte de parapharmacie. Il combine un backend Python, une interface tablette, un package Choregraphe pour Pepper, une base produits SQLite et des briques IA pour le dialogue, la voix, la vision et la sécurisation des réponses.

## Compétences mises en avant

- Architecture d'un système robotique complet autour de Pepper.
- Backend Python structuré avec configuration, orchestration et logs.
- Connexion robot/backend/tablette via réseau, TCP et WebSocket.
- Utilisation de l'API OpenAI Realtime, transcription et synthèse vocale.
- Vision produit avec modèle VLM et fallback par code-barres.
- Base produits SQLite et règles de sécurité métier.

## Fonctionnalités

- Dialogue vocal avec un utilisateur en magasin.
- Interface tablette pour afficher les produits et les étapes de l'échange.
- Identification produit par vision ou code-barres.
- Filtrage de sécurité pour éviter les conseils médicaux risqués.
- Mode simulation lorsque Pepper n'est pas disponible.
- Package Choregraphe avec ponts audio, vidéo et voix.

## Technologies

Python, SQLite, OpenAI Realtime API, Whisper, WebSocket, TCP, Pepper, Choregraphe, NAOqi, HTML, CSS, JavaScript, YAML.

## Structure

- `src/assistant/` : backend principal.
- `tablet/` : interface web pour la tablette Pepper.
- `choregraphe_package/` : package Choregraphe.
- `config/config.yaml` : configuration applicative.
- `data/` : base produits de démonstration et blacklist.

## Configuration

Créer un fichier `.env` à partir de l'exemple :

```bash
cp .env.example .env
```

Puis renseigner les valeurs nécessaires, notamment la clé OpenAI et l'adresse de Pepper si le robot est utilisé.

## Lancement

```bash
python3 -m pip install -r requirements.txt
./run_all.sh
```

Pour Choregraphe, ouvrir :

```text
choregraphe_package/Parapharmacie_OpenAI/Parapharmacie_OpenAI.pml
```

## Notes

La version publique ne contient pas de clé API. Les variables sensibles doivent rester dans `.env`, qui n'est pas versionné.
