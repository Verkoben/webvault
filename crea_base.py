#!/usr/bin/env python3
import argparse
import hashlib
import os
import re
import sqlite3
import time
import zipfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from bs4 import BeautifulSoup
from tqdm import tqdm

from bs4 import XMLParsedAsHTMLWarning

import warnings

warnings.filterwarnings(
    "ignore",
    category=XMLParsedAsHTMLWarning
)

DB = "webvault.db"
HTML_ROOT = Path("a")
EPUB_ROOT = Path("epub")

TEXT_EXTS = (".xhtml", ".html", ".htm")
SKIP_EPUB_NAMES = (
    "nav.xhtml",
    "nav.html",
    "toc.xhtml",
    "toc.html",
)


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def strip_noise(soup: BeautifulSoup) -> None:
    for tag in soup(["script", "style", "noscript", "template", "svg"]):
        tag.decompose()


def file_sha1(path: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha1()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def parse_date_sort(value: str) -> int:
    """Convierte fechas EPUB irregulares a YYYYMMDD aproximado para ordenar."""
    value = clean_text(value)
    if not value:
        return 0

    # 2020-05-17 / 2020/05/17 / 2020.05.17
    m = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", value)
    if m:
        y, mo, d = map(int, m.groups())
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return y * 10000 + mo * 100 + d

    # 2020-05 / 2020/05
    m = re.search(r"(\d{4})[-/.](\d{1,2})", value)
    if m:
        y, mo = map(int, m.groups())
        if 1 <= mo <= 12:
            return y * 10000 + mo * 100 + 1

    # Solo año
    m = re.search(r"\b(\d{4})\b", value)
    if m:
        y = int(m.group(1))
        if 1000 <= y <= 3000:
            return y * 10000 + 101

    return 0


def parse_series_index(value: str) -> float:
    value = clean_text(value).replace(",", ".")
    if not value:
        return 0.0
    m = re.search(r"-?\d+(?:\.\d+)?", value)
    if not m:
        return 0.0
    try:
        return float(m.group(0))
    except Exception:
        return 0.0


def read_html(path: Path):
    raw = path.read_text(errors="ignore")
    soup = BeautifulSoup(raw, "html.parser")
    strip_noise(soup)

    title = soup.title.get_text(" ", strip=True) if soup.title else path.name
    text = soup.get_text(" ", strip=True)

    return {
        "title": clean_text(title),
        "author": "",
        "publisher": "",
        "language": "",
        "date": "",
        "date_sort": 0,
        "identifier": "",
        "subject": "",
        "series": "",
        "series_index": 0.0,
        "text": clean_text(text),
    }


def _tag_texts_from_opf(opf_raw: str, names):
    """Devuelve textos de etiquetas OPF, tolerando namespaces y XML imperfecto."""
    found = []

    try:
        soup = BeautifulSoup(opf_raw, "lxml-xml")
        for name in names:
            for tag in soup.find_all(name):
                value = clean_text(tag.get_text(" ", strip=True))
                if value:
                    found.append(value)
            for tag in soup.find_all(f"dc:{name}"):
                value = clean_text(tag.get_text(" ", strip=True))
                if value:
                    found.append(value)
    except Exception:
        pass

    if not found:
        for name in names:
            pattern = rf"<\s*(?:dc:)?{re.escape(name)}[^>]*>(.*?)<\s*/\s*(?:dc:)?{re.escape(name)}\s*>"
            for match in re.finditer(pattern, opf_raw, re.I | re.S):
                raw = re.sub(r"<[^>]+>", " ", match.group(1))
                value = clean_text(raw)
                if value:
                    found.append(value)

    return list(dict.fromkeys(found))


def _meta_contents_from_opf(opf_raw: str, keys):
    """Extrae <meta name='...' content='...'> y variantes calibre."""
    found = []
    keys_lower = {k.lower() for k in keys}

    try:
        soup = BeautifulSoup(opf_raw, "html.parser")
        for tag in soup.find_all("meta"):
            attrs = {str(k).lower(): v for k, v in tag.attrs.items()}
            candidates = []
            for attr_name in ("name", "property"):
                v = attrs.get(attr_name)
                if isinstance(v, list):
                    candidates.extend(str(x).lower() for x in v)
                elif v is not None:
                    candidates.append(str(v).lower())
            if any(c in keys_lower for c in candidates):
                content = attrs.get("content")
                if isinstance(content, list):
                    content = " ".join(map(str, content))
                value = clean_text(str(content or tag.get_text(" ", strip=True)))
                if value:
                    found.append(value)
    except Exception:
        pass

    if not found:
        for key in keys:
            # name="calibre:series" content="..."
            pattern = (
                rf"<\s*meta[^>]+(?:name|property)\s*=\s*['\"]{re.escape(key)}['\"][^>]+"
                rf"content\s*=\s*['\"]([^'\"]+)['\"][^>]*>"
            )
            for match in re.finditer(pattern, opf_raw, re.I | re.S):
                value = clean_text(match.group(1))
                if value:
                    found.append(value)

    return list(dict.fromkeys(found))


def opf_first(opf_raw: str, names, fallback: str = "") -> str:
    values = _tag_texts_from_opf(opf_raw, names)
    return values[0] if values else fallback


def opf_join(opf_raw: str, names) -> str:
    values = _tag_texts_from_opf(opf_raw, names)
    return "; ".join(values)


def metadata_from_opf(opf_raw: str, fallback_title: str) -> dict:
    date = opf_first(opf_raw, ["date"], "")

    series_values = _meta_contents_from_opf(
        opf_raw,
        [
            "calibre:series",
            "belongs-to-collection",
            "collection",
            "series",
        ],
    )
    series_index_values = _meta_contents_from_opf(
        opf_raw,
        [
            "calibre:series_index",
            "group-position",
            "series_index",
            "series-index",
        ],
    )

    return {
        "title": opf_first(opf_raw, ["title"], fallback_title),
        "author": opf_join(opf_raw, ["creator"]),
        "publisher": opf_join(opf_raw, ["publisher"]),
        "language": opf_first(opf_raw, ["language"], ""),
        "date": date,
        "date_sort": parse_date_sort(date),
        "identifier": opf_join(opf_raw, ["identifier"]),
        "subject": opf_join(opf_raw, ["subject"]),
        "series": series_values[0] if series_values else "",
        "series_index": parse_series_index(series_index_values[0]) if series_index_values else 0.0,
    }


def read_epub(path: Path):
    """
    Lee EPUB directamente como ZIP.

    Evita depender de ebooklib. Para indexar basta extraer OPF y texto de
    los .xhtml/.html internos que existan.
    """
    data = {
        "title": path.stem,
        "author": "",
        "publisher": "",
        "language": "",
        "date": "",
        "date_sort": 0,
        "identifier": "",
        "subject": "",
        "series": "",
        "series_index": 0.0,
        "text": "",
    }
    parts = []

    with zipfile.ZipFile(path, "r") as z:
        names = z.namelist()

        opf_names = sorted(
            [n for n in names if n.lower().endswith(".opf")],
            key=str.casefold,
        )
        for opf_name in opf_names[:3]:
            try:
                opf_raw = z.read(opf_name).decode("utf-8", errors="ignore")
                data.update(metadata_from_opf(opf_raw, data["title"]))
                break
            except Exception:
                continue

        html_names = []
        for n in names:
            low = n.lower()
            if n.endswith("/"):
                continue
            if not low.endswith(TEXT_EXTS):
                continue
            base = Path(low).name
            penalty = 1 if base in SKIP_EPUB_NAMES else 0
            html_names.append((penalty, n))

        html_names.sort(key=lambda item: (item[0], item[1].casefold()))

        for _, name in html_names:
            try:
                raw = z.read(name).decode("utf-8", errors="ignore")
                soup = BeautifulSoup(raw, "html.parser")
                strip_noise(soup)
                text = soup.get_text(" ", strip=True)
                if text:
                    parts.append(text)
            except Exception:
                continue

    data["text"] = clean_text(" ".join(parts))
    data["title"] = clean_text(data.get("title") or path.stem)
    for key in ("author", "publisher", "language", "date", "identifier", "subject", "series"):
        data[key] = clean_text(data.get(key, ""))
    data["date_sort"] = int(data.get("date_sort") or parse_date_sort(data.get("date", "")))
    data["series_index"] = float(data.get("series_index") or 0.0)

    return data


def init_db():
    con = sqlite3.connect(DB, timeout=60)

    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA temp_store=MEMORY")

    con.execute("""
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY,
            uuid TEXT,
            kind TEXT NOT NULL,
            title TEXT NOT NULL,
            author TEXT DEFAULT '',
            publisher TEXT DEFAULT '',
            language TEXT DEFAULT '',
            date TEXT DEFAULT '',
            date_sort INTEGER DEFAULT 0,
            identifier TEXT DEFAULT '',
            subject TEXT DEFAULT '',
            series TEXT DEFAULT '',
            series_index REAL DEFAULT 0,
            path TEXT NOT NULL UNIQUE,
            domain TEXT,
            cover TEXT DEFAULT '',
            mtime INTEGER NOT NULL,
            size INTEGER NOT NULL,
            indexed_at INTEGER NOT NULL
        )
    """)

    existing_cols = {row[1] for row in con.execute("PRAGMA table_info(documents)")}
    migrations = {
        "uuid": "TEXT DEFAULT ''",
        "author": "TEXT DEFAULT ''",
        "publisher": "TEXT DEFAULT ''",
        "language": "TEXT DEFAULT ''",
        "date": "TEXT DEFAULT ''",
        "date_sort": "INTEGER DEFAULT 0",
        "identifier": "TEXT DEFAULT ''",
        "subject": "TEXT DEFAULT ''",
        "series": "TEXT DEFAULT ''",
        "series_index": "REAL DEFAULT 0",
        "cover": "TEXT DEFAULT ''",
    }
    for col, definition in migrations.items():
        if col not in existing_cols:
            print(f"Migrando documents.{col}...", flush=True)
            con.execute(f"ALTER TABLE documents ADD COLUMN {col} {definition}")

    fts_schema = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='documents_fts'"
    ).fetchone()
    if fts_schema and fts_schema[0]:
        sql = fts_schema[0]
        required = ["author", "publisher", "language", "identifier", "subject", "text"]
        missing = [name for name in required if name not in sql]
        if missing:
            raise SystemExit(
                "ERROR: documents_fts existe con esquema antiguo. "
                "Para crear la base desde cero borra primero: "
                "webvault.db webvault.db-wal webvault.db-shm"
            )

    con.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts
        USING fts5(title, author, publisher, language, identifier, subject, text)
    """)

    con.execute("CREATE INDEX IF NOT EXISTS idx_documents_path ON documents(path)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_documents_title ON documents(title)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_documents_author ON documents(author)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_documents_kind ON documents(kind)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_documents_cover ON documents(cover)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_documents_date_sort ON documents(date_sort)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_documents_series ON documents(series)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_documents_uuid ON documents(uuid)")

    con.commit()
    return con


def stat_file(path: Path):
    st = path.stat()
    return int(st.st_mtime), int(st.st_size)


def get_existing(con, path: Path):
    return con.execute(
        "SELECT id, mtime, size FROM documents WHERE path=?",
        (str(path),),
    ).fetchone()


def get_uuid_existing(con, uuid_value: str):
    return con.execute(
        "SELECT id FROM documents WHERE uuid=? LIMIT 1",
        (uuid_value,),
    ).fetchone()


def make_unique_uuid(con, content_hash: str, path: Path) -> str:
    """Usa hash de contenido; si hay duplicado exacto, añade sufijo de ruta."""
    if not get_uuid_existing(con, content_hash):
        return content_hash
    path_suffix = hashlib.sha1(str(path).encode("utf-8", errors="ignore")).hexdigest()[:12]
    return f"{content_hash}-{path_suffix}"


def delete_existing(con, doc_id: int):
    con.execute("DELETE FROM documents_fts WHERE rowid=?", (doc_id,))
    con.execute("DELETE FROM documents WHERE id=?", (doc_id,))


def infer_domain(path: Path):
    parts = path.parts
    if len(parts) >= 2 and parts[0] == "a":
        return parts[1]
    return None


def iter_html_files():
    if not HTML_ROOT.exists():
        return []

    files = []
    for root, dirs, names in os.walk(HTML_ROOT, followlinks=True):
        dirs.sort(key=str.casefold)
        names.sort(key=str.casefold)
        root_path = Path(root)
        if "pdf" in root_path.parts:
            continue
        for name in names:
            if name.lower().endswith(".html"):
                files.append(root_path / name)

    return files


def iter_epub_files():
    if not EPUB_ROOT.exists():
        return []

    files = []
    for root, dirs, names in os.walk(EPUB_ROOT, followlinks=True):
        dirs.sort(key=str.casefold)
        names.sort(key=str.casefold)
        root_path = Path(root)
        for name in names:
            if name.lower().endswith(".epub"):
                files.append(root_path / name)

    return files


def should_skip(con, path: Path):
    try:
        mtime, size = stat_file(path)
    except Exception:
        return False, None, None, None

    existing = get_existing(con, path)
    if existing:
        doc_id, old_mtime, old_size = existing
        if int(old_mtime) == mtime and int(old_size) == size:
            return True, doc_id, mtime, size
        return False, doc_id, mtime, size

    return False, None, mtime, size


def parse_document(path: Path, kind: str):
    try:
        mtime, size = stat_file(path)

        if kind == "HTML":
            data = read_html(path)
        elif kind == "EPUB":
            data = read_epub(path)
        else:
            return {
                "status": "error",
                "path": path,
                "kind": kind,
                "error": "Tipo desconocido",
            }

        if not data.get("text"):
            return {
                "status": "empty",
                "path": path,
                "kind": kind,
                "mtime": mtime,
                "size": size,
            }

        return {
            "status": "parsed",
            "path": path,
            "kind": kind,
            "uuid_base": file_sha1(path),
            "title": data.get("title") or path.stem,
            "author": data.get("author", ""),
            "publisher": data.get("publisher", ""),
            "language": data.get("language", ""),
            "date": data.get("date", ""),
            "date_sort": int(data.get("date_sort") or 0),
            "identifier": data.get("identifier", ""),
            "subject": data.get("subject", ""),
            "series": data.get("series", ""),
            "series_index": float(data.get("series_index") or 0.0),
            "text": data.get("text", ""),
            "domain": infer_domain(path),
            "mtime": mtime,
            "size": size,
        }

    except zipfile.BadZipFile:
        return {
            "status": "error",
            "path": path,
            "kind": kind,
            "error": "EPUB/ZIP corrupto o ilegible",
        }
    except Exception as e:
        return {
            "status": "error",
            "path": path,
            "kind": kind,
            "error": str(e),
        }


def write_document(con, result):
    path = result["path"]
    kind = result["kind"]

    existing = get_existing(con, path)
    if existing:
        doc_id, old_mtime, old_size = existing
        if int(old_mtime) == int(result["mtime"]) and int(old_size) == int(result["size"]):
            return "skip"
        delete_existing(con, doc_id)

    uuid_value = make_unique_uuid(con, result.get("uuid_base", ""), path)

    cur = con.execute(
        """
        INSERT INTO documents(
            uuid, kind, title, author, publisher, language, date, date_sort,
            identifier, subject, series, series_index, path, domain,
            mtime, size, indexed_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            uuid_value,
            kind,
            result["title"],
            result.get("author", ""),
            result.get("publisher", ""),
            result.get("language", ""),
            result.get("date", ""),
            int(result.get("date_sort") or 0),
            result.get("identifier", ""),
            result.get("subject", ""),
            result.get("series", ""),
            float(result.get("series_index") or 0.0),
            str(path),
            result["domain"],
            int(result["mtime"]),
            int(result["size"]),
            int(time.time()),
        ),
    )

    doc_id = cur.lastrowid

    con.execute(
        """
        INSERT INTO documents_fts(
            rowid, title, author, publisher, language, identifier, subject, text
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            doc_id,
            result["title"],
            result.get("author", ""),
            result.get("publisher", ""),
            result.get("language", ""),
            result.get("identifier", ""),
            result.get("subject", ""),
            result["text"],
        ),
    )

    return "indexed"


def process_batch(executor, batch, kind: str):
    futures = [executor.submit(parse_document, path, kind) for path in batch]
    for future in futures:
        yield future.result()


def index_files(con, files, kind: str, workers: int, commit_every: int, batch_size: int):
    stats = {
        "indexed": 0,
        "skip": 0,
        "error": 0,
        "empty": 0,
    }

    to_parse = []

    print(f"\nComprobando {kind}...")
    for path in tqdm(files, desc=f"Comprobando {kind}", unit="doc"):
        skip, _doc_id, _mtime, _size = should_skip(con, path)
        if skip:
            stats["skip"] += 1
        else:
            to_parse.append(path)

    print(f"{kind}: total={len(files)}, sin cambios={stats['skip']}, por indexar={len(to_parse)}")

    if not to_parse:
        return stats

    changed = 0
    batch_size = max(workers, batch_size)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        with tqdm(total=len(to_parse), desc=f"Indexando {kind}", unit="doc") as bar:
            for start in range(0, len(to_parse), batch_size):
                batch = to_parse[start:start + batch_size]

                for result in process_batch(executor, batch, kind):
                    status = result["status"]

                    if status == "parsed":
                        write_status = write_document(con, result)
                        stats[write_status] = stats.get(write_status, 0) + 1
                        changed += 1

                    elif status == "empty":
                        stats["empty"] += 1
                        tqdm.write(f"VACÍO: {result['path']}")

                    else:
                        stats["error"] += 1
                        tqdm.write(f"ERROR leyendo {result['path']}: {result.get('error', '')}")

                    if changed and changed % commit_every == 0:
                        con.commit()

                    bar.set_postfix({
                        "ok": stats["indexed"],
                        "skip": stats["skip"],
                        "empty": stats["empty"],
                        "err": stats["error"],
                    })
                    bar.update(1)

    con.commit()
    return stats


def main():
    parser = argparse.ArgumentParser(
        description="Indexador WebVault robusto, alfabético, multihilo y con metadatos avanzados."
    )
    parser.add_argument("--workers", type=int, default=8, help="Número de hilos lectores. Por defecto: 8")
    parser.add_argument("--commit-every", type=int, default=100, help="Commit cada N documentos. Por defecto: 100")
    parser.add_argument("--batch-size", type=int, default=64, help="Tamaño de lote. Por defecto: 64")
    parser.add_argument("--only", choices=["html", "epub", "all"], default="all", help="Qué indexar. Por defecto: all")
    args = parser.parse_args()

    workers = max(1, min(args.workers, 32))
    commit_every = max(1, args.commit_every)
    batch_size = max(workers, args.batch_size)

    con = init_db()

    total_stats = {
        "indexed": 0,
        "skip": 0,
        "error": 0,
        "empty": 0,
    }

    try:
        if args.only in ("html", "all"):
            html_files = iter_html_files()
            stats = index_files(con, html_files, "HTML", workers, commit_every, batch_size)
            for k, v in stats.items():
                total_stats[k] = total_stats.get(k, 0) + v

        if args.only in ("epub", "all"):
            epub_files = iter_epub_files()
            stats = index_files(con, epub_files, "EPUB", workers, commit_every, batch_size)
            for k, v in stats.items():
                total_stats[k] = total_stats.get(k, 0) + v

        con.commit()

        print("\nCheckpoint WAL...")
        con.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        con.commit()

    finally:
        con.close()

    print()
    print("Resumen:")
    print(f"  Indexados/reindexados: {total_stats.get('indexed', 0)}")
    print(f"  Sin cambios:           {total_stats.get('skip', 0)}")
    print(f"  Vacíos:                {total_stats.get('empty', 0)}")
    print(f"  Errores:               {total_stats.get('error', 0)}")
    print()
    print(f"Base de datos: {DB}")


if __name__ == "__main__":
    main()
