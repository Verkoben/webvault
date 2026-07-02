#!/usr/bin/env python3
import base64
import json
import sqlite3
import mimetypes
from pathlib import Path
from html import escape
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote, quote

BASE = Path.cwd()
DB = BASE / "webvault.db"
STATS_DIR = Path.home() / ".webvault_stats"
STATS_DB = STATS_DIR / "reader_stats.sqlite3"

# Configuración de acceso
NOPASSWD = 1  # 0=requiere contraseña, 1=sin contraseña
USERNAME = "pedro"
PASSWORD = "trascala"

HOST = "127.0.0.1"
PORT = 8181
LIMIT_DEFAULT = 100

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif")
COVER_BASENAMES = (
    "cover", "folder", "portada", "front", "cubierta",
    "cover-front", "cover_front", "bookcover", "thumbnail", "thumb"
)

CSP_READER = (
    "default-src 'self' blob: data:; "
    "script-src 'self' 'unsafe-eval' 'unsafe-inline' blob:; "
    "script-src-elem 'self' 'unsafe-inline' blob:; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "font-src 'self' data: blob:; "
    "connect-src 'self' blob: data:; "
    "frame-src 'self' blob: data:; "
    "child-src 'self' blob: data:; "
    "worker-src 'self' blob: data:;"
)


def table_columns(con, table):
    try:
        return [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
    except sqlite3.Error:
        return []


def has_column(con, table, column):
    return column in table_columns(con, table)


def fts_columns(con):
    return table_columns(con, "documents_fts")


def safe_limit(limit):
    try:
        n = int(limit)
    except Exception:
        n = LIMIT_DEFAULT
    return max(1, min(n, 500))


def quote_fts_phrase(value):
    value = (value or "").strip()
    value = value.replace('"', '""')
    return f'"{value}"'


def normalize_search_query(query):
    """
    Permite búsquedas por campo al estilo:
      author:Anne Rice
      authhor:Anne Rice   # tolera la errata
      autor:Anne Rice
      title:Dune
      publisher:Minotauro
      language:es
      isbn:978
      subject:vampiros
    Si no hay prefijo, busca en toda la FTS.
    """
    q = (query or "").strip()
    if not q:
        return q

    aliases = {
        "author": "author",
        "authhor": "author",
        "autor": "author",
        "autora": "author",
        "title": "title",
        "titulo": "title",
        "título": "title",
        "publisher": "publisher",
        "editorial": "publisher",
        "language": "language",
        "idioma": "language",
        "isbn": "identifier",
        "id": "identifier",
        "identifier": "identifier",
        "subject": "subject",
        "materia": "subject",
        "tema": "subject",
        "text": "text",
        "texto": "text",
    }

    if ":" not in q:
        return q

    prefix, value = q.split(":", 1)
    field = aliases.get(prefix.strip().lower())
    value = value.strip()

    if not field or not value:
        return q

    return f"{field}:{quote_fts_phrase(value)}"


def search_db(query, kind="ALL", limit=LIMIT_DEFAULT):
    if not DB.exists():
        return []

    query = (query or "").strip()
    if not query:
        return []

    match_query = normalize_search_query(query)
    limit = safe_limit(limit)

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row

    try:
        docs_cols = set(table_columns(con, "documents"))
        fts_cols = set(fts_columns(con))

        if not fts_cols:
            return [error_row("La tabla documents_fts no existe o no tiene columnas.")]

        # Columnas opcionales de documents.
        doc_author = "d.author" if "author" in docs_cols else "''"
        doc_publisher = "d.publisher" if "publisher" in docs_cols else "''"
        doc_language = "d.language" if "language" in docs_cols else "''"
        doc_date = "d.date" if "date" in docs_cols else "''"
        doc_identifier = "d.identifier" if "identifier" in docs_cols else "''"
        doc_subject = "d.subject" if "subject" in docs_cols else "''"
        doc_cover = "d.cover" if "cover" in docs_cols else "''"
        doc_domain = "d.domain" if "domain" in docs_cols else "''"

        # En la FTS antigua había title,text. En la nueva hay metadatos también.
        # snippet(documents_fts, -1, ...) escoge automáticamente la mejor columna.
        snippet_expr = "snippet(documents_fts, -1, '[', ']', '...', 32) AS snippet"

        select_sql = f"""
            SELECT
                d.id,
                d.kind,
                d.title,
                d.path,
                {doc_domain} AS domain,
                {doc_cover} AS cover,
                {doc_author} AS author,
                {doc_publisher} AS publisher,
                {doc_language} AS language,
                {doc_date} AS date,
                {doc_identifier} AS identifier,
                {doc_subject} AS subject,
                {snippet_expr}
            FROM documents_fts
            JOIN documents d ON d.id = documents_fts.rowid
            WHERE documents_fts MATCH ?
        """

        params = [match_query]

        if kind in ("HTML", "EPUB"):
            select_sql += " AND d.kind = ?"
            params.append(kind)

        # LIMIT como entero embebido para evitar desajustes de placeholders.
        select_sql += f" LIMIT {limit}"

        rows = con.execute(select_sql, params).fetchall()
        return rows

    except sqlite3.OperationalError as e:
        return [error_row(str(e))]
    except sqlite3.InterfaceError as e:
        return [error_row(f"InterfaceError: {e}")]
    finally:
        con.close()


def error_row(message):
    return {
        "id": "",
        "kind": "ERROR",
        "title": "Error SQLite",
        "path": "",
        "domain": "",
        "cover": "",
        "author": "",
        "publisher": "",
        "language": "",
        "date": "",
        "identifier": "",
        "subject": "",
        "snippet": message,
    }



def resolve_document_path(path_value):
    """
    Devuelve una ruta local segura para un path guardado en SQLite.
    Soporta rutas relativas y absolutas. No modifica la base de datos.
    """
    raw = (path_value or "").strip()
    if not raw:
        return None

    # Quita prefijos accidentales de URL si estuvieran guardados así.
    raw = unquote(raw)
    if raw.startswith("file://"):
        raw = raw[7:]

    p = Path(raw)
    if not p.is_absolute():
        p = BASE / p

    try:
        return p.expanduser().resolve(strict=False)
    except Exception:
        return p


def is_image_file(path):
    try:
        return (
            path is not None
            and path.is_file()
            and path.suffix.lower() in IMAGE_EXTS
            and path.stat().st_size > 0
        )
    except Exception:
        return False


def cover_candidate_score(path, epub_path=None):
    name = path.stem.lower()
    suffix = path.suffix.lower()
    score = 0

    if suffix in (".jpg", ".jpeg"):
        score += 8
    elif suffix == ".png":
        score += 7
    elif suffix == ".webp":
        score += 6

    if epub_path is not None and name == epub_path.stem.lower():
        score += 140

    if name == "cover":
        score += 130
    elif name in COVER_BASENAMES:
        score += 115
    elif name.startswith("cover"):
        score += 105
    elif name.startswith("folder"):
        score += 95
    elif name.startswith("portada") or name.startswith("cubierta"):
        score += 95
    elif name.startswith("front"):
        score += 85
    elif "cover" in name or "portada" in name or "cubierta" in name:
        score += 70

    # Evita elegir iconos diminutos si hay alternativas mejores.
    try:
        size = path.stat().st_size
        if size > 10_000:
            score += 10
        if size > 50_000:
            score += 10
        if size > 150_000:
            score += 5
    except Exception:
        pass

    return score


def find_cover_for_document(path_value, cover_value=""):
    """
    Busca portada sin tocar SQLite:
    1) campo cover si apunta a un fichero real;
    2) imagen con el mismo nombre base que el EPUB;
    3) cover/folder/portada/front en el mismo directorio;
    4) primera imagen razonable del mismo directorio.
    """
    epub_path = resolve_document_path(path_value)

    # 1) Portada existente en la BD, si todavía sirve.
    if cover_value:
        cover_raw = unquote(str(cover_value).strip())
        possible = Path(cover_raw)
        candidates = []
        if possible.is_absolute():
            candidates.append(possible)
        else:
            candidates.append(BASE / possible)
            if epub_path is not None:
                candidates.append(epub_path.parent / possible)
        for c in candidates:
            if is_image_file(c):
                return c

    if epub_path is None:
        return None

    folder = epub_path.parent
    if not folder.exists() or not folder.is_dir():
        return None

    candidates = []

    # 2) Misma raíz que el EPUB: Libro.epub -> Libro.jpg/png/webp.
    for ext in IMAGE_EXTS:
        p = folder / f"{epub_path.stem}{ext}"
        if is_image_file(p):
            candidates.append(p)

    # 3) Nombres típicos.
    for base in COVER_BASENAMES:
        for ext in IMAGE_EXTS:
            p = folder / f"{base}{ext}"
            if is_image_file(p):
                candidates.append(p)

    # 4) Cualquier imagen del mismo directorio, puntuada.
    try:
        for p in folder.iterdir():
            if is_image_file(p):
                candidates.append(p)
    except Exception:
        pass

    if not candidates:
        return None

    # Quita duplicados conservando Path resoluble.
    unique = {}
    for p in candidates:
        try:
            unique[str(p.resolve(strict=False))] = p
        except Exception:
            unique[str(p)] = p

    ranked = sorted(
        unique.values(),
        key=lambda p: cover_candidate_score(p, epub_path),
        reverse=True,
    )
    return ranked[0]


def get_document_by_id(doc_id):
    try:
        doc_id_int = int(doc_id)
    except Exception:
        return None

    if not DB.exists():
        return None

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    try:
        docs_cols = set(table_columns(con, "documents"))
        doc_cover = "cover" if "cover" in docs_cols else "'' AS cover"
        row = con.execute(
            f"SELECT id, kind, title, path, {doc_cover} FROM documents WHERE id=?",
            (doc_id_int,),
        ).fetchone()
        return row
    except sqlite3.Error:
        return None
    finally:
        con.close()


def read_cover_bytes(doc_id):
    row = get_document_by_id(doc_id)
    if row is None:
        return None, None

    cover_path = find_cover_for_document(row["path"], row["cover"] or "")
    if not is_image_file(cover_path):
        return None, None

    mime, _ = mimetypes.guess_type(str(cover_path))
    if not mime or not mime.startswith("image/"):
        mime = "image/jpeg"

    try:
        return cover_path.read_bytes(), mime
    except Exception:
        return None, None

def get_epub_path_from_request(request_path):
    """
    Resuelve una petición .epub contra webvault.db.
    Esto evita bloquear EPUBs si la BD guarda rutas absolutas o rutas fuera de BASE.
    Sólo sirve EPUBs que estén registrados como documentos EPUB.
    """
    if not DB.exists():
        return None

    raw = unquote(request_path or "").strip()
    if not raw:
        return None

    candidates = []
    candidates.append(raw)
    candidates.append(raw.lstrip("/"))

    # Si llegó como //home/ubuntu/..., también probamos /home/ubuntu/...
    if raw.startswith("//"):
        candidates.append(raw[1:])

    # Normaliza duplicados preservando orden.
    seen = set()
    candidates = [c for c in candidates if not (c in seen or seen.add(c))]

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    try:
        placeholders = ",".join("?" for _ in candidates)
        row = con.execute(
            f"""
            SELECT path
            FROM documents
            WHERE kind='EPUB'
              AND (
                path IN ({placeholders})
                OR ('/' || path) IN ({placeholders})
              )
            LIMIT 1
            """,
            candidates + candidates,
        ).fetchone()

        if row is None:
            return None

        path = resolve_document_path(row["path"])
        if path and path.is_file() and path.suffix.lower() == ".epub":
            return path
        return None
    except sqlite3.Error:
        return None
    finally:
        con.close()


def send_file_bytes(handler, file_path, content_type=None):
    try:
        data = file_path.read_bytes()
    except Exception:
        handler.send_error(404, "Archivo no encontrado")
        return

    mime = content_type
    if not mime:
        mime, _ = mimetypes.guess_type(str(file_path))
    if not mime:
        mime = "application/octet-stream"

    handler.send_response(200)
    handler.send_header("Content-Type", mime)
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
    handler.end_headers()
    handler.wfile.write(data)

def get_epub_path_by_id(doc_id):
    """
    Devuelve el EPUB por ID desde webvault.db.
    No revela la ruta real del archivo.
    """
    try:
        doc_id_int = int(doc_id)
    except Exception:
        return None

    row = get_document_by_id(doc_id_int)
    if row is None:
        return None

    try:
        if row["kind"] != "EPUB":
            return None
    except Exception:
        return None

    epub_path = resolve_document_path(row["path"])
    if epub_path and epub_path.is_file() and epub_path.suffix.lower() == ".epub":
        return epub_path
    return None


def slug_for_epub(title):
    value = str(title or "libro").strip()
    value = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in value)
    value = value.strip("-._")
    if not value:
        value = "libro"
    if not value.lower().endswith(".epub"):
        value += ".epub"
    return value

def db_stats():
    if not DB.exists():
        return {"HTML": 0, "EPUB": 0, "TOTAL": 0}

    con = sqlite3.connect(DB)
    try:
        rows = con.execute("SELECT kind, COUNT(*) FROM documents GROUP BY kind").fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        con.close()

    stats = {"HTML": 0, "EPUB": 0, "TOTAL": 0}
    for kind, count in rows:
        stats[kind] = count
        stats["TOTAL"] += count
    return stats




def open_stats_db():
    """
    SQLite separada para estadísticas.
    No toca webvault.db ni ralentiza FTS5.
    """
    STATS_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(STATS_DB, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("""
        CREATE TABLE IF NOT EXISTS reader_stats (
            file TEXT PRIMARY KEY,
            title TEXT,
            visits INTEGER NOT NULL DEFAULT 0,
            first_visit TEXT DEFAULT CURRENT_TIMESTAMP,
            last_visit TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    con.execute("""
        CREATE INDEX IF NOT EXISTS idx_reader_stats_visits
        ON reader_stats(visits DESC, last_visit DESC)
    """)
    return con


def record_reader_visit_file(file_value, title_value=""):
    """
    Mantiene el mismo nombre que la versión JSON para no tocar do_POST.
    Ahora escribe en stats/reader_stats.sqlite3.
    """
    file_value = unquote((file_value or "").strip())
    title_value = (title_value or "").strip()

    if not file_value:
        return False

    con = open_stats_db()
    try:
        con.execute("""
            INSERT INTO reader_stats(file, title, visits, first_visit, last_visit)
            VALUES (?, ?, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(file) DO UPDATE SET
                visits = visits + 1,
                title = CASE
                    WHEN excluded.title IS NOT NULL
                     AND excluded.title != ''
                     AND (reader_stats.title IS NULL OR reader_stats.title = '' OR reader_stats.title = reader_stats.file)
                    THEN excluded.title
                    ELSE reader_stats.title
                END,
                last_visit = CURRENT_TIMESTAMP
        """, (file_value, title_value or file_value))
        con.commit()
        return True
    finally:
        con.close()


def get_reader_stats(limit=100):
    data = {"total_visits": 0, "unique_books": 0, "top": []}

    con = open_stats_db()
    try:
        row = con.execute("""
            SELECT
                COALESCE(SUM(visits), 0) AS total_visits,
                COUNT(*) AS unique_books
            FROM reader_stats
        """).fetchone()

        if row:
            data["total_visits"] = int(row["total_visits"] or 0)
            data["unique_books"] = int(row["unique_books"] or 0)

        rows = con.execute("""
            SELECT file, title, visits, first_visit, last_visit
            FROM reader_stats
            ORDER BY visits DESC, last_visit DESC
            LIMIT ?
        """, (int(limit),)).fetchall()

        data["top"] = [dict(r) for r in rows]
        return data
    finally:
        con.close()


def lookup_document_title_author(file_value):
    """
    Enriquecer estadísticas con título/autor desde webvault.db.
    Si falla, no afecta al servidor.
    """
    if not DB.exists() or not file_value:
        return "", ""

    normalized = unquote(file_value).lstrip("/")

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    try:
        cols = set(table_columns(con, "documents"))
        author_expr = "author" if "author" in cols else "'' AS author"
        row = con.execute(
            f"""
            SELECT title, {author_expr}
            FROM documents
            WHERE path = ?
               OR ('/' || path) = ?
            LIMIT 1
            """,
            (normalized, "/" + normalized),
        ).fetchone()
        if row:
            return row["title"] or "", row["author"] or ""
    except Exception:
        pass
    finally:
        con.close()

    return "", ""



def public_book_url_for_file(file_value, fallback_title="libro"):
    """
    Convierte un valor de estadísticas o path real de EPUB en URL pública funcional:
      /book?id=ID&name=titulo.epub

    Si ya viene como /book?... se deja tal cual.
    Si no se encuentra en webvault.db, devuelve el valor original.
    """
    value = unquote((file_value or "").strip())
    if not value:
        return value

    if value.startswith("/book?"):
        return value

    if not DB.exists():
        return value

    candidates = [value, value.lstrip("/")]
    if value.startswith("//"):
        candidates.append(value[1:])

    seen = set()
    candidates = [c for c in candidates if not (c in seen or seen.add(c))]

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    try:
        placeholders = ",".join("?" for _ in candidates)
        row = con.execute(
            f"""
            SELECT id, title, path
            FROM documents
            WHERE kind='EPUB'
              AND (
                path IN ({placeholders})
                OR ('/' || path) IN ({placeholders})
              )
            LIMIT 1
            """,
            candidates + candidates,
        ).fetchone()

        if row is None:
            return value

        title = row["title"] or fallback_title or Path(row["path"] or "libro").stem
        return "/book?id=" + str(row["id"]) + "&name=" + quote(slug_for_epub(title), safe="")
    except Exception:
        return value
    finally:
        con.close()


def render_stat_page():
    library = db_stats()
    data = get_reader_stats(50)

    rows = []
    for i, item in enumerate(data.get("top", []), 1):
        file_value = item.get("file") or ""
        title = item.get("title") or file_value
        author = ""

        # /stat rápido: no consulta webvault.db por cada fila.
        # Usa directamente los datos guardados en reader_stats.sqlite3.
        public_file_url = file_value
        read_url = "/static/reader.html?file=" + quote(public_file_url, safe="")
        rows.append(f"""
        <tr>
          <td>{i}</td>
          <td>
            <a href="{escape(read_url)}">{escape(title)}</a>
            <div class="muted">{escape(author)}</div>
            <div class="path">{escape(public_file_url)}</div>
          </td>
          <td class="num">{int(item.get("visits", 0) or 0)}</td>
          <td>{escape(item.get("last_visit", "") or "")}</td>
        </tr>
        """)

    if not rows:
        rows.append('<tr><td colspan="4" class="empty">Todavía no hay visitas registradas al lector.</td></tr>')

    total_visits = int(data.get("total_visits", 0) or 0)
    unique_books = int(data.get("unique_books", 0) or 0)

    return f"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<title>WebVault · Estadísticas</title>
<style>
body {{
  font-family: system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
  max-width: 1180px;
  margin: 40px auto;
  padding: 0 20px;
  background: #fafafa;
  color: #151515;
}}
a {{ color: #0645ad; text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
.header {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}}
.back {{
  display: inline-block;
  border: 1px solid #ccc;
  border-radius: 10px;
  padding: 9px 12px;
  background: white;
  font-weight: 800;
}}
.grid {{
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 14px;
  margin: 24px 0 28px;
}}
.card {{
  background: white;
  border: 1px solid #ddd;
  border-radius: 16px;
  padding: 16px;
  box-shadow: 0 8px 26px rgba(0,0,0,.04);
}}
.label {{ color: #666; font-size: 13px; }}
.value {{ font-size: 30px; font-weight: 900; margin-top: 4px; }}
table {{
  width: 100%;
  border-collapse: collapse;
  background: white;
  border: 1px solid #ddd;
  border-radius: 14px;
  overflow: hidden;
}}
th, td {{
  padding: 11px 12px;
  border-bottom: 1px solid #eee;
  vertical-align: top;
  text-align: left;
}}
th {{ background: #f1f1f1; font-size: 13px; }}
.num {{ text-align: right; font-weight: 900; }}
.muted {{ color: #666; font-size: 13px; margin-top: 3px; }}
.path {{ color: #888; font-size: 12px; margin-top: 4px; overflow-wrap: anywhere; }}
.empty {{ text-align: center; color: #666; padding: 24px; }}
.note {{ color: #666; font-size: 13px; margin-top: 18px; }}
code {{ background: #eee; padding: 1px 4px; border-radius: 4px; }}
@media (max-width: 820px) {{ .grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }} }}
@media (max-width: 520px) {{ .grid {{ grid-template-columns: 1fr; }} }}
</style>
</head>
<body>
  <div class="header">
    <div>
      <h1>📊 Estadísticas WebVault</h1>
      <p>Cuenta cada apertura de <code>reader.html</code>.</p>
    </div>
    <a class="back" href="/">📚 Volver</a>
  </div>

  <section class="grid">
    <div class="card"><div class="label">Total biblioteca</div><div class="value">{library["TOTAL"]}</div></div>
    <div class="card"><div class="label">HTML</div><div class="value">{library["HTML"]}</div></div>
    <div class="card"><div class="label">EPUB</div><div class="value">{library["EPUB"]}</div></div>
    <div class="card"><div class="label">Visitas al lector</div><div class="value">{total_visits}</div></div>
  </section>

  <section class="grid">
    <div class="card"><div class="label">Libros abiertos</div><div class="value">{unique_books}</div></div>
  </section>

  <h2>Top libros abiertos</h2>
  <p style="color:#666;font-size:13px">Vista rápida: usa sólo la base de estadísticas.</p>
  <table>
    <thead>
      <tr>
        <th>#</th>
        <th>Libro</th>
        <th class="num">Visitas</th>
        <th>Última visita</th>
      </tr>
    </thead>
    <tbody>
      {''.join(rows)}
    </tbody>
  </table>

</body>
</html>""".encode("utf-8")


def render_obra_social_page():
    return """<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<title>WebVault · Obra social</title>
<style>
body{font-family:system-ui,sans-serif;max-width:980px;margin:40px auto;padding:0 20px;background:#fafafa}
.header{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap}
.back{border:1px solid #ccc;border-radius:10px;padding:9px 12px;text-decoration:none;font-weight:700}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px;margin-top:24px}
.card{background:#fff;border:1px solid #ddd;border-radius:16px;padding:20px}
.btn{display:inline-block;margin-top:12px;padding:10px 14px;border-radius:10px;background:#111;color:#fff;text-decoration:none;font-weight:700}
@media(max-width:720px){.grid{grid-template-columns:1fr}}
</style></head><body>
<div class="header"><div><h1>🤝 Obra social</h1><p>Esta biblioteca apoya iniciativas solidarias.</p></div><a class="back" href="/">📚 Volver</a></div>
<div class="grid">
<div class="card"><h2>❤️ Cáritas</h2><p>Apoyo a personas y familias en situación de vulnerabilidad.</p><a class="btn" target="_blank" rel="noopener noreferrer" href="https://www.caritas.es/donar/">Donar</a></div>
<div class="card"><h2>🎗️ Asociación Española Contra el Cáncer</h2><p>Investigación, prevención y apoyo a pacientes y familiares.</p><a class="btn" target="_blank" rel="noopener noreferrer" href="https://www.contraelcancer.es/es/colabora/dona">Donar</a></div>
<div class="card"><h2>🍽️ Bancos de Alimentos</h2><p>Recogida y distribución de alimentos para personas vulnerables.</p><a class="btn" target="_blank" rel="noopener noreferrer" href="https://www.fesbal.org/donar/">Donar</a></div>
<div class="card"><h2>👶 UNICEF</h2><p>Protección de la infancia, educación, salud y ayuda humanitaria.</p><a class="btn" target="_blank" rel="noopener noreferrer" href="https://www.unicef.es/dona">Donar</a></div>
</div></body></html>""".encode("utf-8")

def render_page(query="", kind="ALL"):
    raw_query = query or ""
    input_query = raw_query
    debug_real_paths = raw_query.rstrip().endswith("$%&")

    if debug_real_paths:
        query = raw_query.rstrip()[:-3].rstrip()
    else:
        query = raw_query

    rows = search_db(query, kind)
    stats = db_stats()

    def active(k):
        return "active" if kind == k else ""

    items = []

    for r in rows:
        r_kind = r["kind"]
        title = r["title"] or (Path(r["path"]).name if r["path"] else "")
        path = r["path"] or ""
        domain = r["domain"] or ""
        snippet = r["snippet"] or ""
        cover = r["cover"] or ""
        author = r["author"] or ""
        publisher = r["publisher"] or ""
        language = r["language"] or ""
        date = r["date"] or ""
        identifier = r["identifier"] or ""
        subject = r["subject"] or ""

        if r_kind == "ERROR":
            items.append(
                f"""
                <article class="error">
                  <h2>{escape(title)}</h2>
                  <p>{escape(snippet)}</p>
                </article>
                """
            )
            continue

        href = "/" + quote(path)
        display_path = path
        reader_href = href
        icon = "[EPUB]" if r_kind == "EPUB" else "[HTML]"

        if r_kind == "EPUB":
            book_url = "/book?id=" + str(r["id"]) + "&name=" + quote(slug_for_epub(title), safe="")
            encoded_book_url = quote(book_url, safe="")
            reader_href = "/static/reader.html?file=" + encoded_book_url
            href = reader_href
            display_path = path if debug_real_paths else book_url

        cover_html = '<div class="cover placeholder">Sin portada</div>'
        cover_path = find_cover_for_document(path, cover)
        if cover_path is not None:
            cover_html = (
                f'<a href="{href}">'
                f'<img class="cover" src="/cover?id={escape(str(r["id"]))}" alt="Portada" loading="lazy">'
                f'</a>'
            )

        meta_rows = []
        if author:
            meta_rows.append(("Autor", author))
        if publisher:
            meta_rows.append(("Editorial", publisher))
        if language:
            meta_rows.append(("Idioma", language))
        if date:
            meta_rows.append(("Fecha", date))
        if identifier:
            meta_rows.append(("ISBN/ID", identifier))
        if subject:
            meta_rows.append(("Materia", subject))

        meta_html = ""
        if meta_rows:
            meta_html = '<div class="bookmeta">' + "".join(
                f'<div class="meta-row"><span class="meta-label">{escape(label)}:</span> '
                f'<span class="meta-value">{escape(value)}</span></div>'
                for label, value in meta_rows
            ) + "</div>"

        pdf_link = ""
        if r_kind == "HTML":
            html_path = BASE / path
            pdf_path = html_path.parent / "pdf" / (html_path.stem + ".pdf")
            if pdf_path.exists():
                pdf_rel = pdf_path.relative_to(BASE)
                pdf_link = f' · <a href="/{quote(str(pdf_rel))}">PDF</a>'

        links = ""
        if r_kind == "EPUB":
            links = f'<a href="{reader_href}">📖 Leer</a>'
        elif r_kind == "HTML":
            links = f'<a href="{href}">Abrir</a>{pdf_link}'
        else:
            links = f'<a href="{href}">Abrir</a>'

        items.append(
            f"""
            <article class="result-card">
              {cover_html}
              <div class="result-body">
                <h2>{icon} <a href="{href}">{escape(title)}</a></h2>
                {meta_html}
                <p>{escape(snippet)}</p>
                <small>{escape(r_kind)} · {escape(domain)} · {escape(display_path)}</small>
                <div class="links">{links}</div>
              </div>
            </article>
            """
        )

    results_html = "\n".join(items)

    return f"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<title>WebVault</title>
<style>
body {{
  font-family: system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
  max-width: 1320px;
  margin: 40px auto;
  padding: 0 20px;
  line-height: 1.5;
  background: #fafafa;
}}
.layout {{
  display: grid;
  grid-template-columns: 190px minmax(0, 1fr);
  gap: 22px;
  align-items: start;
}}
.sidebar {{
  position: sticky;
  top: 18px;
  background: white;
  border: 1px solid #ddd;
  border-radius: 12px;
  padding: 14px;
}}
.sidebar h2 {{
  font-size: 16px;
  margin: 0 0 10px;
}}
.field-buttons {{
  display: grid;
  gap: 8px;
}}
.field-btn {{
  width: 100%;
  text-align: left;
  font-size: 14px;
  padding: 8px 10px;
}}
.main {{ min-width: 0; }}
h1 {{ margin-bottom: 4px; }}
.stats {{ color: #555; margin-bottom: 24px; }}
form {{
  display: flex;
  gap: 8px;
  margin: 20px 0;
  flex-wrap: wrap;
}}
input {{
  font-size: 20px;
  padding: 10px;
  flex: 1;
  min-width: 320px;
}}
button {{
  font-size: 16px;
  padding: 10px 14px;
  border: 1px solid #bbb;
  border-radius: 8px;
  background: white;
  cursor: pointer;
}}
button.active {{
  background: #111;
  color: white;
}}
.result-card {{
  display: flex;
  gap: 18px;
  align-items: flex-start;
  background: white;
  border: 1px solid #ddd;
  border-radius: 12px;
  padding: 18px 20px;
  margin: 14px 0;
}}
.result-body {{ flex: 1; min-width: 0; }}
.cover {{
  width: 92px;
  min-width: 92px;
  height: 138px;
  object-fit: cover;
  border-radius: 6px;
  border: 1px solid #ccc;
  background: #eee;
}}
.cover.placeholder {{
  display: flex;
  align-items: center;
  justify-content: center;
  text-align: center;
  color: #777;
  font-size: 12px;
}}
article.error {{
  border-color: #c00;
  background: #fff3f3;
}}
a {{
  color: #0645ad;
  text-decoration: none;
}}
a:hover {{ text-decoration: underline; }}
small {{ color: #666; }}
.bookmeta {{
  color: #444;
  font-size: 14px;
  margin: 4px 0 8px;
  text-align: left;
}}
.meta-row {{
  display: grid;
  grid-template-columns: 90px minmax(0, 1fr);
  gap: 8px;
  align-items: start;
  text-align: left;
}}
.meta-label {{
  font-weight: 650;
  color: #222;
  text-align: left;
}}
.meta-value {{
  text-align: left;
  overflow-wrap: anywhere;
}}
.help {{
  margin-top: -10px;
  margin-bottom: 18px;
  color: #666;
  font-size: 14px;
  text-align: left;
}}
mark, b {{ background: #fff3a0; }}
.links {{ margin-top: 8px; font-weight: 600; }}
@media (max-width: 780px) {{
  .layout {{ grid-template-columns: 1fr; }}
  .sidebar {{ position: static; }}
  .field-buttons {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
}}
@media (max-width: 650px) {{
  .result-card {{ gap: 12px; padding: 14px; }}
  .cover {{ width: 70px; min-width: 70px; height: 105px; }}
}}
code {{
  background: #eee;
  padding: 1px 4px;
  border-radius: 4px;
}}
</style>
</head>
<body>
<h1>WebVault</h1>
<div class="stats">
  Total: {stats["TOTAL"]} · HTML: {stats["HTML"]} · EPUB: {stats["EPUB"]} · <a href="/stat">📊 Estadísticas</a> · <a href="/obra-social">🤝 Obra social</a>
</div>

<div class="layout">
  <aside class="sidebar">
    <h2>Buscar por</h2>
    <div class="field-buttons">
      <button type="button" class="field-btn" data-prefix="Autor:">Autor</button>
      <button type="button" class="field-btn" data-prefix="Title:">Título</button>
      <button type="button" class="field-btn" data-prefix="Editorial:">Editorial</button>
      <button type="button" class="field-btn" data-prefix="Idioma:">Idioma</button>
      <button type="button" class="field-btn" data-prefix="ISBN:">ISBN/ID</button>
      <button type="button" class="field-btn" data-prefix="Materia:">Materia</button>
      <button type="button" class="field-btn" data-prefix="Texto:">Texto</button>
      <button type="button" class="field-btn" data-prefix="">Todo</button>
    </div>
  </aside>

  <main class="main">
    <form action="/search" method="get">
      <input id="searchBox" name="q" value="{escape(input_query)}" placeholder="Buscar por título, autor, editorial o texto..." autofocus>
      <button class="{active("ALL")}" name="kind" value="ALL">Todos</button>
      <button class="{active("HTML")}" name="kind" value="HTML">HTML</button>
      <button class="{active("EPUB")}" name="kind" value="EPUB">EPUB</button>
    </form>
    <div class="help">
      Pulsa una opción de la izquierda para rellenar el prefijo. Ejemplos:
      <code>Autor:Anne Rice</code>, <code>Title:Dune</code>,
      <code>Editorial:Minotauro</code>, <code>Idioma:es</code>,
      <code>ISBN:978</code>, <code>Materia:vampiros</code>.
    </div>

    <p>Resultados: {len(rows)}</p>

    {results_html}
  </main>
</div>

<script>
(function() {{
  const input = document.getElementById("searchBox");
  const buttons = document.querySelectorAll(".field-btn");

  function stripKnownPrefix(value) {{
    return value.replace(/^(autor|author|authhor|autora|title|titulo|título|editorial|publisher|idioma|language|isbn|id|identifier|materia|tema|subject|texto|text):\\s*/i, "");
  }}

  buttons.forEach(button => {{
    button.addEventListener("click", () => {{
      const prefix = button.dataset.prefix || "";
      const current = stripKnownPrefix(input.value || "");
      input.value = prefix + current;
      input.focus();
      const pos = input.value.length;
      input.setSelectionRange(pos, pos);
    }});
  }});
}})();
</script>

</body>
</html>""".encode("utf-8")


class Handler(SimpleHTTPRequestHandler):
    def list_directory(self, path):
        self.send_error(403, "Listado de directorios desactivado")
        return None

    def authenticate(self):
        if NOPASSWD:
            return True

        auth = self.headers.get("Authorization")

        if auth is None or not auth.startswith("Basic "):
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="WebVault"')
            self.end_headers()
            return False

        encoded = auth.split(" ", 1)[1].strip()

        try:
            decoded = base64.b64decode(encoded).decode("utf-8")
            user, passwd = decoded.split(":", 1)
        except Exception:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="WebVault"')
            self.end_headers()
            return False

        if user == USERNAME and passwd == PASSWORD:
            return True

        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="WebVault"')
        self.end_headers()
        return False

    def end_headers(self):
        if self.path.startswith("/static/reader.html"):
            self.send_header("Content-Security-Policy", CSP_READER)
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        super().end_headers()


    def do_POST(self):
        if not self.authenticate():
            return

        parsed = urlparse(self.path)

        if parsed.path in ("/api/stats", "/api/stats/reader-visit"):
            try:
                length = int(self.headers.get("Content-Length", "0") or "0")
            except Exception:
                length = 0

            raw = self.rfile.read(length) if length > 0 else b"{}"

            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                payload = {}

            ok = record_reader_visit_file(
                payload.get("file", ""),
                payload.get("title", "")
            )

            data = json.dumps({"ok": bool(ok)}, ensure_ascii=False).encode("utf-8")
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.end_headers()
            self.wfile.write(data)
            return

        self.send_error(404, "Endpoint no encontrado")


    def do_GET(self):
        if not self.authenticate():
            return

        parsed = urlparse(self.path)


        if parsed.path == "/obra-social":
            data = render_obra_social_page()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.end_headers()
            self.wfile.write(data)
            return

        if parsed.path in ("/stat", "/stats"):
            data = render_stat_page()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.end_headers()
            self.wfile.write(data)
            return

        if parsed.path == "/book":
            params = parse_qs(parsed.query)
            doc_id = params.get("id", [""])[0]
            epub_path = get_epub_path_by_id(doc_id)
            if epub_path is None:
                self.send_error(404, "EPUB no encontrado")
                return
            send_file_bytes(self, epub_path, "application/epub+zip")
            return

        if parsed.path.lower().endswith(".epub"):
            epub_path = get_epub_path_from_request(parsed.path)
            if epub_path is None:
                self.send_error(404, "EPUB no encontrado o no registrado")
                return
            send_file_bytes(self, epub_path, "application/epub+zip")
            return

        if parsed.path == "/cover":
            params = parse_qs(parsed.query)
            doc_id = params.get("id", [""])[0]
            data, mime = read_cover_bytes(doc_id)
            if data is None:
                self.send_error(404, "Portada no encontrada")
                return
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.end_headers()
            self.wfile.write(data)
            return

        if parsed.path in ("/", "/search"):
            params = parse_qs(parsed.query)
            q = unquote(params.get("q", [""])[0])
            kind = params.get("kind", ["ALL"])[0].upper()

            if kind not in ("ALL", "HTML", "EPUB"):
                kind = "ALL"

            data = render_page(q, kind)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if parsed.path == "/static/reader.html":
            reader = BASE / "static" / "reader.html"
            if not reader.exists():
                self.send_error(404, "reader.html no encontrado")
                return
            data = reader.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        # Fallback seguro:
        # - bloquea ficheros internos y estadísticas;
        # - evita traversal fuera de BASE;
        # - bloquea directorios;
        # - deja servir EPUB/HTML/PDF/imágenes y recursos necesarios sin whitelist agresiva.
        lowered_path = parsed.path.lower()

        if (
            lowered_path.startswith("/stats/")
            or lowered_path.startswith("/stat/")
            or lowered_path in ("/readme.html", "/readme.md")
            or lowered_path.endswith(".sqlite3")
            or lowered_path.endswith(".sqlite")
            or lowered_path.endswith(".db")
            or lowered_path.endswith(".db-wal")
            or lowered_path.endswith(".db-shm")
            or lowered_path.endswith(".sqlite3-wal")
            or lowered_path.endswith(".sqlite3-shm")
            or lowered_path.endswith(".py")
            or lowered_path.endswith(".pyc")
            or lowered_path.endswith(".log")
            or lowered_path.endswith(".bak")
            or lowered_path.endswith(".tmp")
        ):
            self.send_error(403, "Acceso denegado")
            return

        try:
            base_resolved = BASE.resolve(strict=False)
            requested_path = (BASE / unquote(parsed.path.lstrip("/"))).resolve(strict=False)
            requested_path.relative_to(base_resolved)
        except Exception:
            self.send_error(403, "Acceso denegado")
            return

        if requested_path.is_dir():
            self.send_error(403, "Listado de directorios desactivado")
            return

        return super().do_GET()


if __name__ == "__main__":
    print("Servidor WebVault:")
    print(f"  http://{HOST}:{PORT}")
    print("Usuario:", USERNAME)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
