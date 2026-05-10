# Projet AMS Pepper (Open AI)

Il contient:
- 1 package Choregraphe
- backend Python complet
- tablette web
- base produits
- scripts de lancement

## Choregraphe

Ouvrir ce fichier:

- `choregraphe_package/Parapharmacie_OpenAI/Parapharmacie_OpenAI.pml`

Le comportement contient 3 boxes organisees:
- `AudioBridge` : micro Pepper -> backend TCP
- `VideoBridge` : camera Pepper -> backend TCP
- `TTSControl` : backend -> voix Pepper

## Ou est la cle OpenAI

Dans ce fichier :

- `.env`

Exemple de base:

- `.env.example`

## Base de donnees et tablette

Elles sont incluses dans ce dossier:
- `data/products.db`
- `tablet/index.html`
- `tablet/app_legacy_ui.js`
- `tablet/styles.css`

## Lancement (mode complet)

1. Installer les deps

```bash
python3 -m pip install -r requirements.txt
```

2. Creer `.env`

```bash
cp .env.example .env
# puis editer .env (OPENAI_API_KEY, PEPPER_IP)
```

3. Lancer backend + tablette

```bash
./run_all.sh
```

4. Ouvrir Choregraphe
- ouvrir `choregraphe_package/Parapharmacie_OpenAI/Parapharmacie_OpenAI.pml`
- dans `AudioBridge` et `VideoBridge`, mettre `MAC_IP = <IP_DE_TON_PC>`
- Run

## Scripts

- `run_backend.sh` : backend complet (`assistant.main`)
- `run_tablet.sh` : serveur web tablette
- `run_all.sh` : lance tablette + backend
