/**
 * Compat UI pour WebView Pepper ancienne (ES5).
 * Même écrans que index.html, sans syntaxe JS moderne.
 */
(function () {
    "use strict";

    function getWsFromQuery() {
        var search = window.location.search || "";
        var match = search.match(/[?&]ws=([^&]+)/);
        if (match && match[1]) {
            try {
                return decodeURIComponent(match[1]);
            } catch (e) {
                return match[1];
            }
        }
        return "";
    }

    function getNumberFromQuery(name, defaultValue) {
        var search = window.location.search || "";
        var re = new RegExp("(?:[?&])" + name + "=([^&]+)(?:&|$)");
        var match = search.match(re);
        if (!match || !match[1]) {
            return defaultValue;
        }
        try {
            var parsed = Number(decodeURIComponent(match[1]));
            return isNaN(parsed) ? defaultValue : parsed;
        } catch (e) {
            return defaultValue;
        }
    }

    var CONFIG = {
        serverUrl: (function () {
            var fromQuery = getWsFromQuery();
            if (fromQuery) {
                return fromQuery;
            }
            var host = window.location.hostname || "localhost";
            return "ws://" + host + ":8765";
        })(),
        textQuestionEnabled: (function () {
            var search = window.location.search || "";
            return /(?:[?&])textq=1(?:&|$)/.test(search);
        })(),
        // 0 => pas de timeout côté tablette (le backend pilote la durée).
        scanTimeout: Math.max(0, getNumberFromQuery("scan_timeout_ms", 0)),
        voiceListenDurationS: Math.max(3, getNumberFromQuery("voice_duration_s", 3600)),
        placeholderImage: "placeholder.png",
        debug: true
    };

    var AppState = {
        currentScreen: "home",
        connected: false,
        ws: null,
        products: [],
        filteredProducts: [],
        top3Results: [],
        voiceRecording: false,
        scanInProgress: false,
        productLockedUntil: 0
    };

    function byId(id) {
        return document.getElementById(id);
    }

    var App = {
        init: function () {
            this.loadDemoProducts();
            this.connectWebSocket();
            this.configureTextQuestionButton();
            this.showScreen("home");
            this.log("UI compat initialisée");
        },

        configureTextQuestionButton: function () {
            var btn = byId("text-question-btn");
            if (!btn) {
                return;
            }
            btn.style.display = CONFIG.textQuestionEnabled ? "" : "none";
        },

        log: function () {
            if (!CONFIG.debug || !window.console) {
                return;
            }
            var args = Array.prototype.slice.call(arguments);
            args.unshift("[" + new Date().toISOString() + "]");
            console.log.apply(console, args);
        },

        updateConnectionStatus: function (statusText) {
            var statusEl = byId("ws-status");
            if (statusEl) {
                statusEl.textContent = "WebSocket: " + statusText;
            }
        },

        showScreen: function (screenId) {
            var screens = document.getElementsByClassName("screen");
            var i = 0;
            for (i = 0; i < screens.length; i += 1) {
                screens[i].className = screens[i].className.replace(" active", "");
            }

            var screen = byId("screen-" + screenId);
            if (screen) {
                if (screen.className.indexOf("active") < 0) {
                    screen.className += " active";
                }
                AppState.currentScreen = screenId;
                if (screenId === "barcode-scan") {
                    this.updateBarcodeStatus("waiting", "En attente du code-barres...");
                }
                if (screenId === "advice") {
                    this.requestProducts();
                }
            }
        },

        normalizeText: function (value) {
            return String(value || "")
                .toLowerCase()
                .replace(/[àáâãäå]/g, "a")
                .replace(/[èéêë]/g, "e")
                .replace(/[ìíîï]/g, "i")
                .replace(/[òóôõö]/g, "o")
                .replace(/[ùúûü]/g, "u")
                .replace(/[ç]/g, "c")
                .replace(/[ñ]/g, "n");
        },

        toAbsoluteUrl: function (value) {
            var text = String(value || "").trim();
            if (!text) {
                return "";
            }
            if (/^(https?:|wss?:|data:|blob:|file:|\/)/i.test(text)) {
                return text;
            }
            try {
                return new URL(text, window.location.href).toString();
            } catch (e) {
                return text;
            }
        },

        createProductPlaceholder: function () {
            return this.toAbsoluteUrl(CONFIG.placeholderImage);
        },

        resolveImageUrl: function (imageUrl, product) {
            var value = String(imageUrl || "").trim();
            var isGenericPlaceholder = (
                !value
                || value === CONFIG.placeholderImage
                || /\/placeholder\.svg$/.test(value)
                || /\/placeholder\.png$/.test(value)
            );
            if (isGenericPlaceholder) {
                return this.createProductPlaceholder(product || {});
            }
            return this.toAbsoluteUrl(value);
        },

        inferProductCategories: function (product) {
            product = product || {};
            var haystack = this.normalizeText(
                (product.hair_type || "")
                + " " + (product.usage || "")
                + " " + (product.name || "")
                + " " + (product.brand || "")
            );

            var categories = [];
            if (/(sec|deshydrat|hydrat|nourri|nutrition)/.test(haystack)) categories.push("secs");
            if (/(gras|sebo|seborr)/.test(haystack)) categories.push("gras");
            if (/(tous types|tout type|normal|usage quotidien|frequent|doux)/.test(haystack)) categories.push("normaux");
            if (/(color|mech)/.test(haystack)) categories.push("colores");
            if (/(pellic|anti[ -]?pellic|dermite|ds\+?)/.test(haystack)) categories.push("pellicules");
            if (/(sensible|reactif|hypersens|irrit|delicat)/.test(haystack)) categories.push("sensibles");
            if (/(abime|fragil|repar|casse|chute|fortifi)/.test(haystack)) categories.push("abimes");
            if (categories.length === 0) categories.push("autres");
            return categories;
        },

        showLoading: function (message) {
            byId("loading-text").textContent = message || "Chargement...";
            this.showScreen("loading");
        },

        setScanInProgress: function (inProgress) {
            AppState.scanInProgress = !!inProgress;
        },

        setVoiceRecordingState: function (recording) {
            AppState.voiceRecording = !!recording;
            var startIds = ["voice-start-btn", "product-voice-start-btn"];
            var sendIds = ["voice-send-btn", "product-voice-send-btn"];
            var i;
            for (i = 0; i < startIds.length; i += 1) {
                var startBtn = byId(startIds[i]);
                if (startBtn) {
                    startBtn.style.display = recording ? "none" : "";
                }
            }
            for (i = 0; i < sendIds.length; i += 1) {
                var sendBtn = byId(sendIds[i]);
                if (sendBtn) {
                    sendBtn.style.display = recording ? "" : "none";
                }
            }
        },

        updateVoiceTranscriptPreview: function (text) {
            var value = String(text || "").trim();
            var ids = ["voice-transcript-preview", "product-voice-transcript-preview"];
            var i;
            for (i = 0; i < ids.length; i += 1) {
                var el = byId(ids[i]);
                if (el) {
                    el.textContent = value;
                }
            }
        },

        showSecurityMessage: function (title, message) {
            byId("security-title").textContent = title || "Information";
            byId("security-message").textContent = message || "";
            this.showScreen("security");
        },

        showError: function (message) {
            this.showSecurityMessage("Erreur", message || "Erreur inconnue");
        },

        updateBarcodeStatus: function (status, message) {
            var icon = byId("barcode-status-icon");
            var text = byId("barcode-status-text");
            if (!icon || !text) {
                return;
            }

            icon.className = "status-icon " + status;
            if (status === "success") {
                icon.textContent = "✅";
            } else if (status === "error") {
                icon.textContent = "❌";
            } else {
                icon.textContent = "⏳";
            }
            text.textContent = message || "";
        },

        sendCommand: function (command, data) {
            if (!AppState.connected || !AppState.ws || AppState.ws.readyState !== 1) {
                this.log("Commande ignorée (WS non connectée):", command);
                return false;
            }
            AppState.ws.send(JSON.stringify({
                type: "command",
                command: command,
                data: data || {},
                timestamp: Date.now()
            }));
            return true;
        },

        startVisualScan: function () {
            if (AppState.scanInProgress) {
                this.log("Scan déjà en cours, action ignorée");
                return;
            }
            this.setScanInProgress(true);
            this.showLoading("Analyse du produit en cours...");
            if (!this.sendCommand("start_visual_scan", {})) {
                this.setScanInProgress(false);
                this.showError("Connexion tablette indisponible.");
                return;
            }
        },

        startBarcodeScan: function () {
            if (AppState.scanInProgress) {
                this.log("Scan déjà en cours, action ignorée");
                return;
            }
            this.setScanInProgress(true);
            this.showScreen("barcode-scan");
            this.updateBarcodeStatus("waiting", "Recherche du code-barres...");
            if (!this.sendCommand("start_barcode_scan", {})) {
                this.setScanInProgress(false);
                this.showError("Connexion tablette indisponible.");
                return;
            }
        },

        startVoiceQuestion: function () {
            var useProductContext = (AppState.currentScreen === "product" && !!AppState.currentProduct);
            if (!this.sendCommand("start_voice_question", {
                duration_s: CONFIG.voiceListenDurationS,
                manual_send: true,
                use_product_context: useProductContext
            })) {
                this.showError("Connexion tablette indisponible.");
                return;
            }
            this.setVoiceRecordingState(true);
            this.updateVoiceTranscriptPreview("Micro activé. Parlez puis appuyez sur « Envoyer la question ».");
        },

        stopVoiceQuestion: function () {
            if (!AppState.voiceRecording) {
                return;
            }
            if (!this.sendCommand("stop_voice_question", { manual_send: true })) {
                this.showError("Connexion tablette indisponible.");
                return;
            }
            this.setVoiceRecordingState(false);
            this.showLoading("Envoi de la question en cours...");
        },

        askTextQuestion: function () {
            if (!CONFIG.textQuestionEnabled) {
                return;
            }
            var question = window.prompt("Entrez votre question sur le shampooing :");
            var text = String(question || "").trim();
            if (!text) {
                return;
            }
            if (!this.sendCommand("ask_question", { question: text })) {
                this.showError("Connexion tablette indisponible.");
                return;
            }
            this.showLoading("Question envoyée à Pepper...");
        },

        showProduct: function (product) {
            product = product || {};
            this.setScanInProgress(false);
            AppState.currentProduct = product;
            AppState.productLockedUntil = Date.now() + 15000;
            var img = byId("product-image");
            if (img) {
                img.src = this.resolveImageUrl(product.image, product);
                img.onerror = function () {
                    img.onerror = null;
                    img.src = App.resolveImageUrl("", product);
                };
            }
            byId("product-name").textContent = product.name || "Produit inconnu";
            byId("product-brand").textContent = product.brand || "";
            byId("product-price").textContent = (typeof product.price === "number" && product.price > 0)
                ? (product.price.toFixed(2) + " €")
                : "Prix non disponible";
            byId("product-usage").textContent = product.usage || "Pas d'information disponible";
            byId("product-hair-type").textContent = product.hair_type || "Tous types";
            this.showScreen("product");
        },

        showTop3: function (results) {
            this.setScanInProgress(false);
            AppState.top3Results = results || [];
            var container = byId("top3-cards");
            container.innerHTML = "";
            var self = this;
            var i = 0;

            for (i = 0; i < AppState.top3Results.length; i += 1) {
                (function (idx) {
                    var result = AppState.top3Results[idx];
                    var card = document.createElement("div");
                    card.className = "top3-card";
                    card.onclick = function () {
                        self.selectTop3Product(idx);
                    };

                    var num = document.createElement("div");
                    num.className = "top3-card-number";
                    num.textContent = String(idx + 1);

                    var image = document.createElement("img");
                    image.className = "top3-card-image";
                    image.src = self.resolveImageUrl(result.image, result);
                    image.alt = result.name || "Produit";
                    image.onerror = function () {
                        image.onerror = null;
                        image.src = self.resolveImageUrl("", result);
                    };

                    var name = document.createElement("p");
                    name.className = "top3-card-name";
                    name.textContent = result.name || "Produit";

                    var conf = document.createElement("p");
                    conf.className = "top3-card-confidence";
                    conf.textContent = String(Math.round((result.confidence || 0) * 100)) + "% de confiance";

                    card.appendChild(num);
                    card.appendChild(image);
                    card.appendChild(name);
                    card.appendChild(conf);
                    container.appendChild(card);
                })(i);
            }

            this.showScreen("top3");
        },

        selectTop3Product: function (index) {
            var result = AppState.top3Results[index];
            if (!result) {
                return;
            }
            this.sendCommand("confirm_product", { ean: result.ean, index: index });
            this.showLoading("Validation du produit...");
        },

        filterProducts: function (filter) {
            var buttons = document.getElementsByClassName("filter-btn");
            var i = 0;
            for (i = 0; i < buttons.length; i += 1) {
                if ((buttons[i].getAttribute("data-filter") || "") === filter) {
                    if (buttons[i].className.indexOf("active") < 0) {
                        buttons[i].className += " active";
                    }
                } else {
                    buttons[i].className = buttons[i].className.replace(" active", "");
                }
            }

            if (filter === "all") {
                AppState.filteredProducts = AppState.products.slice(0);
            } else {
                AppState.filteredProducts = [];
                for (i = 0; i < AppState.products.length; i += 1) {
                    var p = AppState.products[i];
                    var categories = p._categories || this.inferProductCategories(p);
                    p._categories = categories;
                    if (categories.indexOf(String(filter)) >= 0) {
                        AppState.filteredProducts.push(p);
                    }
                }
            }
            this.renderProductsGrid();
        },

        renderProductsGrid: function () {
            var container = byId("products-grid");
            container.innerHTML = "";
            var self = this;
            var i = 0;
            for (i = 0; i < AppState.filteredProducts.length; i += 1) {
                (function (idx) {
                    var product = AppState.filteredProducts[idx];
                    var item = document.createElement("div");
                    item.className = "product-grid-item";
                    item.onclick = function () {
                        self.showProduct(product);
                    };

                    var img = document.createElement("img");
                    img.src = self.resolveImageUrl(product.image, product);
                    img.alt = product.name || "Produit";
                    img.onerror = function () {
                        img.onerror = null;
                        img.src = self.resolveImageUrl("", product);
                    };

                    var name = document.createElement("p");
                    name.className = "name";
                    name.textContent = product.name || "Produit";

                    var brand = document.createElement("p");
                    brand.className = "brand";
                    brand.textContent = product.brand || "";

                    var price = document.createElement("p");
                    price.className = "price";
                    price.textContent = (typeof product.price === "number")
                        ? (product.price.toFixed(2) + " €")
                        : "";

                    item.appendChild(img);
                    item.appendChild(name);
                    item.appendChild(brand);
                    item.appendChild(price);
                    container.appendChild(item);
                })(i);
            }
        },

        showAdviceAnswer: function (question, answer, recommendations) {
            var answerBox = byId("advice-answer-box");
            var answerText = byId("advice-answer-text");
            if (answerBox && answerText) {
                answerText.textContent = String(answer || "").trim() || "Réponse vide";
                answerBox.style.display = "";
            }

            var recoSection = byId("advice-reco-section");
            var recoGrid = byId("advice-reco-grid");
            var items = recommendations || [];
            if (Object.prototype.toString.call(items) !== "[object Array]") {
                items = [];
            }
            if (recoSection && recoGrid) {
                recoGrid.innerHTML = "";
                if (items.length > 0) {
                    var self = this;
                    for (var i = 0; i < items.length; i += 1) {
                        (function (idx) {
                            var p = items[idx] || {};
                            if (Object.prototype.toString.call(p.hair_type) === "[object Array]") {
                                p.hair_type = p.hair_type.join(", ");
                            }
                            var item = document.createElement("div");
                            item.className = "product-grid-item";
                            item.onclick = function () {
                                self.showProduct(p);
                            };

                            var img = document.createElement("img");
                            img.src = self.resolveImageUrl(p.image, p);
                            img.alt = p.name || "Produit";
                            img.onerror = function () {
                                img.onerror = null;
                                img.src = self.resolveImageUrl("", p);
                            };

                            var name = document.createElement("p");
                            name.className = "name";
                            name.textContent = p.name || "Produit";

                            var brand = document.createElement("p");
                            brand.className = "brand";
                            brand.textContent = p.brand || "";

                            var price = document.createElement("p");
                            price.className = "price";
                            price.textContent = (typeof p.price === "number")
                                ? (p.price.toFixed(2) + " €")
                                : "";

                            item.appendChild(img);
                            item.appendChild(name);
                            item.appendChild(brand);
                            item.appendChild(price);
                            recoGrid.appendChild(item);
                        })(i);
                    }
                    recoSection.style.display = "";
                } else {
                    recoSection.style.display = "none";
                }
            }
            this.showScreen("advice");
        },

        handleServerMessage: function (rawMessage) {
            var message;
            try {
                message = JSON.parse(rawMessage);
            } catch (e) {
                this.log("Message invalide:", rawMessage);
                return;
            }

            if (message.type === "product_identified") {
                this.showProduct(message.product);
            } else if (message.type === "top3_results") {
                this.showTop3(message.results);
            } else if (message.type === "barcode_detected") {
                this.setScanInProgress(false);
                this.updateBarcodeStatus("success", "Code-barres détecté: " + (message.ean || ""));
                if (message.product) {
                    this.showProduct(message.product);
                }
            } else if (message.type === "barcode_failed") {
                this.setScanInProgress(false);
                this.updateBarcodeStatus("error", "Code-barres non reconnu");
            } else if (message.type === "security_alert") {
                this.setScanInProgress(false);
                this.showSecurityMessage(message.title || "Information", message.message || "");
            } else if (message.type === "error") {
                var err = String(message.message || "");
                var errLower = err.toLowerCase();
                if (errLower.indexOf("scan déjà en cours") >= 0 || errLower.indexOf("scan deja en cours") >= 0) {
                    this.log("Erreur non bloquante ignorée:", err);
                    return;
                }
                this.setScanInProgress(false);
                this.showError(err || "Erreur inconnue");
            } else if (message.type === "show_screen") {
                var nextScreen = message.screen || "home";
                if (nextScreen === "home" || nextScreen === "scan-choice") {
                    this.setScanInProgress(false);
                }
                if (
                    AppState.currentScreen === "product"
                    && Date.now() < (AppState.productLockedUntil || 0)
                    && nextScreen !== "home"
                    && nextScreen !== "product"
                ) {
                    this.log("show_screen ignoré (fiche produit verrouillée):", nextScreen);
                } else {
                    this.showScreen(nextScreen);
                }
            } else if (message.type === "products_list") {
                AppState.products = (message.products || []).map(function (p) {
                    var product = p || {};
                    if (Object.prototype.toString.call(product.hair_type) === "[object Array]") {
                        product.hair_type = product.hair_type.join(", ");
                    }
                    product._categories = this.inferProductCategories(product);
                    product.image = this.resolveImageUrl(product.image, product);
                    return product;
                }, this);
                this.filterProducts("all");
            } else if (message.type === "status") {
                this.updateConnectionStatus(message.status || "connecté");
            } else if (message.type === "qa_answer") {
                if (
                    String(message.context_mode || "").toLowerCase() === "product"
                    && AppState.currentProduct
                ) {
                    var preview = byId("product-voice-transcript-preview");
                    if (preview) {
                        var a = (message.answer || "Réponse vide").trim();
                        preview.textContent = "Réponse: " + a;
                    }
                    this.showScreen("product");
                } else {
                    this.showAdviceAnswer(
                        message.question || "",
                        message.answer || "Réponse vide",
                        message.recommendations || []
                    );
                }
            } else if (message.type === "voice_status") {
                var status = String(message.status || "").toLowerCase();
                var msg = message.message || "";
                var title = message.title || "Question vocale";
                if (status === "listening") {
                    this.setVoiceRecordingState(true);
                    this.updateVoiceTranscriptPreview(msg || "Micro activé. Parlez puis appuyez sur « Envoyer la question ».");
                } else if (status === "processing") {
                    this.showLoading(msg || "Traitement vocal en cours...");
                } else if (status === "timeout" || status === "error") {
                    this.setVoiceRecordingState(false);
                    this.showSecurityMessage(title, msg || "Je n'ai pas bien entendu.");
                } else if (status === "transcript") {
                    // Transcription cachée côté UI (demande métier).
                    this.log("Transcription vocale reçue (masquée).");
                } else if (status === "done" && AppState.currentScreen === "loading") {
                    this.setVoiceRecordingState(false);
                    this.showScreen(AppState.currentProduct ? "product" : "advice");
                }
            }
        },

        connectWebSocket: function () {
            var self = this;
            try {
                AppState.ws = new WebSocket(CONFIG.serverUrl);
            } catch (e) {
                self.updateConnectionStatus("Erreur");
                setTimeout(function () { self.connectWebSocket(); }, 3000);
                return;
            }

            AppState.ws.onopen = function () {
                AppState.connected = true;
                self.updateConnectionStatus("Connecté");
                self.requestProducts();
            };

            AppState.ws.onclose = function () {
                AppState.connected = false;
                self.setVoiceRecordingState(false);
                self.setScanInProgress(false);
                self.updateConnectionStatus("Déconnecté");
                setTimeout(function () { self.connectWebSocket(); }, 3000);
            };

            AppState.ws.onerror = function () {
                self.updateConnectionStatus("Erreur");
            };

            AppState.ws.onmessage = function (event) {
                self.handleServerMessage(event.data);
            };
        },

        requestProducts: function () {
            var sent = this.sendCommand("get_products", { shampoo_only: true, limit: 200 });
            if (!sent) {
                this.log("Demande produits ignorée: WS non connectée");
            }
        },

        loadDemoProducts: function () {
            AppState.products = [
                {
                    ean: "3282770149272",
                    name: "Shampooing Extra-Doux Lait d'Avoine",
                    brand: "Klorane",
                    price: 9.5,
                    usage: "Shampooing doux pour usage fréquent.",
                    hair_type: "Tous types",
                    image: CONFIG.placeholderImage
                },
                {
                    ean: "3600523735501",
                    name: "Elseve Color-Vive Shampooing",
                    brand: "L'Oréal Paris",
                    price: 4.9,
                    usage: "Protection couleur pour cheveux colorés.",
                    hair_type: "Cheveux colorés",
                    image: CONFIG.placeholderImage
                },
                {
                    ean: "3337871324568",
                    name: "Dercos Anti-Pelliculaire",
                    brand: "Vichy",
                    price: 12.9,
                    usage: "Élimine les pellicules.",
                    hair_type: "Pellicules",
                    image: CONFIG.placeholderImage
                }
            ];
            AppState.filteredProducts = AppState.products.slice(0);
        }
    };

    function bootstrapApp() {
        if (window.__PEPPER_APP_BOOTSTRAPPED__) {
            return;
        }
        window.__PEPPER_APP_BOOTSTRAPPED__ = true;
        App.init();
    }

    window.App = App;
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", bootstrapApp);
    } else {
        bootstrapApp();
    }
})();
