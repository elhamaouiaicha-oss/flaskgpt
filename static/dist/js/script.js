"use strict";

// Caractère envoyé par Flask pour séparer la réponse d'un message d'erreur.
const ERROR_MARK = "\x1e";

// Question affichée quand on importe un fichier sans rien écrire (comme côté Flask).
const DEFAULT_FILE_PROMPT = "Fais un résumé clair et structuré de ce fichier.";

const MAX_UPLOAD_BYTES = 5 * 1024 * 1024;

// Couleurs d'un bloc selon son rôle et sa note (classes Tailwind complètes,
// pour que Tailwind les détecte en lisant ce fichier).
const USER_STYLE = ["border-indigo-100", "bg-indigo-50"];
const RATING_STYLES = {
    "0": ["border-slate-200", "bg-white"],
    "1": ["border-emerald-400", "bg-emerald-50"],
    "-1": ["border-rose-400", "bg-rose-50"],
};
const BUTTON_ACTIVE = {
    "1": ["bg-emerald-600", "border-emerald-600", "text-white"],
    "-1": ["bg-rose-600", "border-rose-600", "text-white"],
};
const BUTTON_IDLE = ["bg-white", "border-slate-300", "hover:bg-slate-100"];

const state = { conversationId: null, busy: false, file: null };
const els = {};
let converter = null;


// ---------------------------------------------------------------------------
// Utilitaires
// ---------------------------------------------------------------------------

function storageGet(key) {
    try { return localStorage.getItem(key); } catch { return null; }
}

function storageSet(key, value) {
    try { localStorage.setItem(key, value); } catch { /* stockage indisponible */ }
}

// Appelle l'API Flask ; en cas d'erreur, lève une Error avec le message du serveur.
async function api(url, options = {}) {
    const response = await fetch(url, options);
    if (!response.ok) throw new Error(await errorMessage(response));
    return response.status === 204 ? null : response.json();
}

function postJSON(url, body = {}) {
    return api(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
    });
}

async function errorMessage(response) {
    try {
        const data = await response.json();
        if (data.error) return data.error;
    } catch { /* la réponse n'est pas du JSON */ }
    return `Erreur ${response.status}`;
}

let statusTimer = null;
function showStatus(text, isError = true) {
    els.status.textContent = text;
    els.status.className = "mb-2 text-sm " + (isError ? "text-rose-600" : "text-emerald-600");
    els.status.hidden = false;
    clearTimeout(statusTimer);
    statusTimer = setTimeout(() => { els.status.hidden = true; }, 6000);
}

function scrollToBottom(force = false) {
    const nearBottom =
        window.innerHeight + window.scrollY >= document.body.scrollHeight - 160;
    if (force || nearBottom) {
        window.scrollTo({ top: document.body.scrollHeight });
    }
}


// ---------------------------------------------------------------------------
// Affichage des messages
// ---------------------------------------------------------------------------

// Convertit le Markdown en HTML sécurisé (ou affiche du texte si les
// bibliothèques du CDN ne sont pas chargées).
function renderMarkdown(element, text) {
    if (!window.showdown || !window.DOMPurify) {
        element.textContent = text;
        return;
    }
    element.innerHTML = DOMPurify.sanitize(converter.makeHtml(text));
}

function highlightCode(element) {
    if (!window.hljs) return;
    element.querySelectorAll("pre code").forEach((code) => hljs.highlightElement(code));
}

// Applique la note d'une réponse : couleur du bloc et état des boutons.
function applyRating(block, rating) {
    block.dataset.score = String(rating);

    block.classList.remove(...Object.values(RATING_STYLES).flat());
    block.classList.add(...RATING_STYLES[String(rating)]);

    block.querySelectorAll("button[data-rating]").forEach((button) => {
        const active = Number(button.dataset.rating) === rating;
        button.setAttribute("aria-pressed", String(active));
        button.classList.remove(...BUTTON_IDLE, ...BUTTON_ACTIVE["1"], ...BUTTON_ACTIVE["-1"]);
        button.classList.add(...(active ? BUTTON_ACTIVE[button.dataset.rating] : BUTTON_IDLE));
    });
}

// Crée un bloc de message à partir du modèle HTML caché.
function createMessageBlock({ id = null, role, content = "", rating = 0, attachmentName = null }) {
    const block = document.querySelector("#chat-template").cloneNode(true);
    block.removeAttribute("id");
    block.classList.remove("hidden");
    block.dataset.role = role;
    if (id) block.dataset.id = id;

    block.querySelector(".author").textContent = role === "user" ? "Vous" : "FlaskGPT";

    const attachment = block.querySelector(".attachment");
    if (attachmentName) {
        attachment.textContent = "📎 " + attachmentName;
        attachment.hidden = false;
    }

    const message = block.querySelector(".message");
    if (role === "user") {
        // Le texte de l'utilisateur est affiché tel quel, sans interpréter de HTML.
        block.classList.add(...USER_STYLE);
        message.classList.add("whitespace-pre-wrap");
        message.textContent = content;
    } else {
        applyRating(block, rating);
        if (content) {
            renderMarkdown(message, content);
            highlightCode(message);
            block.querySelector(".actions").hidden = false;
        }
    }

    els.output.appendChild(block);
    updateEmptyState();
    return block;
}

function updateEmptyState() {
    els.emptyState.hidden = els.output.children.length > 0;
}

// Lit la réponse progressive et l'affiche au fur et à mesure.
async function readResponseChunks(reader, block) {
    const decoder = new TextDecoder("utf-8");
    const message = block.querySelector(".message");
    let text = "";
    let answer = "";
    let error = "";

    const render = () => {
        const cut = text.indexOf(ERROR_MARK);
        answer = cut < 0 ? text : text.slice(0, cut);
        error = cut < 0 ? "" : text.slice(cut + 1).trim();

        message.classList.remove("animate-pulse", "text-slate-400");
        renderMarkdown(message, answer);
        if (error) {
            const warning = document.createElement("p");
            warning.className = "text-sm text-rose-600";
            warning.textContent = "⚠️ " + error;
            message.appendChild(warning);
        }
        scrollToBottom();
    };

    try {
        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            text += decoder.decode(value, { stream: true });
            render();
        }
        text += decoder.decode();
        render();
    } finally {
        reader.releaseLock();
    }

    return { answer, error };
}


// ---------------------------------------------------------------------------
// Discussions
// ---------------------------------------------------------------------------

// Remplit le menu déroulant avec la liste des discussions.
function fillSelect(conversations, selectedId) {
    els.select.replaceChildren(
        ...conversations.map((conversation) => {
            const option = document.createElement("option");
            option.value = String(conversation.id);
            option.textContent = conversation.title;
            return option;
        })
    );
    els.select.value = String(selectedId);
}

async function refreshList(selectedId = state.conversationId) {
    const conversations = await api("/api/conversations");
    fillSelect(conversations, selectedId);
    return conversations;
}

function updateExportLinks() {
    const id = state.conversationId;
    els.exportCurrentMd.href = `/api/export?format=md&conversation_id=${id}`;
    els.exportCurrentJson.href = `/api/export?format=json&conversation_id=${id}`;
}

async function loadConversation(id) {
    const data = await api(`/api/conversations/${id}`);
    state.conversationId = id;
    storageSet("flaskgpt:conversation", String(id));

    els.output.replaceChildren();
    data.messages.forEach((m) =>
        createMessageBlock({
            id: m.id,
            role: m.role,
            content: m.content,
            rating: m.rating,
            attachmentName: m.attachment_name,
        })
    );

    els.select.value = String(id);
    updateExportLinks();
    updateEmptyState();
    scrollToBottom(true);
}

async function createConversation() {
    // Inutile de multiplier les discussions vides.
    if (state.conversationId && els.output.children.length === 0) {
        els.prompt.focus();
        return;
    }
    const created = await postJSON("/api/conversations");
    await refreshList(created.id);
    await loadConversation(created.id);
    els.prompt.focus();
}

async function clearConversation() {
    if (els.output.children.length === 0) return;
    if (!confirm("Vider cette discussion ? Tous ses messages seront supprimés.")) return;

    await postJSON(`/api/conversations/${state.conversationId}/clear`);
    els.output.replaceChildren();
    updateEmptyState();
    await refreshList();
}

async function deleteConversation() {
    if (!confirm("Supprimer définitivement cette discussion ?")) return;

    await api(`/api/conversations/${state.conversationId}`, { method: "DELETE" });
    const conversations = await api("/api/conversations");
    if (conversations.length === 0) {
        const created = await postJSON("/api/conversations");
        await refreshList(created.id);
        await loadConversation(created.id);
    } else {
        fillSelect(conversations, conversations[0].id);
        await loadConversation(conversations[0].id);
    }
}


// ---------------------------------------------------------------------------
// Fichier importé
// ---------------------------------------------------------------------------

function setFile(file) {
    if (file && file.size > MAX_UPLOAD_BYTES) {
        showStatus("Fichier trop volumineux (5 Mo maximum).");
        file = null;
    }
    state.file = file;
    els.fileInput.value = "";
    els.fileName.textContent = file ? file.name : "";
    els.fileChip.hidden = !file;
    els.prompt.placeholder = file
        ? "Pose une question sur ce fichier (vide = résumé)…"
        : "Ask your question…";
}


// ---------------------------------------------------------------------------
// Envoi d'un message
// ---------------------------------------------------------------------------

function setBusy(busy) {
    state.busy = busy;
    for (const element of [
        els.send, els.select, els.newChat, els.clearChat, els.deleteChat, els.attach,
    ]) {
        element.disabled = busy;
    }
    els.send.setAttribute("aria-busy", String(busy));
}

async function sendMessage(event) {
    event.preventDefault();
    if (state.busy) return;

    const prompt = els.prompt.value.trim();
    const file = state.file;
    if (!prompt && !file) return;

    setBusy(true);
    els.status.hidden = true;

    const userBlock = createMessageBlock({
        role: "user",
        content: prompt || DEFAULT_FILE_PROMPT,
        attachmentName: file ? file.name : null,
    });
    const replyBlock = createMessageBlock({ role: "assistant" });
    const replyMessage = replyBlock.querySelector(".message");
    replyMessage.textContent = "Réflexion…";
    replyMessage.classList.add("animate-pulse", "text-slate-400");
    scrollToBottom(true);

    const form = new FormData();
    form.append("prompt", prompt);
    if (file) form.append("file", file, file.name);

    els.prompt.value = "";
    setFile(null);

    try {
        const response = await fetch(
            `/api/conversations/${state.conversationId}/messages`,
            { method: "POST", body: form }
        );

        if (!response.ok) {
            // Le serveur a refusé le message : on le retire et on le remet dans le formulaire.
            userBlock.remove();
            replyBlock.remove();
            updateEmptyState();
            els.prompt.value = prompt;
            if (file) setFile(file);
            showStatus(await errorMessage(response));
            return;
        }

        if (response.headers.get("X-File-Truncated")) {
            showStatus("Fichier long : seuls les premiers caractères ont été analysés.", false);
        }

        const { answer } = await readResponseChunks(response.body.getReader(), replyBlock);

        if (answer) {
            replyBlock.dataset.id = response.headers.get("X-Message-Id");
            replyBlock.querySelector(".actions").hidden = false;
            highlightCode(replyMessage);
        }
        scrollToBottom();
    } catch (error) {
        console.error(error);
        replyMessage.classList.remove("animate-pulse", "text-slate-400");
        replyMessage.textContent = "Erreur : impossible de récupérer la réponse.";
    } finally {
        setBusy(false);
        els.prompt.focus();
        try { await refreshList(); } catch (error) { console.error(error); }
    }
}


// ---------------------------------------------------------------------------
// Évaluation d'une réponse
// ---------------------------------------------------------------------------

async function rateMessage(button) {
    const block = button.closest("[data-role]");
    if (!block || !block.dataset.id) return;

    const clicked = Number(button.dataset.rating);
    const current = Number(block.dataset.score || 0);
    // Un second clic sur le même bouton retire la note.
    const next = clicked === current ? 0 : clicked;

    try {
        await postJSON(`/api/messages/${block.dataset.id}/rating`, { rating: next });
        applyRating(block, next);
    } catch (error) {
        showStatus(error.message);
    }
}


// ---------------------------------------------------------------------------
// Démarrage
// ---------------------------------------------------------------------------

// Exécute une action asynchrone en affichant l'erreur éventuelle.
function guarded(action) {
    return async (...args) => {
        try {
            await action(...args);
        } catch (error) {
            console.error(error);
            showStatus(error.message);
        }
    };
}

async function init() {
    let conversations = await api("/api/conversations");
    if (conversations.length === 0) {
        conversations = [await postJSON("/api/conversations")];
    }
    const saved = Number(storageGet("flaskgpt:conversation"));
    const initial = conversations.find((c) => c.id === saved) ?? conversations[0];

    fillSelect(conversations, initial.id);
    await loadConversation(initial.id);
}

// Attend que le HTML soit chargé avant d'exécuter ce code.
document.addEventListener("DOMContentLoaded", () => {
    Object.assign(els, {
        output: document.querySelector("#gpt-output"),
        emptyState: document.querySelector("#empty-state"),
        form: document.querySelector("#prompt-form"),
        prompt: document.querySelector("#prompt"),
        send: document.querySelector("#send"),
        status: document.querySelector("#status"),
        select: document.querySelector("#conversation-select"),
        newChat: document.querySelector("#new-chat"),
        clearChat: document.querySelector("#clear-chat"),
        deleteChat: document.querySelector("#delete-chat"),
        exportMenu: document.querySelector("#export-menu"),
        exportCurrentMd: document.querySelector("#export-current-md"),
        exportCurrentJson: document.querySelector("#export-current-json"),
        attach: document.querySelector("#attach"),
        fileInput: document.querySelector("#file-input"),
        fileChip: document.querySelector("#file-chip"),
        fileName: document.querySelector("#file-name"),
        fileRemove: document.querySelector("#file-remove"),
    });

    if (window.showdown) {
        converter = new showdown.Converter({
            tables: true,
            strikethrough: true,
            simplifiedAutoLink: true,
        });
    }

    els.form.addEventListener("submit", sendMessage);
    els.select.addEventListener("change", guarded(() => loadConversation(Number(els.select.value))));
    els.newChat.addEventListener("click", guarded(createConversation));
    els.clearChat.addEventListener("click", guarded(clearConversation));
    els.deleteChat.addEventListener("click", guarded(deleteConversation));

    els.attach.addEventListener("click", () => els.fileInput.click());
    els.fileInput.addEventListener("change", () => setFile(els.fileInput.files[0] ?? null));
    els.fileRemove.addEventListener("click", () => setFile(null));

    // Notation : un seul écouteur pour tous les boutons 👍 / 👎.
    els.output.addEventListener("click", (event) => {
        const button = event.target.closest("button[data-rating]");
        if (button) rateMessage(button);
    });

    // Ferme le menu "Exporter" après un choix ou un clic à l'extérieur.
    els.exportMenu.addEventListener("click", (event) => {
        if (event.target.closest("a")) setTimeout(() => { els.exportMenu.open = false; }, 0);
    });
    document.addEventListener("click", (event) => {
        if (!els.exportMenu.contains(event.target)) els.exportMenu.open = false;
    });

    guarded(init)();
});
