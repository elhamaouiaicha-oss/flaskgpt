# Importe le type Iterator, qui représente un objet fournissant des valeurs une par une.
from collections.abc import Iterator

# Outils standards : fichiers, base de données, dates, texte.
import html
import io
import json
import os
import re
import sqlite3
import zipfile
from contextlib import contextmanager
from datetime import datetime

# Importe la classe du client OpenAI.
from openai import OpenAI

# Permet de charger les variables du fichier .env.
from dotenv import load_dotenv

# Du module "flask" on importe ce dont l'application a besoin.
from flask import Flask, Response, jsonify, render_template, request

load_dotenv()

# Utilise la bibliothèque OpenAI pour communiquer avec le serveur Groq.
client = OpenAI(
    api_key=os.getenv("GROQ_API_KEY"),
    base_url="https://api.groq.com/openai/v1"
)

MODEL = "openai/gpt-oss-20b"

SYSTEM_PROMPT = (
    "Tu es FlaskGPT, un assistant IA serviable. "
    "Réponds dans la langue de l'utilisateur, en Markdown quand c'est utile. "
    "Quand un fichier est joint, appuie-toi sur son contenu."
)

# Question utilisée quand l'utilisateur importe un fichier sans écrire de texte.
DEFAULT_FILE_PROMPT = "Fais un résumé clair et structuré de ce fichier."
DEFAULT_TITLE = "Nouvelle discussion"

# Limites pour les fichiers importés.
MAX_UPLOAD_BYTES = 5 * 1024 * 1024   # 5 Mo
MAX_FILE_CHARS = 15000               # texte envoyé au modèle
TEXT_EXTENSIONS = {
    ".txt", ".md", ".csv", ".json", ".log", ".xml", ".yml", ".yaml",
    ".py", ".js", ".html", ".css", ".sql",
}

# Marque envoyée dans le flux pour séparer la réponse d'une éventuelle erreur.
ERROR_MARK = "\x1e"

# Crée l'application web Flask.
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES

# La base de données SQLite est un simple fichier à la racine du projet.
DB_PATH = os.path.join(app.root_path, "chat.db")


# ---------------------------------------------------------------------------
# Base de données (SQLite)
# ---------------------------------------------------------------------------

@contextmanager
def db():
    """Ouvre une connexion, valide les changements, puis la referme."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Crée les tables si elles n'existent pas encore."""
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                title      TEXT NOT NULL DEFAULT 'Nouvelle discussion',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS messages (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL
                                REFERENCES conversations(id) ON DELETE CASCADE,
                role            TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                content         TEXT NOT NULL DEFAULT '',
                attachment_name TEXT,
                attachment_text TEXT,
                rating          INTEGER NOT NULL DEFAULT 0
                                CHECK (rating IN (-1, 0, 1)),
                created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_messages_conversation
                ON messages (conversation_id, id);
            """
        )


init_db()

# Une réponse vide est le "brouillon" d'une réponse en cours de génération.
VISIBLE_MESSAGES = "NOT (role = 'assistant' AND content = '')"


def json_error(message: str, status: int):
    return jsonify(error=message), status


def shorten(text: str, limit: int = 40) -> str:
    """Résume un texte en une ligne courte (pour le titre d'une discussion)."""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def conversation_summary(conn, conversation_id: int):
    return conn.execute(
        """
        SELECT c.id, c.title, c.updated_at,
               (SELECT COUNT(*) FROM messages m
                 WHERE m.conversation_id = c.id AND """ + VISIBLE_MESSAGES + """
               ) AS message_count
          FROM conversations c
         WHERE c.id = ?
        """,
        (conversation_id,),
    ).fetchone()


# ---------------------------------------------------------------------------
# Pages et discussions
# ---------------------------------------------------------------------------

# Route de la racine : page d'accueil.
@app.route('/')
def home():
    return render_template('index.html')


# Liste des discussions (la plus récente d'abord) : alimente le menu déroulant.
@app.route('/api/conversations', methods=['GET'])
def list_conversations():
    with db() as conn:
        rows = conn.execute(
            """
            SELECT c.id, c.title, c.updated_at,
                   (SELECT COUNT(*) FROM messages m
                     WHERE m.conversation_id = c.id AND """ + VISIBLE_MESSAGES + """
                   ) AS message_count
              FROM conversations c
             ORDER BY c.updated_at DESC, c.id DESC
            """
        ).fetchall()
    return jsonify([dict(row) for row in rows])


# Crée une nouvelle discussion vide.
@app.route('/api/conversations', methods=['POST'])
def create_conversation():
    with db() as conn:
        cursor = conn.execute(
            "INSERT INTO conversations (title) VALUES (?)", (DEFAULT_TITLE,)
        )
        created = conversation_summary(conn, cursor.lastrowid)
    return jsonify(dict(created)), 201


# Renvoie une discussion et tous ses messages.
@app.route('/api/conversations/<int:conversation_id>', methods=['GET'])
def get_conversation(conversation_id: int):
    with db() as conn:
        conversation = conversation_summary(conn, conversation_id)
        if conversation is None:
            return json_error("Discussion introuvable.", 404)
        messages = conn.execute(
            "SELECT id, role, content, attachment_name, rating, created_at "
            "FROM messages WHERE conversation_id = ? AND " + VISIBLE_MESSAGES +
            " ORDER BY id",
            (conversation_id,),
        ).fetchall()
    return jsonify(
        conversation=dict(conversation),
        messages=[dict(message) for message in messages],
    )


# Supprime une discussion (ses messages partent avec, grâce à ON DELETE CASCADE).
@app.route('/api/conversations/<int:conversation_id>', methods=['DELETE'])
def delete_conversation(conversation_id: int):
    with db() as conn:
        cursor = conn.execute(
            "DELETE FROM conversations WHERE id = ?", (conversation_id,)
        )
    if cursor.rowcount == 0:
        return json_error("Discussion introuvable.", 404)
    return "", 204


# Vide la discussion : on garde la discussion, on supprime ses messages.
@app.route('/api/conversations/<int:conversation_id>/clear', methods=['POST'])
def clear_conversation(conversation_id: int):
    with db() as conn:
        if conversation_summary(conn, conversation_id) is None:
            return json_error("Discussion introuvable.", 404)
        conn.execute(
            "DELETE FROM messages WHERE conversation_id = ?", (conversation_id,)
        )
        conn.execute(
            "UPDATE conversations SET title = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ?",
            (DEFAULT_TITLE, conversation_id),
        )
    return "", 204


# ---------------------------------------------------------------------------
# Envoi d'un message (avec fichier optionnel) et réponse en flux
# ---------------------------------------------------------------------------

def extract_text(upload) -> str:
    """Lit un fichier importé et en retourne le texte brut."""
    extension = os.path.splitext(upload.filename.lower())[1]
    data = upload.read()

    if extension == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            raise ValueError(
                "Pour lire les PDF, installe pypdf : poetry add pypdf"
            )
        try:
            reader = PdfReader(io.BytesIO(data))
            text = "\n".join((page.extract_text() or "") for page in reader.pages)
        except Exception:
            raise ValueError("Impossible de lire ce PDF.")

    elif extension == ".docx":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                info = archive.getinfo("word/document.xml")
                if info.file_size > 20 * 1024 * 1024:
                    raise ValueError("Document Word trop volumineux.")
                xml = archive.read("word/document.xml").decode("utf-8", "replace")
        except (zipfile.BadZipFile, KeyError):
            raise ValueError("Impossible de lire ce document Word.")
        paragraphs = re.findall(r"<w:p[ >].*?</w:p>", xml, flags=re.S)
        text = "\n".join(
            html.unescape("".join(re.findall(r"<w:t[^>]*>(.*?)</w:t>", p, flags=re.S)))
            for p in paragraphs
        )

    elif extension in TEXT_EXTENSIONS:
        text = data.decode("utf-8", errors="replace")

    else:
        raise ValueError(
            "Format non pris en charge. Formats acceptés : "
            "txt, md, csv, json, pdf, docx, py, html…"
        )

    return text.strip()


def build_conversation(rows) -> list[dict]:
    """Transforme les messages de la base en liste de messages pour le modèle."""
    conversation = [{"role": "system", "content": SYSTEM_PROMPT}]
    for row in rows:
        content = row["content"]
        if row["attachment_text"]:
            content = (
                f"Fichier joint « {row['attachment_name']} » :\n\n"
                f"{row['attachment_text']}\n\n---\n{content}"
            )
        conversation.append({"role": row["role"], "content": content})
    return conversation


@app.route('/api/conversations/<int:conversation_id>/messages', methods=['POST'])
def send_message(conversation_id: int):
    question = (request.form.get("prompt") or "").strip()
    upload = request.files.get("file")

    attachment_name = None
    attachment_text = None
    truncated = False

    if upload and upload.filename:
        try:
            attachment_text = extract_text(upload)
        except ValueError as error:
            return json_error(str(error), 400)
        if not attachment_text:
            return json_error("Ce fichier ne contient aucun texte exploitable.", 400)
        attachment_name = os.path.basename(upload.filename)[:120]
        if len(attachment_text) > MAX_FILE_CHARS:
            attachment_text = attachment_text[:MAX_FILE_CHARS]
            truncated = True

    if not question and attachment_text is None:
        return json_error("Écris une question ou importe un fichier.", 400)
    if not question:
        question = DEFAULT_FILE_PROMPT

    with db() as conn:
        conversation = conn.execute(
            "SELECT id, title FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
        if conversation is None:
            return json_error("Discussion introuvable.", 404)

        # Enregistre la question de l'utilisateur.
        conn.execute(
            "INSERT INTO messages (conversation_id, role, content, "
            "attachment_name, attachment_text) VALUES (?, 'user', ?, ?, ?)",
            (conversation_id, question, attachment_name, attachment_text),
        )

        # Le premier message donne son titre à la discussion.
        if conversation["title"] == DEFAULT_TITLE:
            title_source = (
                request.form.get("prompt", "").strip() or attachment_name or question
            )
            conn.execute(
                "UPDATE conversations SET title = ? WHERE id = ?",
                (shorten(title_source), conversation_id),
            )

        # Crée une réponse vide : son id permet à l'interface de la noter.
        reply_id = conn.execute(
            "INSERT INTO messages (conversation_id, role, content) "
            "VALUES (?, 'assistant', '')",
            (conversation_id,),
        ).lastrowid
        conn.execute(
            "UPDATE conversations SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (conversation_id,),
        )

        history = conn.execute(
            "SELECT role, content, attachment_name, attachment_text "
            "FROM messages WHERE conversation_id = ? AND id < ? AND "
            + VISIBLE_MESSAGES + " ORDER BY id",
            (conversation_id, reply_id),
        ).fetchall()

    response = Response(
        event_stream(build_conversation(history), reply_id),
        content_type="text/plain; charset=utf-8",
    )
    response.headers["X-Message-Id"] = str(reply_id)
    if truncated:
        response.headers["X-File-Truncated"] = "1"
    return response


def save_reply(reply_id: int, content: str) -> None:
    """Enregistre la réponse ; supprime le brouillon si rien n'a été généré."""
    with db() as conn:
        if content.strip():
            conn.execute(
                "UPDATE messages SET content = ? WHERE id = ?", (content, reply_id)
            )
        else:
            conn.execute("DELETE FROM messages WHERE id = ?", (reply_id,))


# Génère progressivement les fragments de texte de la réponse.
def event_stream(conversation: list[dict], reply_id: int) -> Iterator[str]:
    parts: list[str] = []
    try:
        # Envoie la conversation au modèle et active la réception progressive.
        response = client.chat.completions.create(
            model=MODEL,
            messages=conversation,
            stream=True
        )

        # Parcourt les fragments reçus depuis l'API.
        for chunk in response:
            # Ignore les fragments qui ne contiennent aucun choix.
            if not chunk.choices:
                continue

            # Récupère le texte du fragment, ou une chaîne vide.
            text = chunk.choices[0].delta.content or ""

            # Transmet le texte à Flask, qui l'envoie au navigateur.
            if text:
                parts.append(text)
                yield text
    except Exception as error:
        app.logger.exception("Erreur pendant l'appel au modèle")
        # L'erreur est envoyée au navigateur mais n'est pas enregistrée.
        yield ERROR_MARK + shorten(f"{type(error).__name__} : {error}", 300)
    finally:
        # Enregistre ce qui a été généré, même si la connexion a été coupée.
        save_reply(reply_id, "".join(parts))


# ---------------------------------------------------------------------------
# Évaluation des réponses
# ---------------------------------------------------------------------------

# rating : 1 = utile (vert), -1 = pas utile (rouge), 0 = pas d'avis.
@app.route('/api/messages/<int:message_id>/rating', methods=['POST'])
def rate_message(message_id: int):
    data = request.get_json(silent=True)
    rating = data.get("rating") if isinstance(data, dict) else None
    if type(rating) is not int or rating not in (-1, 0, 1):
        return json_error("'rating' doit valoir -1, 0 ou 1.", 400)

    with db() as conn:
        cursor = conn.execute(
            "UPDATE messages SET rating = ? WHERE id = ? AND role = 'assistant'",
            (rating, message_id),
        )
    if cursor.rowcount == 0:
        return json_error("Réponse introuvable.", 404)
    return jsonify(id=message_id, rating=rating)


# ---------------------------------------------------------------------------
# Export des discussions
# ---------------------------------------------------------------------------

RATING_LABELS = {1: "👍 Utile", -1: "👎 Pas utile"}


def load_export_data(conversation_id: int | None) -> list[dict]:
    with db() as conn:
        if conversation_id is None:
            conversations = conn.execute(
                "SELECT id, title, created_at, updated_at FROM conversations "
                "ORDER BY id"
            ).fetchall()
        else:
            conversations = conn.execute(
                "SELECT id, title, created_at, updated_at FROM conversations "
                "WHERE id = ?",
                (conversation_id,),
            ).fetchall()

        data = []
        for conversation in conversations:
            messages = conn.execute(
                "SELECT role, content, attachment_name, rating, created_at "
                "FROM messages WHERE conversation_id = ? AND " + VISIBLE_MESSAGES +
                " ORDER BY id",
                (conversation["id"],),
            ).fetchall()
            data.append({**dict(conversation), "messages": [dict(m) for m in messages]})
    return data


def export_markdown(conversations: list[dict]) -> str:
    lines = [f"# Export FlaskGPT — {datetime.now():%d/%m/%Y %H:%M}", ""]
    for conversation in conversations:
        lines += [f"## {conversation['title']}", ""]
        for message in conversation["messages"]:
            if message["role"] == "user":
                lines.append("**Vous**")
                if message["attachment_name"]:
                    lines.append(f"*📎 Fichier joint : {message['attachment_name']}*")
            else:
                label = RATING_LABELS.get(message["rating"])
                lines.append(f"**FlaskGPT**{f' ({label})' if label else ''}")
            lines += ["", message["content"], ""]
        lines.append("---")
        lines.append("")
    return "\n".join(lines)


# ?format=json|md   et   ?conversation_id=3 (sinon : toutes les discussions)
@app.route('/api/export')
def export_conversations():
    export_format = request.args.get("format", "json")
    if export_format not in ("json", "md"):
        return json_error("Format d'export inconnu (json ou md).", 400)

    conversation_id = request.args.get("conversation_id", type=int)
    conversations = load_export_data(conversation_id)
    if conversation_id is not None and not conversations:
        return json_error("Discussion introuvable.", 404)

    name = "toutes-les-discussions" if conversation_id is None else f"discussion-{conversation_id}"
    filename = f"flaskgpt-{name}-{datetime.now():%Y%m%d}.{export_format}"

    if export_format == "json":
        body = json.dumps(conversations, ensure_ascii=False, indent=2)
        mimetype = "application/json"
    else:
        body = export_markdown(conversations)
        mimetype = "text/markdown"

    return Response(
        body,
        mimetype=mimetype,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.errorhandler(413)
def too_large(_error):
    return json_error("Fichier trop volumineux (5 Mo maximum).", 413)


# Démarre le serveur lorsque tu lances directement le fichier.
if __name__ == "__main__":
    app.run(debug=True)
