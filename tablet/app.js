/**
 * Phase 10 - Interface Tablette Pepper
 * Application JavaScript pour l'interface tactile
 */

const CONFIG = {
    // URL du serveur WS: query param ?ws=... > host page > localhost
    serverUrl: (() => {
        let wsParam = '';
        try {
            wsParam = new URLSearchParams(window.location.search).get('ws') || '';
        } catch (e) {
            wsParam = '';
        }
        if (wsParam) return wsParam;

        const host = window.location.hostname || 'localhost';
        const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws';
        return `${protocol}://${host}:8765`;
    })(),
    textQuestionEnabled: (() => {
        try {
            return (new URLSearchParams(window.location.search).get('textq') || '') === '1';
        } catch (e) {
            return false;
        }
    })(),

    // Timeouts (0 => pas de timeout côté tablette)
    connectionTimeout: 5000,
    scanTimeout: (() => {
        try {
            const v = Number(new URLSearchParams(window.location.search).get('scan_timeout_ms') || '0');
            return Number.isFinite(v) && v > 0 ? v : 0;
        } catch (e) {
            return 0;
        }
    })(),
    voiceListenDurationS: (() => {
        try {
            const v = Number(new URLSearchParams(window.location.search).get('voice_duration_s') || '3600');
            return Number.isFinite(v) ? Math.max(3, v) : 3600;
        } catch (e) {
            return 3600;
        }
    })(),
    placeholderImage: 'placeholder.png',

    // Debug
    debug: true
};

const AppState = {
    currentScreen: 'home',
    connected: false,
    ws: null,
    products: [],
    filteredProducts: [],
    currentProduct: null,
    top3Results: [],
    currentFilter: 'all',
    voiceRecording: false,
    scanInProgress: false,
    productLockedUntil: 0
};

const App = {
    /**
     * Initialisation de l'application
     */
    init() {
        this.log('Initialisation de l\'application...');

        // Charger les produits de démonstration
        this.loadDemoProducts();

        // Tenter la connexion WebSocket
        this.connectWebSocket();

        // Le secours question écrite reste masqué sauf activation explicite.
        this.configureTextQuestionButton();

        // Afficher l'écran d'accueil
        this.showScreen('home');

        this.log('Application initialisée');
    },

    configureTextQuestionButton() {
        const btn = document.getElementById('text-question-btn');
        if (!btn) return;
        btn.style.display = CONFIG.textQuestionEnabled ? '' : 'none';
    },

    /**
     * Affichage d'un écran
     */
    showScreen(screenId) {
        this.log(`Affichage écran: ${screenId}`);

        // Masquer tous les écrans
        document.querySelectorAll('.screen').forEach(screen => {
            screen.classList.remove('active');
        });

        // Afficher l'écran demandé
        const screen = document.getElementById(`screen-${screenId}`);
        if (screen) {
            screen.classList.add('active');
            AppState.currentScreen = screenId;
            if (screenId === 'barcode-scan') {
                this.updateBarcodeStatus('waiting', 'En attente du code-barres...');
            }
            if (screenId === 'advice') {
                this.requestProducts();
            }
        }
    },

    normalizeText(value) {
        return String(value || '')
            .normalize('NFD')
            .replace(/[\u0300-\u036f]/g, '')
            .toLowerCase();
    },

    toAbsoluteUrl(value) {
        const text = String(value || '').trim();
        if (!text) return '';
        if (/^(https?:|wss?:|data:|blob:|file:|\/)/i.test(text)) {
            return text;
        }
        try {
            return new URL(text, window.location.href).toString();
        } catch (e) {
            return text;
        }
    },

    createProductPlaceholder() {
        return this.toAbsoluteUrl(CONFIG.placeholderImage);
    },

    resolveImageUrl(imageUrl, product = null) {
        const value = String(imageUrl || '').trim();
        const isGenericPlaceholder = (
            !value
            || value === CONFIG.placeholderImage
            || value.endsWith('/placeholder.svg')
            || value.endsWith('/placeholder.png')
        );
        if (isGenericPlaceholder) {
            return this.createProductPlaceholder(product || {});
        }
        return this.toAbsoluteUrl(value);
    },

    inferProductCategories(product) {
        const haystack = this.normalizeText([
            product.hair_type || '',
            product.usage || '',
            product.name || '',
            product.brand || ''
        ].join(' '));

        const categories = [];
        if (/(sec|deshydrat|hydrat|nourri|nutrition)/.test(haystack)) categories.push('secs');
        if (/(gras|sebo|seborr)/.test(haystack)) categories.push('gras');
        if (/(tous types|tout type|normal|usage quotidien|frequent|doux)/.test(haystack)) categories.push('normaux');
        if (/(color|mech)/.test(haystack)) categories.push('colores');
        if (/(pellic|anti[ -]?pellic|dermite|ds\+?)/.test(haystack)) categories.push('pellicules');
        if (/(sensible|reactif|hypersens|irrit|delicat)/.test(haystack)) categories.push('sensibles');
        if (/(abime|fragil|repar|casse|chute|fortifi)/.test(haystack)) categories.push('abimes');
        if (categories.length === 0) categories.push('autres');
        return categories;
    },

    /**
     * Affichage de l'écran de chargement
     */
    showLoading(message = 'Chargement...') {
        document.getElementById('loading-text').textContent = message;
        this.showScreen('loading');
    },

    setScanInProgress(inProgress) {
        AppState.scanInProgress = Boolean(inProgress);
    },

    setVoiceRecordingState(recording) {
        AppState.voiceRecording = Boolean(recording);
        const startButtons = ['voice-start-btn', 'product-voice-start-btn'];
        const sendButtons = ['voice-send-btn', 'product-voice-send-btn'];
        startButtons.forEach((id) => {
            const el = document.getElementById(id);
            if (el) el.style.display = recording ? 'none' : '';
        });
        sendButtons.forEach((id) => {
            const el = document.getElementById(id);
            if (el) el.style.display = recording ? '' : 'none';
        });
    },

    updateVoiceTranscriptPreview(text) {
        const value = String(text || '').trim();
        ['voice-transcript-preview', 'product-voice-transcript-preview'].forEach((id) => {
            const el = document.getElementById(id);
            if (el) el.textContent = value;
        });
    },

    /**
     * Démarrer le scan visuel
     */
    startVisualScan() {
        this.log('Démarrage scan visuel');
        if (AppState.scanInProgress) {
            this.log('Scan déjà en cours, action ignorée');
            return;
        }
        this.setScanInProgress(true);
        this.showLoading('Analyse du produit en cours...');

        // Envoyer commande au serveur
        const sent = this.sendCommand('start_visual_scan');
        if (!sent) {
            this.setScanInProgress(false);
            this.showScreen('scan-choice');
            this.showError('Connexion tablette indisponible.');
            return;
        }
    },

    /**
     * Démarrer le scan code-barres
     */
    startBarcodeScan() {
        this.log('Démarrage scan code-barres');
        if (AppState.scanInProgress) {
            this.log('Scan déjà en cours, action ignorée');
            return;
        }
        this.setScanInProgress(true);
        this.showScreen('barcode-scan');
        this.updateBarcodeStatus('waiting', 'Recherche du code-barres...');

        const sent = this.sendCommand('start_barcode_scan');
        if (!sent) {
            this.setScanInProgress(false);
            this.showError('Connexion tablette indisponible.');
            return;
        }
    },

    /**
     * Démarrer une question vocale fallback
     */
    startVoiceQuestion() {
        this.log('Démarrage question vocale fallback');
        const useProductContext = (AppState.currentScreen === 'product' && !!AppState.currentProduct);
        const sent = this.sendCommand('start_voice_question', {
            duration_s: CONFIG.voiceListenDurationS,
            manual_send: true,
            use_product_context: useProductContext
        });
        if (!sent) {
            this.showError('Connexion tablette indisponible.');
            return;
        }
        this.setVoiceRecordingState(true);
        this.updateVoiceTranscriptPreview('Micro activé. Parlez puis appuyez sur « Envoyer la question ».');
    },

    stopVoiceQuestion() {
        if (!AppState.voiceRecording) {
            return;
        }
        const sent = this.sendCommand('stop_voice_question', { manual_send: true });
        if (!sent) {
            this.showError('Connexion tablette indisponible.');
            return;
        }
        this.setVoiceRecordingState(false);
        this.showLoading('Envoi de la question en cours...');
    },

    askTextQuestion() {
        if (!CONFIG.textQuestionEnabled) {
            return;
        }
        const question = window.prompt('Entrez votre question sur le shampooing :');
        const text = String(question || '').trim();
        if (!text) {
            return;
        }
        const sent = this.sendCommand('ask_question', { question: text });
        if (!sent) {
            this.showError('Connexion tablette indisponible.');
            return;
        }
        this.showLoading('Question envoyée à Pepper...');
    },

    /**
     * Afficher les résultats Top-3
     */
    showTop3(results) {
        this.log('Affichage Top-3', results);
        this.setScanInProgress(false);

        AppState.top3Results = results;
        const container = document.getElementById('top3-cards');
        container.innerHTML = '';

        results.forEach((result, index) => {
            const card = document.createElement('div');
            card.className = 'top3-card';
            card.onclick = () => this.selectTop3Product(index);

            card.innerHTML = `
                <div class="top3-card-number">${index + 1}</div>
                <img class="top3-card-image" src="${this.resolveImageUrl(result.image, result)}" alt="${result.name}">
                <p class="top3-card-name">${result.name}</p>
                <p class="top3-card-confidence">${Math.round(result.confidence * 100)}% de confiance</p>
            `;
            const img = card.querySelector('.top3-card-image');
            if (img) {
                img.onerror = () => {
                    img.onerror = null;
                    img.src = this.resolveImageUrl('', result);
                };
            }

            container.appendChild(card);
        });

        this.showScreen('top3');
    },

    /**
     * Sélectionner un produit du Top-3
     */
    selectTop3Product(index) {
        const result = AppState.top3Results[index];
        if (result) {
            this.log(`Produit sélectionné: ${result.name}`);
            this.sendCommand('confirm_product', { ean: result.ean, index: index });
            this.showLoading('Validation du produit...');
        }
    },

    /**
     * Afficher une fiche produit
     */
    showProduct(product) {
        this.log('Affichage produit', product);
        this.setScanInProgress(false);

        AppState.currentProduct = product;
        AppState.productLockedUntil = Date.now() + 15000;

        const image = document.getElementById('product-image');
        image.src = this.resolveImageUrl(product.image, product);
        image.onerror = () => {
            image.onerror = null;
            image.src = this.resolveImageUrl('', product);
        };
        document.getElementById('product-name').textContent = product.name || 'Produit inconnu';
        document.getElementById('product-brand').textContent = product.brand || '';
        document.getElementById('product-price').textContent = product.price ? `${product.price.toFixed(2)} €` : 'Prix non disponible';
        document.getElementById('product-usage').textContent = product.usage || 'Pas d\'information disponible';
        document.getElementById('product-hair-type').textContent = product.hair_type || 'Tous types';

        this.showScreen('product');
    },

    /**
     * Filtrer les produits par type de cheveux
     */
    filterProducts(filter) {
        this.log(`Filtre: ${filter}`);
        AppState.currentFilter = filter;

        // Mettre à jour les boutons de filtre
        document.querySelectorAll('.filter-btn').forEach(btn => {
            btn.classList.toggle('active', btn.dataset.filter === filter);
        });

        // Filtrer les produits
        if (filter === 'all') {
            AppState.filteredProducts = [...AppState.products];
        } else {
            AppState.filteredProducts = AppState.products.filter((p) => {
                const categories = Array.isArray(p._categories)
                    ? p._categories
                    : this.inferProductCategories(p);
                p._categories = categories;
                return categories.includes(filter);
            });
        }

        // Afficher les produits filtrés
        this.renderProductsGrid();
    },

    /**
     * Rendre la grille de produits
     */
    renderProductsGrid() {
        const container = document.getElementById('products-grid');
        container.innerHTML = '';

        AppState.filteredProducts.forEach(product => {
            const item = document.createElement('div');
            item.className = 'product-grid-item';
            item.onclick = () => this.showProduct(product);

            item.innerHTML = `
                <img src="${this.resolveImageUrl(product.image, product)}" alt="${product.name}">
                <p class="name">${product.name}</p>
                <p class="brand">${product.brand || ''}</p>
                <p class="price">${product.price ? product.price.toFixed(2) + ' €' : ''}</p>
            `;
            const img = item.querySelector('img');
            if (img) {
                img.onerror = () => {
                    img.onerror = null;
                    img.src = this.resolveImageUrl('', product);
                };
            }

            container.appendChild(item);
        });
    },

    showAdviceAnswer(question, answer, recommendations = []) {
        const answerBox = document.getElementById('advice-answer-box');
        const answerText = document.getElementById('advice-answer-text');
        if (answerBox && answerText) {
            answerText.textContent = String(answer || '').trim() || 'Réponse vide';
            answerBox.style.display = '';
        }

        const recoSection = document.getElementById('advice-reco-section');
        const recoGrid = document.getElementById('advice-reco-grid');
        if (recoSection && recoGrid) {
            recoGrid.innerHTML = '';
            const items = Array.isArray(recommendations) ? recommendations : [];
            if (items.length > 0) {
                items.forEach((product) => {
                    const p = { ...(product || {}) };
                    if (Array.isArray(p.hair_type)) {
                        p.hair_type = p.hair_type.join(', ');
                    }
                    const item = document.createElement('div');
                    item.className = 'product-grid-item';
                    item.onclick = () => this.showProduct(p);
                    item.innerHTML = `
                        <img src="${this.resolveImageUrl(p.image, p)}" alt="${p.name || 'Produit'}">
                        <p class="name">${p.name || 'Produit'}</p>
                        <p class="brand">${p.brand || ''}</p>
                        <p class="price">${p.price ? p.price.toFixed(2) + ' €' : ''}</p>
                    `;
                    const img = item.querySelector('img');
                    if (img) {
                        img.onerror = () => {
                            img.onerror = null;
                            img.src = this.resolveImageUrl('', p);
                        };
                    }
                    recoGrid.appendChild(item);
                });
                recoSection.style.display = '';
            } else {
                recoSection.style.display = 'none';
            }
        }

        this.showScreen('advice');
    },

    /**
     * Afficher un message de sécurité
     */
    showSecurityMessage(title, message) {
        this.log('Message sécurité', { title, message });

        document.getElementById('security-title').textContent = title;
        document.getElementById('security-message').textContent = message;
        this.showScreen('security');
    },

    /**
     * Afficher une erreur
     */
    showError(message) {
        this.showSecurityMessage('Erreur', message);
    },

    /**
     * Mettre à jour le statut du scan code-barres
     */
    updateBarcodeStatus(status, message) {
        const icon = document.getElementById('barcode-status-icon');
        const text = document.getElementById('barcode-status-text');

        icon.className = 'status-icon ' + status;

        switch (status) {
            case 'waiting':
                icon.textContent = '⏳';
                break;
            case 'success':
                icon.textContent = '✅';
                break;
            case 'error':
                icon.textContent = '❌';
                break;
        }

        text.textContent = message;
    },


    /**
     * Connexion WebSocket au serveur Mac
     */
    connectWebSocket() {
        this.log(`Connexion WebSocket à ${CONFIG.serverUrl}...`);

        try {
            AppState.ws = new WebSocket(CONFIG.serverUrl);

            AppState.ws.onopen = () => {
                this.log('WebSocket connecté');
                AppState.connected = true;
                this.updateConnectionStatus('Connecté');
                this.requestProducts();
            };

            AppState.ws.onclose = () => {
                this.log('WebSocket déconnecté');
                AppState.connected = false;
                this.setVoiceRecordingState(false);
                this.setScanInProgress(false);
                this.updateConnectionStatus('Déconnecté');

                // Tentative de reconnexion après 5s
                setTimeout(() => this.connectWebSocket(), 5000);
            };

            AppState.ws.onerror = (error) => {
                this.log('Erreur WebSocket', error);
                this.updateConnectionStatus('Erreur');
            };

            AppState.ws.onmessage = (event) => {
                this.handleServerMessage(event.data);
            };

        } catch (error) {
            this.log('Erreur création WebSocket', error);
        }
    },

    /**
     * Envoyer une commande au serveur
     */
    sendCommand(command, data = {}) {
        if (!AppState.connected) {
            this.log('Non connecté, commande ignorée:', command);
            return false;
        }

        const message = JSON.stringify({
            type: 'command',
            command: command,
            data: data,
            timestamp: Date.now()
        });

        this.log('Envoi commande:', message);
        AppState.ws.send(message);
        return true;
    },

    /**
     * Traiter un message du serveur
     */
    handleServerMessage(rawMessage) {
        try {
            const message = JSON.parse(rawMessage);
            this.log('Message reçu:', message);

            switch (message.type) {
                case 'product_identified':
                    this.showProduct(message.product);
                    break;

                case 'top3_results':
                    this.showTop3(message.results);
                    break;

                case 'barcode_detected':
                    this.setScanInProgress(false);
                    this.updateBarcodeStatus('success', `Code-barres détecté: ${message.ean}`);
                    if (message.product) {
                        this.showProduct(message.product);
                    }
                    break;

                case 'barcode_failed':
                    this.setScanInProgress(false);
                    this.updateBarcodeStatus('error', 'Code-barres non reconnu');
                    break;

                case 'security_alert':
                    this.setScanInProgress(false);
                    this.showSecurityMessage(message.title, message.message);
                    break;

                case 'error':
                    if (String(message.message || '').toLowerCase().includes('scan déjà en cours')) {
                        this.log('Erreur non bloquante ignorée:', message.message);
                        break;
                    }
                    this.setScanInProgress(false);
                    this.showError(message.message);
                    break;

                case 'show_screen':
                    {
                        const nextScreen = message.screen || 'home';
                        if (nextScreen === 'home' || nextScreen === 'scan-choice') {
                            this.setScanInProgress(false);
                        }
                        if (
                            AppState.currentScreen === 'product'
                            && Date.now() < (AppState.productLockedUntil || 0)
                            && nextScreen !== 'home'
                            && nextScreen !== 'product'
                        ) {
                            this.log('show_screen ignoré (fiche produit verrouillée):', nextScreen);
                        } else {
                            this.showScreen(nextScreen);
                        }
                    }
                    break;

                case 'products_list':
                    AppState.products = (message.products || []).map((p) => {
                        const product = { ...(p || {}) };
                        if (Array.isArray(product.hair_type)) {
                            product.hair_type = product.hair_type.join(', ');
                        }
                        product._categories = this.inferProductCategories(product);
                        product.image = this.resolveImageUrl(product.image, product);
                        return product;
                    });
                    this.filterProducts('all');
                    break;

                case 'status':
                    this.updateConnectionStatus(
                        message.status === 'connected'
                            ? 'Connecté'
                            : (message.status || 'Inconnu')
                    );
                    break;

                case 'qa_answer':
                    if (
                        String(message.context_mode || '').toLowerCase() === 'product'
                        && AppState.currentProduct
                    ) {
                        const preview = document.getElementById('product-voice-transcript-preview');
                        if (preview) {
                            preview.textContent = `Réponse: ${message.answer || 'Réponse vide'}`;
                        }
                        this.showScreen('product');
                    } else {
                        this.showAdviceAnswer(
                            message.question || '',
                            message.answer || 'Réponse vide',
                            message.recommendations || []
                        );
                    }
                    break;

                case 'voice_status': {
                    const status = String(message.status || '').toLowerCase();
                    const title = message.title || 'Question vocale';
                    const text = message.message || '';
                    if (status === 'listening') {
                        this.setVoiceRecordingState(true);
                        this.updateVoiceTranscriptPreview(text || 'Micro activé. Parlez puis appuyez sur « Envoyer la question ».');
                    } else if (status === 'processing') {
                        this.showLoading(text || 'Traitement vocal en cours...');
                    } else if (status === 'timeout' || status === 'error') {
                        this.setVoiceRecordingState(false);
                        this.showSecurityMessage(title, text || "Je n'ai pas bien entendu.");
                    } else if (status === 'transcript') {
                        this.log('Transcription vocale reçue (masquée).');
                    } else if (status === 'done' && AppState.currentScreen === 'loading') {
                        this.setVoiceRecordingState(false);
                        this.showScreen(AppState.currentProduct ? 'product' : 'advice');
                    }
                    break;
                }

                default:
                    this.log('Message non géré:', message.type);
            }

        } catch (error) {
            this.log('Erreur parsing message:', error);
        }
    },

    /**
     * Mettre à jour le statut WebSocket affiché
     */
    updateConnectionStatus(statusText) {
        const statusEl = document.getElementById('ws-status');
        if (statusEl) {
            statusEl.textContent = `WebSocket: ${statusText}`;
        }
    },

    requestProducts() {
        const sent = this.sendCommand('get_products', { shampoo_only: true, limit: 200 });
        if (!sent) {
            this.log('Demande produits ignorée: WS non connectée');
        }
    },


    /**
     * Logger avec horodatage
     */
    log(...args) {
        if (CONFIG.debug) {
            console.log(`[${new Date().toISOString()}]`, ...args);
        }
    },

    /**
     * Charger des produits de démonstration
     */
    loadDemoProducts() {
        AppState.products = [
            {
                ean: '3282770149272',
                name: 'Shampooing Extra-Doux Lait d\'Avoine',
                brand: 'Klorane',
                price: 9.50,
                usage: 'Shampooing doux pour usage fréquent. Appliquer sur cheveux mouillés, masser et rincer.',
                hair_type: 'Tous types',
                image: CONFIG.placeholderImage
            },
            {
                ean: '3600523735501',
                name: 'Elseve Color-Vive Shampooing',
                brand: 'L\'Oréal Paris',
                price: 4.90,
                usage: 'Protection couleur pour cheveux colorés. Usage quotidien possible.',
                hair_type: 'Cheveux colorés',
                image: CONFIG.placeholderImage
            },
            {
                ean: '3600542154796',
                name: 'Ultra Doux Shampooing Miel',
                brand: 'Garnier',
                price: 3.80,
                usage: 'Shampooing nourrissant au miel. Répare et renforce les cheveux.',
                hair_type: 'Cheveux secs',
                image: CONFIG.placeholderImage
            },
            {
                ean: '3282779354424',
                name: 'Forticea Shampooing Énergisant',
                brand: 'René Furterer',
                price: 14.90,
                usage: 'Stimule la croissance des cheveux. Usage 2-3 fois par semaine.',
                hair_type: 'Cheveux fragilisés',
                image: CONFIG.placeholderImage
            },
            {
                ean: '3282770107357',
                name: 'Elution Shampooing Dermo-Protecteur',
                brand: 'Ducray',
                price: 11.90,
                usage: 'Rééquilibre le cuir chevelu. Convient aux cuirs chevelus sensibles.',
                hair_type: 'Cuir chevelu sensible',
                image: CONFIG.placeholderImage
            },
            {
                ean: '3401360262652',
                name: 'Nodé Shampooing Fluide',
                brand: 'Bioderma',
                price: 10.50,
                usage: 'Shampooing quotidien non détergent. Respecte l\'équilibre du cuir chevelu.',
                hair_type: 'Tous types',
                image: CONFIG.placeholderImage
            },
            {
                ean: '3337871324568',
                name: 'Dercos Anti-Pelliculaire',
                brand: 'Vichy',
                price: 12.90,
                usage: 'Élimine les pellicules dès la première application. Usage 2-3 fois/semaine.',
                hair_type: 'Pellicules',
                image: CONFIG.placeholderImage
            },
            {
                ean: '3338221000125',
                name: 'Phytokératine Extrême Shampooing',
                brand: 'Phyto',
                price: 13.50,
                usage: 'Répare les cheveux très abîmés. Kératine végétale.',
                hair_type: 'Cheveux abîmés',
                image: CONFIG.placeholderImage
            },
            {
                ean: '3433422404847',
                name: 'Kerium Shampooing Anti-Chute',
                brand: 'La Roche-Posay',
                price: 15.90,
                usage: 'Réduit la chute de cheveux. Enrichi en Madécassoside.',
                hair_type: 'Chute de cheveux',
                image: CONFIG.placeholderImage
            },
            {
                ean: '3264680003783',
                name: 'Rêve de Miel Shampooing',
                brand: 'Nuxe',
                price: 11.50,
                usage: 'Shampooing doux et nourrissant au miel. Pour cheveux normaux à secs.',
                hair_type: 'Cheveux normaux à secs',
                image: CONFIG.placeholderImage
            }
        ];

        AppState.filteredProducts = [...AppState.products];
        this.log(`${AppState.products.length} produits chargés`);
    }
};

function bootstrapApp() {
    if (window.__PEPPER_APP_BOOTSTRAPPED__) {
        return;
    }
    window.__PEPPER_APP_BOOTSTRAPPED__ = true;
    App.init();
}

// Exposer l'application globalement pour les onclick HTML
window.App = App;

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bootstrapApp);
} else {
    bootstrapApp();
}
