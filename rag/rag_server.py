# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=2.0", "numpy"]
# ///
"""
RAG agentico local para o OpenCode — embeddings e geracao 100% no Ollama.

Nao e um retrieval de tiro unico: expoe primitivas para o agente ITERAR —
buscar, avaliar, expandir a vizinhanca do trecho, cruzar com busca literal
e refinar a consulta ate ter contexto suficiente.

Armazenamento: SQLite por colecao em ./store/<colecao>.db
Embeddings:    Ollama /api/embed (padrao: nomic-embed-text, 768d)
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
from mcp.server.mcpserver import MCPServer

# ---------------------------------------------------------------- configuracao

BASE = Path(__file__).resolve().parent
STORE = Path(os.environ.get("RAG_STORE", BASE / "store"))
OLLAMA = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
EMBED_MODEL = os.environ.get("RAG_EMBED_MODEL", "nomic-embed-text")

# Janela de chunk em linhas: grande o bastante para uma funcao/secao inteira,
# com sobreposicao para nao cortar contexto no meio.
CHUNK_LINES = int(os.environ.get("RAG_CHUNK_LINES", "60"))
OVERLAP_LINES = int(os.environ.get("RAG_OVERLAP_LINES", "15"))
MAX_CHARS = int(os.environ.get("RAG_MAX_CHARS", "6000"))

# nomic e mxbai sao treinados com prefixo de tarefa; sem ele a qualidade cai.
PREFIX = {
    "nomic-embed-text": ("search_document: ", "search_query: "),
    "mxbai-embed-large": ("", "Represent this sentence for searching relevant passages: "),
}

TEXT_EXT = {
    ".md", ".markdown", ".txt", ".rst", ".org",
    ".py", ".js", ".ts", ".tsx", ".jsx", ".mjs", ".go", ".rs", ".java", ".kt",
    ".c", ".h", ".cpp", ".hpp", ".cs", ".rb", ".php", ".swift", ".scala", ".lua",
    ".sh", ".bash", ".zsh", ".ps1", ".psm1", ".bat",
    ".sql", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env",
    ".html", ".css", ".scss", ".xml", ".csv", ".tsv", ".log",
}
SKIP_DIRS = {
    ".git", ".svn", "node_modules", "__pycache__", ".venv", "venv", "env",
    "dist", "build", "target", ".next", ".cache", ".mypy_cache", ".pytest_cache",
    ".idea", ".vscode", "site-packages", ".opencode", "store",
}
MAX_FILE_BYTES = int(os.environ.get("RAG_MAX_FILE_BYTES", str(2_000_000)))

mcp = MCPServer("rag-local", instructions="RAG local sobre Ollama. Fluxo agentico: rag_search -> avaliar -> rag_expand/rag_grep -> refinar a consulta -> responder citando arquivo:linha.")

# ------------------------------------------------------------------ armazenagem

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta  (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL,
    sha TEXT NOT NULL, mtime REAL NOT NULL, chunks INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    ord INTEGER NOT NULL, line_start INTEGER NOT NULL, line_end INTEGER NOT NULL,
    text TEXT NOT NULL, vec BLOB NOT NULL);
CREATE INDEX IF NOT EXISTS idx_chunks_file ON chunks(file_id, ord);
"""


def _safe(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", name or ""):
        raise ValueError(f"nome de colecao invalido: {name!r} (use A-Z a-z 0-9 . _ -)")
    return name


def db(collection: str, create: bool = True) -> sqlite3.Connection:
    STORE.mkdir(parents=True, exist_ok=True)
    p = STORE / f"{_safe(collection)}.db"
    if not create and not p.exists():
        raise FileNotFoundError(
            f"colecao '{collection}' nao existe. Rode rag_index primeiro "
            f"ou veja rag_collections."
        )
    con = sqlite3.connect(p)
    con.executescript(SCHEMA)
    return con


# ------------------------------------------------------------------- embeddings

def embed(texts: list[str], is_query: bool) -> np.ndarray:
    """Chama o Ollama em lote. Devolve matriz (n, d) ja normalizada em L2."""
    doc_p, qry_p = PREFIX.get(EMBED_MODEL, ("", ""))
    pref = qry_p if is_query else doc_p
    out: list[list[float]] = []
    B = 32
    for i in range(0, len(texts), B):
        batch = [pref + t for t in texts[i : i + B]]
        req = urllib.request.Request(
            f"{OLLAMA}/api/embed",
            data=json.dumps({"model": EMBED_MODEL, "input": batch}).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                out.extend(json.loads(r.read())["embeddings"])
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"Ollama inacessivel em {OLLAMA} ({e}). O servidor esta rodando? "
                f"O modelo '{EMBED_MODEL}' esta baixado (ollama pull {EMBED_MODEL})?"
            ) from e
    v = np.asarray(out, dtype=np.float32)
    v /= np.linalg.norm(v, axis=1, keepdims=True).clip(min=1e-12)
    return v


def load_vecs(con: sqlite3.Connection) -> tuple[list[int], np.ndarray]:
    ids, blobs = [], []
    for cid, blob in con.execute("SELECT id, vec FROM chunks"):
        ids.append(cid)
        blobs.append(np.frombuffer(blob, dtype=np.float32))
    if not ids:
        return [], np.zeros((0, 0), dtype=np.float32)
    return ids, np.vstack(blobs)


# ------------------------------------------------------------------- chunking

def chunk_file(path: Path) -> list[tuple[int, int, str]]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    if not any(ln.strip() for ln in lines):
        return []
    step = max(1, CHUNK_LINES - OVERLAP_LINES)
    out = []
    for start in range(0, len(lines), step):
        window = lines[start : start + CHUNK_LINES]
        text = "\n".join(window)[:MAX_CHARS]
        if text.strip():
            out.append((start + 1, start + len(window), text))
        if start + CHUNK_LINES >= len(lines):
            break
    return out


def walk(root: Path, globs: list[str]) -> list[Path]:
    if root.is_file():
        return [root]
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            p = Path(dirpath) / fn
            if globs:
                if not any(p.match(g) for g in globs):
                    continue
            elif p.suffix.lower() not in TEXT_EXT:
                continue
            try:
                if p.stat().st_size > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            found.append(p)
    return found


# ---------------------------------------------------------------------- tools

@mcp.tool()
def index(
    path: str,
    collection: str = "default",
    globs: str = "",
    reindex: bool = False,
) -> str:
    """Indexa um arquivo ou diretorio numa colecao para busca semantica posterior.

    Incremental: arquivo cujo conteudo nao mudou (SHA-256) e pulado. Arquivo
    indexado que sumiu do disco e removido da colecao.

    path:       arquivo ou diretorio a indexar (caminho absoluto de preferencia)
    collection: nome da colecao (isole por caso/projeto; ex.: 'caso-a', 'acervo')
    globs:      padroes separados por virgula, ex.: "*.md" ou "*.md,*.py";
                vazio usa a lista de extensoes de texto/codigo conhecidas
    reindex:    True forca reprocessar tudo, ignorando o cache incremental
    """
    root = Path(path).expanduser().resolve()
    if not root.exists():
        return f"ERRO: caminho inexistente: {root}"

    con = db(collection)
    con.execute(
        "INSERT INTO meta(k,v) VALUES('embed_model',?) "
        "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (EMBED_MODEL,),
    )
    known = {r[0]: (r[1], r[2]) for r in con.execute("SELECT path, sha, id FROM files")}

    patterns = [g.strip() for g in globs.split(",") if g.strip()]
    files = walk(root, patterns)
    added = skipped = updated = 0
    total_chunks = 0

    for f in files:
        try:
            raw = f.read_bytes()
        except OSError:
            continue
        sha = hashlib.sha256(raw).hexdigest()
        key = str(f)
        prev = known.get(key)
        if prev and prev[0] == sha and not reindex:
            skipped += 1
            continue

        pieces = chunk_file(f)
        if not pieces:
            continue
        vecs = embed([t for _, _, t in pieces], is_query=False)

        if prev:
            con.execute("DELETE FROM chunks WHERE file_id=?", (prev[1],))
            con.execute(
                "UPDATE files SET sha=?, mtime=?, chunks=? WHERE id=?",
                (sha, f.stat().st_mtime, len(pieces), prev[1]),
            )
            fid = prev[1]
            updated += 1
        else:
            cur = con.execute(
                "INSERT INTO files(path,sha,mtime,chunks) VALUES(?,?,?,?)",
                (key, sha, f.stat().st_mtime, len(pieces)),
            )
            fid = cur.lastrowid
            added += 1

        con.executemany(
            "INSERT INTO chunks(file_id,ord,line_start,line_end,text,vec) "
            "VALUES(?,?,?,?,?,?)",
            [
                (fid, i, ls, le, txt, vecs[i].tobytes())
                for i, (ls, le, txt) in enumerate(pieces)
            ],
        )
        total_chunks += len(pieces)
        con.commit()

    # arquivos que sumiram do disco
    removed = 0
    for p, (_, fid) in known.items():
        if not Path(p).exists():
            con.execute("DELETE FROM chunks WHERE file_id=?", (fid,))
            con.execute("DELETE FROM files WHERE id=?", (fid,))
            removed += 1
    con.commit()

    n_files = con.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    n_chunks = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    con.close()
    return (
        f"Colecao '{collection}' <- {root}\n"
        f"novos {added} | atualizados {updated} | inalterados {skipped} | "
        f"removidos {removed} | chunks gravados {total_chunks}\n"
        f"Total da colecao: {n_files} arquivos, {n_chunks} chunks "
        f"(embeddings: {EMBED_MODEL})"
    )


@mcp.tool()
def search(
    query: str,
    collection: str = "default",
    k: int = 6,
    path_filter: str = "",
    full_text: bool = False,
) -> str:
    """Busca semantica hibrida (cosseno + reforco lexical) na colecao.

    Devolve trechos com chunk_id, arquivo e faixa de linhas. Use o chunk_id em
    rag_expand para abrir a vizinhanca quando o trecho vier cortado.

    query:       pergunta ou descricao do que se procura, em linguagem natural
    collection:  colecao a consultar
    k:           quantos trechos retornar (1-20)
    path_filter: substring do caminho para restringir (ex.: 'Segundo_Cerebro')
    full_text:   True devolve o chunk inteiro; False (padrao) so um preview
    """
    k = max(1, min(int(k), 20))
    con = db(collection, create=False)
    ids, mat = load_vecs(con)
    if not ids:
        con.close()
        return f"Colecao '{collection}' esta vazia. Rode rag_index antes."

    q = embed([query], is_query=True)[0]
    sims = mat @ q

    # reforco lexical: termos raros da consulta que aparecem literalmente no
    # trecho. Cobre identificadores/hashes que o embedding sozinho erra.
    terms = [t.lower() for t in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", query)]
    rows = {
        cid: (p, ls, le, tx)
        for cid, p, ls, le, tx in con.execute(
            "SELECT c.id, f.path, c.line_start, c.line_end, c.text "
            "FROM chunks c JOIN files f ON f.id=c.file_id"
        )
    }
    scored = []
    for i, cid in enumerate(ids):
        p, ls, le, tx = rows[cid]
        if path_filter and path_filter.lower() not in p.lower():
            continue
        low = tx.lower()
        hits = sum(1 for t in set(terms) if t in low)
        bonus = 0.02 * hits
        scored.append((float(sims[i]) + bonus, float(sims[i]), hits, cid, p, ls, le, tx))

    if not scored:
        con.close()
        return f"Nenhum trecho bate com path_filter={path_filter!r}."

    scored.sort(key=lambda r: r[0], reverse=True)
    con.close()

    out = [f"{len(scored)} trechos considerados | top {k} para: {query!r}"]
    for score, cos, hits, cid, p, ls, le, tx in scored[:k]:
        body = tx if full_text else (tx[:900] + ("\n[...]" if len(tx) > 900 else ""))
        out.append(
            f"\n--- chunk_id={cid} | cos={cos:.3f} termos={hits} | "
            f"{p}:{ls}-{le} ---\n{body}"
        )
    return "\n".join(out)


@mcp.tool()
def expand(chunk_id: int, collection: str = "default", radius: int = 1) -> str:
    """Abre a vizinhanca de um trecho: o chunk e os `radius` vizinhos no arquivo.

    Use quando um resultado do rag_search parecer relevante mas cortado.
    """
    con = db(collection, create=False)
    row = con.execute(
        "SELECT c.file_id, c.ord, f.path FROM chunks c JOIN files f ON f.id=c.file_id "
        "WHERE c.id=?",
        (chunk_id,),
    ).fetchone()
    if not row:
        con.close()
        return f"chunk_id {chunk_id} nao existe na colecao '{collection}'."
    fid, ordn, path = row
    rows = con.execute(
        "SELECT id, line_start, line_end, text FROM chunks "
        "WHERE file_id=? AND ord BETWEEN ? AND ? ORDER BY ord",
        (fid, ordn - max(0, radius), ordn + max(0, radius)),
    ).fetchall()
    con.close()
    parts = [f"{path} — vizinhanca do chunk {chunk_id} (raio {radius})"]
    for cid, ls, le, tx in rows:
        mark = "  <<< pedido" if cid == chunk_id else ""
        parts.append(f"\n--- chunk_id={cid} | linhas {ls}-{le}{mark} ---\n{tx}")
    return "\n".join(parts)


@mcp.tool()
def grep(pattern: str, collection: str = "default", k: int = 20) -> str:
    """Busca literal por regex sobre o texto ja indexado (complementa a semantica).

    Use para identificadores exatos, hashes, numeros e nomes de funcao — onde
    embedding erra e casamento literal acerta.
    """
    try:
        rx = re.compile(pattern, re.I | re.M)
    except re.error as e:
        return f"ERRO: regex invalida: {e}"
    con = db(collection, create=False)
    out, n = [], 0
    limit = max(1, min(int(k), 100))
    for cid, p, ls, le, tx in con.execute(
        "SELECT c.id, f.path, c.line_start, c.line_end, c.text "
        "FROM chunks c JOIN files f ON f.id=c.file_id"
    ):
        m = rx.search(tx)
        if m:
            line_off = tx[: m.start()].count("\n")
            lines = tx.splitlines()
            line_txt = lines[line_off] if line_off < len(lines) else ""
            out.append(f"chunk_id={cid} | {p}:{ls + line_off} | {line_txt.strip()[:200]}")
            n += 1
            if n >= limit:
                break
    con.close()
    return "\n".join(out) if out else f"Sem ocorrencia de /{pattern}/ na colecao."


@mcp.tool()
def collections() -> str:
    """Lista as colecoes existentes com contagem de arquivos, chunks e modelo usado."""
    STORE.mkdir(parents=True, exist_ok=True)
    dbs = sorted(STORE.glob("*.db"))
    if not dbs:
        return f"Nenhuma colecao ainda em {STORE}. Comece com rag_index."
    lines = [f"Colecoes em {STORE}:"]
    for p in dbs:
        con = sqlite3.connect(p)
        try:
            nf = con.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            nc = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            em = con.execute("SELECT v FROM meta WHERE k='embed_model'").fetchone()
            lines.append(f"  {p.stem}: {nf} arquivos, {nc} chunks, embed={em[0] if em else '?'}")
        except sqlite3.Error:
            lines.append(f"  {p.stem}: (banco ilegivel)")
        finally:
            con.close()
    return "\n".join(lines)


@mcp.tool()
def sources(collection: str = "default", path_filter: str = "") -> str:
    """Lista os arquivos indexados numa colecao — util para saber o que esta coberto."""
    con = db(collection, create=False)
    rows = con.execute("SELECT path, chunks FROM files ORDER BY path").fetchall()
    con.close()
    rows = [r for r in rows if not path_filter or path_filter.lower() in r[0].lower()]
    if not rows:
        return "Nenhum arquivo bate com o filtro."
    return f"{len(rows)} arquivos:\n" + "\n".join(f"  {p} ({c} chunks)" for p, c in rows)


@mcp.tool()
def forget(collection: str, confirm: bool = False) -> str:
    """Apaga uma colecao inteira. Exige confirm=True — a operacao e irreversivel."""
    p = STORE / f"{_safe(collection)}.db"
    if not p.exists():
        return f"Colecao '{collection}' nao existe."
    if not confirm:
        return f"Nada feito. Para apagar '{collection}' de vez, chame com confirm=True."
    p.unlink()
    return f"Colecao '{collection}' apagada ({p})."


if __name__ == "__main__":
    mcp.run()
