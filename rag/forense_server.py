# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=2.0", "numpy", "networkx", "pillow", "pypdf"]
# ///
"""
Agente investigativo local — camada forense sobre o OpenCode.

Principio de projeto: TUDO que pode ser deterministico e feito em Python
(extracao de identificadores com validador, correlacao, grafo, SQL, hash).
O LLM so orquestra e redige. Isso vale por dois motivos:

  1. Forense — regex com digito verificador e reprodutivel e auditavel;
     "o modelo achou" nao e.
  2. Desempenho — nesta maquina o modelo grande roda 71% em CPU. Cada passo
     do agente custa caro, entao o numero de passos precisa ser pequeno.

Cada afirmacao volta a uma linha de um arquivo com hash registrado.

Armazenamento: um SQLite por caso em ./store/casos/<caso>.db
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

import networkx as nx
import numpy as np
from mcp.server.mcpserver import MCPServer

# ---------------------------------------------------------------- configuracao

BASE = Path(__file__).resolve().parent
STORE = Path(os.environ.get("FORENSE_STORE", BASE / "store" / "casos"))
OLLAMA = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
EMBED_MODEL = os.environ.get("RAG_EMBED_MODEL", "nomic-embed-text")

CHUNK_LINES = int(os.environ.get("RAG_CHUNK_LINES", "60"))
OVERLAP_LINES = int(os.environ.get("RAG_OVERLAP_LINES", "15"))
MAX_CHARS = 6000
MAX_FILE_BYTES = int(os.environ.get("RAG_MAX_FILE_BYTES", str(50_000_000)))

PREFIX = {
    "nomic-embed-text": ("search_document: ", "search_query: "),
    "mxbai-embed-large": ("", "Represent this sentence for searching relevant passages: "),
}

TEXT_EXT = {".txt", ".md", ".log", ".json", ".xml", ".eml", ".vcf", ".sql", ".ini", ".cfg"}
HTML_EXT = {".html", ".htm", ".xhtml"}
TAB_EXT = {".csv", ".tsv"}
PDF_EXT = {".pdf"}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "store", ".opencode"}

mcp = MCPServer(
    "forense",
    instructions=(
        "Agente investigativo forense local. Fluxo: ingest -> buscar/consultar -> "
        "correlacionar -> voltar a fonte (mentions) -> responder separando "
        "EVIDENCIA DIRETA, CORRELACAO e INFERENCIA."
    ),
)

# ------------------------------------------------------------------- esquema

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY,
    path TEXT UNIQUE NOT NULL,
    kind TEXT NOT NULL,
    source_label TEXT DEFAULT '',
    bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    sha512 TEXT NOT NULL,
    mtime REAL NOT NULL,
    ingested_at TEXT NOT NULL,
    lines INTEGER NOT NULL DEFAULT 0,
    extraction TEXT NOT NULL DEFAULT 'texto');
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY,
    doc_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    ord INTEGER NOT NULL, line_start INTEGER NOT NULL, line_end INTEGER NOT NULL,
    text TEXT NOT NULL, vec BLOB NOT NULL);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id, ord);
CREATE TABLE IF NOT EXISTS entities (
    id INTEGER PRIMARY KEY,
    type TEXT NOT NULL, value TEXT NOT NULL, norm TEXT NOT NULL,
    UNIQUE(type, norm));
CREATE TABLE IF NOT EXISTS mentions (
    id INTEGER PRIMARY KEY,
    entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    doc_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    line INTEGER NOT NULL, context TEXT NOT NULL,
    method TEXT NOT NULL, confidence REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_mentions_entity ON mentions(entity_id);
CREATE INDEX IF NOT EXISTS idx_mentions_doc ON mentions(doc_id, line);
CREATE TABLE IF NOT EXISTS relations (
    id INTEGER PRIMARY KEY,
    a_id INTEGER NOT NULL, b_id INTEGER NOT NULL,
    doc_id INTEGER NOT NULL, line INTEGER NOT NULL, kind TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_rel_a ON relations(a_id);
CREATE INDEX IF NOT EXISTS idx_rel_b ON relations(b_id);
CREATE TABLE IF NOT EXISTS steps (
    id INTEGER PRIMARY KEY, ts TEXT NOT NULL, acao TEXT NOT NULL, nota TEXT NOT NULL);
"""


def _safe(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", name or ""):
        raise ValueError(f"nome de caso invalido: {name!r} (use A-Z a-z 0-9 . _ -)")
    return name


def db(caso: str, create: bool = True) -> sqlite3.Connection:
    STORE.mkdir(parents=True, exist_ok=True)
    p = STORE / f"{_safe(caso)}.db"
    if not create and not p.exists():
        raise FileNotFoundError(f"caso '{caso}' nao existe. Use forense_ingest primeiro.")
    con = sqlite3.connect(p, timeout=60)
    con.executescript(SCHEMA)
    return con


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _log(con: sqlite3.Connection, acao: str, nota: str) -> None:
    con.execute("INSERT INTO steps(ts,acao,nota) VALUES(?,?,?)", (_now(), acao, nota[:500]))


# ------------------------------------------------- extracao de identificadores

# A biblioteca forense do usuario (C:\ACERVO\Ferramentas) e a fonte da verdade dos
# validadores: ja passou por producao e tem regressao propria (testar_validacao.py).
# Reimplementar aqui abriria divergencia silenciosa entre o agente e o pipeline.
def _ferramentas_padrao() -> Path:
    """Onde procurar a biblioteca forense quando ACERVO_FERRAMENTAS nao vier.

    O instalador normalmente define a variavel; este padrao cobre quem roda o
    servidor a mao. Fixar o caminho do Windows aqui faria o macOS cair no
    fallback de validadores em silencio — e fallback silencioso e a falha que
    este projeto inteiro tenta nao ter.
    """
    for c in (Path(r"C:\ACERVO\Ferramentas"), Path.home() / "ACERVO" / "Ferramentas"):
        if c.is_dir():
            return c
    return Path.home() / "ACERVO" / "Ferramentas"


FERRAMENTAS = Path(os.environ.get("ACERVO_FERRAMENTAS") or _ferramentas_padrao())
LIB = {"disponivel": False, "motivo": "nao procurado"}
try:
    import sys
    if FERRAMENTAS.is_dir() and str(FERRAMENTAS) not in sys.path:
        sys.path.insert(0, str(FERRAMENTAS))
    import forense_correlacao as _fc
    import forense_validacao as _fv
    import forense_ingestores as _fi
    LIB = {"disponivel": True, "motivo": f"biblioteca em {FERRAMENTAS}"}
except Exception as _e:  # biblioteca ausente -> validadores locais
    _fc = _fv = _fi = None
    LIB = {"disponivel": False, "motivo": f"fallback local ({_e})"}


# --------------------------------------------------------- leitura de texto bruto

# Encodings na ordem em que aparecem em exportacao de fonte oficial brasileira.
# utf-8-sig primeiro: consome o BOM, que senao vira caractere invisivel na
# primeira coluna e estraga o nome dela na tabela SQL.
ENCODINGS = ("utf-8-sig", "utf-8", "cp1252", "latin-1")


def _encoding_de(p: Path) -> str:
    """Descobre o encoding real do arquivo, em vez de assumir UTF-8.

    Assumir UTF-8 em fonte cp1252 nao da erro — da **mojibake dentro do campo**,
    que atravessa a analise inteira e so aparece na peca. Medido em 24/08/2026:
    num acervo real de dados cadastrais, 10 de 17 CSV eram cp1252.
    """
    if _fi is not None:
        try:
            achado = _fi.detectar_encoding(p)
            if achado:
                return achado
        except Exception:
            pass
    bruto = p.read_bytes()[: 1 << 20]
    for enc in ENCODINGS:
        try:
            bruto.decode(enc)
            return enc
        except UnicodeDecodeError:
            continue
    return "latin-1"  # nunca falha; ultimo recurso


def _texto_de(p: Path) -> str:
    return p.read_text(encoding=_encoding_de(p), errors="replace")


def _delimitador_de(p: Path) -> str:
    """Deduz o separador pela linha de cabecalho.

    Exportacao oficial brasileira usa ponto e virgula com frequencia (a virgula
    e separador decimal). Assumir virgula produz **tabela de uma coluna so** —
    que nao da erro, apenas torna o SQL inutil sem avisar.
    """
    try:
        cab = p.read_text(encoding=_encoding_de(p), errors="replace").split("\n", 1)[0]
    except OSError:
        return ";" if p.suffix.lower() != ".tsv" else "\t"
    contagem = {d: cab.count(d) for d in (";", ",", "\t", "|")}
    melhor = max(contagem, key=contagem.get)
    return melhor if contagem[melhor] else ","


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


def _cpf_ok(v: str) -> bool:
    d = _digits(v)
    if _fc is not None:
        return bool(_fc.cpf_valido(d))
    if len(d) != 11 or len(set(d)) == 1:
        return False
    for n in (9, 10):
        s = sum(int(d[i]) * ((n + 1) - i) for i in range(n))
        dv = (s * 10) % 11 % 10
        if dv != int(d[n]):
            return False
    return True


def _cnpj_ok(v: str) -> bool:
    d = _digits(v)
    if _fv is not None:
        return bool(_fv.cnpj_valido(d))
    if len(d) != 14 or len(set(d)) == 1:
        return False
    for n, pesos in ((12, [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]),
                     (13, [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2])):
        s = sum(int(d[i]) * pesos[i] for i in range(n))
        dv = 0 if s % 11 < 2 else 11 - (s % 11)
        if dv != int(d[n]):
            return False
    return True


def _luhn_ok(v: str) -> bool:
    d = _digits(v)
    if len(d) != 15:
        return False
    if _fv is not None:
        try:
            return str(_fv.imei_dv(d[:14])) == d[14]
        except Exception:
            pass
    tot = 0
    for i, ch in enumerate(reversed(d)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        tot += n
    return tot % 10 == 0


def _tel_ok(v: str) -> bool:
    """Telefone valido segundo a biblioteca, a partir do valor BRUTO.

    Contagem de digitos nao basta: DDD inexistente passa. Medido em acervo real
    (24/08/2026) — a regex sozinha aceitou um valor de 11 digitos cujo "DDD" era
    01, e o agente chegou a apresenta-lo como ponto de convergencia.

    O bruto vai inteiro para o validador de proposito: o "+" e o unico sinal que
    separa "+31 ..." (Holanda) de "(31) ..." (Minas), e quem normaliza antes de
    validar destroi essa evidencia.
    """
    if _fv is not None:
        try:
            achados = _fv.validar_telefone(v)
            return not any(a.gravidade == _fv.ERRO for a in achados)
        except Exception:
            pass
    return 10 <= len(_digits(v)) <= 13


def _ipv4_ok(v: str) -> bool:
    parts = v.split(".")
    return len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 and
                                   (p == "0" or not p.startswith("0")) for p in parts)


# (tipo, regex, validador, confianca). Validador corta o ruido que so o regex
# nao corta — mesma filosofia do auditar.py da vault.
PATTERNS: list[tuple[str, re.Pattern, object, float]] = [
    ("cpf", re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b"), _cpf_ok, 0.95),
    ("cnpj", re.compile(r"\b\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}\b"), _cnpj_ok, 0.95),
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), None, 0.95),
    ("ipv4", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), _ipv4_ok, 0.90),
    ("ipv6", re.compile(r"\b(?:[0-9a-fA-F]{1,4}:){4,7}[0-9a-fA-F]{1,4}\b"), None, 0.85),
    ("imei", re.compile(r"\b\d{15}\b"), _luhn_ok, 0.90),
    ("telefone", re.compile(r"(?:\+55\s?)?\(?\b\d{2}\)?\s?9?\d{4}[-\s]?\d{4}\b"),
     _tel_ok, 0.75),
    ("mac", re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b"), None, 0.90),
    ("placa", re.compile(r"\b[A-Z]{3}-?\d[A-Z0-9]\d{2}\b"), None, 0.80),
    ("processo_cnj", re.compile(r"\b\d{7}-?\d{2}\.?\d{4}\.?\d\.?\d{2}\.?\d{4}\b"), None, 0.95),
    ("url", re.compile(r"\bhttps?://[^\s<>\"')]{4,200}"), None, 0.90),
    ("hash", re.compile(r"\b[0-9a-fA-F]{32,64}\b"),
     lambda v: len(v) in (32, 40, 64), 0.85),
    ("btc", re.compile(r"\b(?:bc1[a-z0-9]{25,62}|[13][a-km-zA-HJ-NP-Z1-9]{25,34})\b"), None, 0.75),
    ("usuario", re.compile(r"(?<![\w@.])@[A-Za-z0-9._]{3,30}\b"), None, 0.70),
    ("timestamp", re.compile(
        r"\b\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?(?:\s?UTC[+-]?\d{0,2})?\b"), None, 0.90),
]

# Faixas que poluem o grafo sem agregar vinculo (loopback, privadas, multicast).
MAC_FORMA = re.compile(r"(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}")
IP_RUIDO = re.compile(r"^(?:0\.|127\.|10\.|192\.168\.|169\.254\.|22[4-9]\.|2[3-5]\d\.|172\.(?:1[6-9]|2\d|3[01])\.)")


def _norm(tipo: str, valor: str) -> str:
    if tipo in ("cpf", "cnpj", "imei", "telefone", "processo_cnj"):
        return _digits(valor)
    if tipo in ("email", "url", "usuario", "hash", "mac", "ipv6"):
        return valor.lower().strip().rstrip(".,;)")
    if tipo == "placa":
        return valor.replace("-", "").upper()
    return valor.strip()


# Quando o MESMO conjunto de digitos e aceito por mais de um tipo, vence o que
# tem verificacao mais forte. Medido em acervo real: um valor de 11 digitos com
# DV de CPF valido tambem casa com a forma de telefone e entrava como DUAS
# entidades — o grafo passaria a ligar pessoas por um "telefone" que e um CPF.
# Digito verificador (CPF/CNPJ) e Luhn (IMEI) sao evidencia; "tem 11 digitos"
# nao e. Numero menor = precedencia maior.
PRECEDENCIA = {"processo_cnj": 0, "cnpj": 1, "cpf": 2, "imei": 3, "telefone": 4}


def extrair(linha: str) -> list[tuple[str, str, float]]:
    """Devolve [(tipo, valor, confianca)] achados numa linha, ja validados.

    Extrai por LINHA, nao por coluna — logo nao sabe que o campo se chama "CPF".
    Por isso a precedencia acima existe: ela resolve a ambiguidade pelo unico
    criterio disponivel sem o cabecalho, que e a forca da verificacao.
    """
    achados = []
    vistos = set()
    for tipo, rx, valida, conf in PATTERNS:
        for m in rx.finditer(linha):
            v = m.group(0)
            if valida and not valida(v):
                continue
            if tipo == "ipv4" and IP_RUIDO.match(v):
                continue
            # Endereco MAC casa com a forma generica de IPv6 (6 grupos de 2 hex
            # separados por dois-pontos). Sem esta guarda, o MESMO endereco entra
            # como duas entidades distintas e vira dois nos no grafo de vinculos —
            # medido em acervo real: 13 de 13 "IPv6" eram MAC.
            if tipo == "ipv6" and MAC_FORMA.fullmatch(v):
                continue
            if tipo == "hash" and re.fullmatch(r"\d+", v):
                continue  # sequencia so de digitos raramente e hash
            n = _norm(tipo, v)
            if (tipo, n) in vistos:
                continue
            vistos.add((tipo, n))
            achados.append((tipo, v, conf))

    # resolve colisao entre tipos que disputam o MESMO conjunto de digitos
    melhor: dict[str, tuple[str, str, float]] = {}
    livres = []
    for tipo, v, conf in achados:
        if tipo not in PRECEDENCIA:
            livres.append((tipo, v, conf))
            continue
        chave = _digits(v)
        atual = melhor.get(chave)
        if atual is None or PRECEDENCIA[tipo] < PRECEDENCIA[atual[0]]:
            melhor[chave] = (tipo, v, conf)
    return livres + list(melhor.values())


# ------------------------------------------------------------------ embeddings

def embed(texts: list[str], is_query: bool) -> np.ndarray:
    doc_p, qry_p = PREFIX.get(EMBED_MODEL, ("", ""))
    pref = qry_p if is_query else doc_p
    out: list[list[float]] = []
    for i in range(0, len(texts), 32):
        batch = [pref + t for t in texts[i : i + 32]]
        req = urllib.request.Request(
            f"{OLLAMA}/api/embed",
            data=json.dumps({"model": EMBED_MODEL, "input": batch}).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=900) as r:
                out.extend(json.loads(r.read())["embeddings"])
        except urllib.error.URLError as e:
            raise RuntimeError(f"Ollama inacessivel em {OLLAMA}: {e}") from e
    v = np.asarray(out, dtype=np.float32)
    v /= np.linalg.norm(v, axis=1, keepdims=True).clip(min=1e-12)
    return v


# --------------------------------------------------------- extracao de texto

class _Strip(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.buf: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        elif tag in ("br", "p", "div", "tr", "li", "h1", "h2", "h3", "td", "th"):
            self.buf.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip:
            self.skip -= 1
        elif tag in ("p", "div", "tr", "li", "td", "th"):
            self.buf.append("\n")

    def handle_data(self, data):
        if not self.skip and data.strip():
            self.buf.append(data.strip() + " ")


def _poppler(tool: str) -> str | None:
    return shutil.which(tool)


def pdf_texto(p: Path, ocr: bool) -> tuple[str, str]:
    """Devolve (texto, metodo). Cai para OCR quando nao ha camada de texto."""
    exe = _poppler("pdftotext")
    txt = ""
    if exe:
        try:
            r = subprocess.run([exe, "-layout", "-enc", "UTF-8", str(p), "-"],
                               capture_output=True, timeout=600)
            txt = r.stdout.decode("utf-8", "replace")
        except (subprocess.SubprocessError, OSError):
            txt = ""
    if not txt.strip():
        try:
            from pypdf import PdfReader
            txt = "\n".join((pg.extract_text() or "") for pg in PdfReader(str(p)).pages)
        except Exception:
            txt = ""
    if txt.strip() or not ocr:
        return txt, ("texto" if txt.strip() else "vazio")
    return _ocr_pdf(p), "ocr"


def _ocr_pdf(p: Path) -> str:
    """OCR na calibracao medida da vault: 300 dpi, cinza, 2x LANCZOS, contraste 3.0,
    --psm 4 -l por. 600 dpi piora — nao aumentar."""
    topng, tess = _poppler("pdftoppm"), _poppler("tesseract")
    if not (topng and tess):
        return ""
    from PIL import Image, ImageEnhance
    saida = []
    with tempfile.TemporaryDirectory() as td:
        subprocess.run([topng, "-r", "300", "-gray", "-png", str(p), f"{td}/pg"],
                       capture_output=True, timeout=1800)
        for img in sorted(Path(td).glob("pg*.png")):
            im = Image.open(img).convert("L")
            im = im.resize((im.width * 2, im.height * 2), Image.LANCZOS)
            im = ImageEnhance.Contrast(im).enhance(3.0)
            im.save(img)
            r = subprocess.run([tess, str(img), "stdout", "--psm", "4", "-l", "por"],
                               capture_output=True, timeout=600)
            saida.append(r.stdout.decode("utf-8", "replace"))
    return "\n".join(saida)


def ler_documento(p: Path, ocr: bool) -> tuple[str, str, str]:
    """Devolve (texto, kind, metodo_extracao)."""
    ext = p.suffix.lower()
    if ext in PDF_EXT:
        t, m = pdf_texto(p, ocr)
        return t, "pdf", m
    if ext in HTML_EXT:
        s = _Strip()
        s.feed(_texto_de(p))
        return re.sub(r"\n{3,}", "\n\n", "".join(s.buf)), "html", "html-strip"
    if ext in TAB_EXT:
        return _texto_de(p), "tabela", "texto"
    return _texto_de(p), "texto", "texto"


# ------------------------------------------------------- ingestao de tabelas

def _tabela_sql(con: sqlite3.Connection, p: Path, doc_id: int) -> str:
    """CSV/TSV vira tabela REAL no SQLite, para o agente consultar com SQL de
    verdade em vez de pedir ao modelo que 'entenda' a planilha por embedding."""
    nome = "t_" + re.sub(r"[^a-z0-9_]", "_", p.stem.lower())[:40]
    delim = "\t" if p.suffix.lower() == ".tsv" else _delimitador_de(p)
    with p.open(encoding=_encoding_de(p), errors="replace", newline="") as fh:
        rd = csv.reader(fh, delimiter=delim)
        try:
            head = next(rd)
        except StopIteration:
            return ""
        cols, usados = [], set()
        for i, h in enumerate(head):
            c = re.sub(r"[^a-z0-9_]", "_", (h or f"col{i}").strip().lower())[:40] or f"col{i}"
            while c in usados:
                c += "_"
            usados.add(c)
            cols.append(c)
        con.execute(f"DROP TABLE IF EXISTS {nome}")
        con.execute(
            f"CREATE TABLE {nome} (_doc_id INTEGER, _linha INTEGER, "
            + ", ".join(f"{c} TEXT" for c in cols) + ")"
        )
        ph = ",".join("?" * (len(cols) + 2))
        lote, n = [], 0
        for i, row in enumerate(rd, start=2):
            row = (row + [""] * len(cols))[: len(cols)]
            lote.append([doc_id, i] + row)
            n += 1
            if len(lote) >= 2000:
                con.executemany(f"INSERT INTO {nome} VALUES ({ph})", lote)
                lote = []
        if lote:
            con.executemany(f"INSERT INTO {nome} VALUES ({ph})", lote)
    return f"{nome} ({n} linhas, colunas: {', '.join(cols)})"


# ---------------------------------------------------------------------- tools

@mcp.tool()
def ingest(
    path: str,
    caso: str,
    globs: str = "",
    ocr: bool = False,
    source_label: str = "",
) -> str:
    """Ingere arquivo/diretorio num caso: hash de custodia, texto, entidades e indice.

    Nunca escreve no material de origem — so le. Registra SHA-256 e SHA-512 de
    cada arquivo no momento da ingestao (custodia). PDF sem camada de texto so e
    lido com ocr=True. CSV/TSV viram tabela SQL consultavel por forense_sql.

    path:         arquivo ou diretorio de trabalho (NUNCA a midia original)
    caso:         identificador do caso; isola tudo num banco proprio
    globs:        filtros por virgula, ex.: "*.pdf,*.html" (vazio = todos suportados)
    ocr:          True liga OCR para PDF sem texto (lento; 300dpi/psm4/por)
    source_label: origem do material, ex.: "META", "operadora", "extracao Cellebrite"
    """
    root = Path(path).expanduser().resolve()
    if not root.exists():
        return f"ERRO: caminho inexistente: {root}"
    con = db(caso)
    pats = [g.strip() for g in globs.split(",") if g.strip()]
    sup = TEXT_EXT | HTML_EXT | TAB_EXT | PDF_EXT

    alvos: list[Path] = []
    if root.is_file():
        alvos = [root]
    else:
        for dp, dn, fn in os.walk(root):
            dn[:] = [d for d in dn if d not in SKIP_DIRS and not d.startswith(".")]
            for f in fn:
                q = Path(dp) / f
                if pats and not any(q.match(g) for g in pats):
                    continue
                if not pats and q.suffix.lower() not in sup:
                    continue
                try:
                    if q.stat().st_size > MAX_FILE_BYTES:
                        continue
                except OSError:
                    continue
                alvos.append(q)

    novos = pulados = 0
    tot_ents = 0
    tabelas: list[str] = []
    for f in alvos:
        raw = f.read_bytes()
        s256 = hashlib.sha256(raw).hexdigest()
        s512 = hashlib.sha512(raw).hexdigest()
        prev = con.execute("SELECT id, sha256 FROM documents WHERE path=?", (str(f),)).fetchone()
        if prev and prev[1] == s256:
            pulados += 1
            continue
        if prev:
            con.execute("DELETE FROM documents WHERE id=?", (prev[0],))
            con.execute("DELETE FROM chunks WHERE doc_id=?", (prev[0],))
            con.execute("DELETE FROM mentions WHERE doc_id=?", (prev[0],))
            con.execute("DELETE FROM relations WHERE doc_id=?", (prev[0],))

        texto, kind, metodo = ler_documento(f, ocr)
        linhas = texto.splitlines()
        cur = con.execute(
            "INSERT INTO documents(path,kind,source_label,bytes,sha256,sha512,mtime,"
            "ingested_at,lines,extraction) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (str(f), kind, source_label, len(raw), s256, s512, f.stat().st_mtime,
             _now(), len(linhas), metodo),
        )
        doc_id = cur.lastrowid

        if kind == "tabela":
            t = _tabela_sql(con, f, doc_id)
            if t:
                tabelas.append(t)

        # entidades linha a linha -> proveniencia exata + vinculo por co-ocorrencia
        for ln, linha in enumerate(linhas, start=1):
            if not linha.strip() or len(linha) > 20000:
                continue
            achados = extrair(linha)
            if not achados:
                continue
            ids = []
            for tipo, valor, conf in achados:
                n = _norm(tipo, valor)
                con.execute(
                    "INSERT INTO entities(type,value,norm) VALUES(?,?,?) "
                    "ON CONFLICT(type,norm) DO NOTHING", (tipo, valor, n))
                eid = con.execute(
                    "SELECT id FROM entities WHERE type=? AND norm=?", (tipo, n)).fetchone()[0]
                con.execute(
                    "INSERT INTO mentions(entity_id,doc_id,line,context,method,confidence) "
                    "VALUES(?,?,?,?,?,?)",
                    (eid, doc_id, ln, linha.strip()[:300], f"regex:{tipo}", conf))
                ids.append(eid)
                tot_ents += 1
            # entidades na MESMA linha/registro = vinculo forte
            for i in range(len(ids)):
                for j in range(i + 1, len(ids)):
                    con.execute(
                        "INSERT INTO relations(a_id,b_id,doc_id,line,kind) VALUES(?,?,?,?,?)",
                        (ids[i], ids[j], doc_id, ln, "mesma-linha"))

        if linhas:
            step = max(1, CHUNK_LINES - OVERLAP_LINES)
            pedacos = []
            for st in range(0, len(linhas), step):
                w = linhas[st : st + CHUNK_LINES]
                t = "\n".join(w)[:MAX_CHARS]
                if t.strip():
                    pedacos.append((st + 1, st + len(w), t))
                if st + CHUNK_LINES >= len(linhas):
                    break
            if pedacos:
                vs = embed([t for _, _, t in pedacos], is_query=False)
                con.executemany(
                    "INSERT INTO chunks(doc_id,ord,line_start,line_end,text,vec) "
                    "VALUES(?,?,?,?,?,?)",
                    [(doc_id, i, a, b, t, vs[i].tobytes())
                     for i, (a, b, t) in enumerate(pedacos)])
        novos += 1
        con.commit()

    _log(con, "ingest", f"{root} -> {novos} novos, {pulados} inalterados")
    nd = con.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    ne = con.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    nm = con.execute("SELECT COUNT(*) FROM mentions").fetchone()[0]
    con.commit()
    con.close()
    out = [f"Caso '{caso}' <- {root}",
           f"documentos novos {novos} | inalterados {pulados} | mencoes gravadas {tot_ents}",
           f"Total: {nd} documentos, {ne} entidades distintas, {nm} mencoes"]
    if tabelas:
        out.append("Tabelas SQL criadas: " + "; ".join(tabelas))
    return "\n".join(out)


@mcp.tool()
def status(caso: str) -> str:
    """Panorama do caso: documentos, entidades por tipo, tabelas SQL e passos ja dados."""
    con = db(caso, create=False)
    nd, nb = con.execute("SELECT COUNT(*), COALESCE(SUM(bytes),0) FROM documents").fetchone()
    linhas = [f"CASO '{caso}' — {nd} documentos ({nb/1e6:.1f} MB)"]
    linhas.append("\nPor tipo de entidade:")
    for t, n in con.execute(
        "SELECT e.type, COUNT(DISTINCT e.id) FROM entities e GROUP BY e.type ORDER BY 2 DESC"
    ):
        linhas.append(f"  {t:14s} {n}")
    tabs = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 't\\_%' ESCAPE '\\'")]
    if tabs:
        linhas.append("\nTabelas SQL: " + ", ".join(tabs))
    linhas.append("\nDocumentos:")
    for p, k, lab, ln, ex in con.execute(
        "SELECT path, kind, source_label, lines, extraction FROM documents ORDER BY id LIMIT 40"
    ):
        linhas.append(f"  [{k}/{ex}] {p} ({ln} linhas) {('origem=' + lab) if lab else ''}")
    ns = con.execute("SELECT COUNT(*) FROM steps").fetchone()[0]
    linhas.append(f"\nPassos registrados: {ns} (veja forense_historico)")
    con.close()
    return "\n".join(linhas)


@mcp.tool()
def search_semantic(query: str, caso: str, k: int = 6, path_filter: str = "") -> str:
    """Busca por SIGNIFICADO — use para conceitos ('movimentacao financeira',
    'usuario acessou a conta'), nao para identificadores exatos."""
    k = max(1, min(int(k), 20))
    con = db(caso, create=False)
    ids, blobs = [], []
    for cid, b in con.execute("SELECT id, vec FROM chunks"):
        ids.append(cid)
        blobs.append(np.frombuffer(b, dtype=np.float32))
    if not ids:
        con.close()
        return f"Caso '{caso}' sem indice vetorial. Rode forense_ingest."
    mat = np.vstack(blobs)
    q = embed([query], is_query=True)[0]
    sims = mat @ q
    linhas_q = [t.lower() for t in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", query)]
    rows = {cid: r for cid, *r in con.execute(
        "SELECT c.id, d.path, c.line_start, c.line_end, c.text "
        "FROM chunks c JOIN documents d ON d.id=c.doc_id")}
    sc = []
    for i, cid in enumerate(ids):
        p, a, b, tx = rows[cid]
        if path_filter and path_filter.lower() not in p.lower():
            continue
        hits = sum(1 for t in set(linhas_q) if t in tx.lower())
        sc.append((float(sims[i]) + 0.02 * hits, float(sims[i]), cid, p, a, b, tx))
    con.close()
    if not sc:
        return "Nada bate com o path_filter."
    sc.sort(key=lambda r: r[0], reverse=True)
    out = [f"top {k} de {len(sc)} trechos para: {query!r}"]
    for _, cos, cid, p, a, b, tx in sc[:k]:
        out.append(f"\n--- chunk_id={cid} | cos={cos:.3f} | {p}:{a}-{b} ---\n{tx[:900]}")
    return "\n".join(out)


@mcp.tool()
def search_exact(pattern: str, caso: str, k: int = 30) -> str:
    """Busca LITERAL (regex) com numero de linha — use para IP, IMEI, CPF, telefone,
    hash, usuario. Para identificador, esta ferramenta e superior a semantica."""
    try:
        rx = re.compile(pattern, re.I)
    except re.error as e:
        return f"ERRO: regex invalida: {e}"
    con = db(caso, create=False)
    out, n = [], 0
    lim = max(1, min(int(k), 200))
    for cid, p, ls, tx in con.execute(
        "SELECT c.id, d.path, c.line_start, c.text FROM chunks c "
        "JOIN documents d ON d.id=c.doc_id"
    ):
        for i, linha in enumerate(tx.splitlines()):
            if rx.search(linha):
                out.append(f"{p}:{ls+i} | chunk_id={cid} | {linha.strip()[:220]}")
                n += 1
                if n >= lim:
                    break
        if n >= lim:
            break
    con.close()
    return "\n".join(out) if out else f"Sem ocorrencia literal de /{pattern}/ no caso."


@mcp.tool()
def doc_read(caso: str, path_contains: str, line_start: int = 1, line_end: int = 80) -> str:
    """Le a fonte original numa faixa de linhas — a volta a evidencia bruta.

    Use para conferir com os proprios olhos o que a busca afirmou.
    """
    con = db(caso, create=False)
    row = con.execute(
        "SELECT path, sha256 FROM documents WHERE path LIKE ? ORDER BY LENGTH(path) LIMIT 1",
        (f"%{path_contains}%",)).fetchone()
    con.close()
    if not row:
        return f"Nenhum documento do caso bate com {path_contains!r}."
    p = Path(row[0])
    if not p.exists():
        return f"ARQUIVO AUSENTE do disco: {p} (estava no caso; custodia rompida)"
    txt, _, _ = ler_documento(p, ocr=False)
    linhas = txt.splitlines()
    a, b = max(1, int(line_start)), min(len(linhas), int(line_end))
    corpo = "\n".join(f"{i:>6} | {linhas[i-1]}" for i in range(a, b + 1))
    return f"{p} (sha256 {row[1][:16]}...) linhas {a}-{b} de {len(linhas)}\n{corpo}"


@mcp.tool()
def sql(query: str, caso: str, limit: int = 50) -> str:
    """Executa SELECT (somente leitura) no banco do caso.

    Tabelas do sistema: documents, entities, mentions, relations, chunks, steps.
    CSV/TSV ingeridos viram tabelas t_<nome> — veja forense_schema.
    Exemplo: SELECT ip, COUNT(DISTINCT conta) FROM t_acessos GROUP BY ip HAVING COUNT(DISTINCT conta)>1
    """
    q = query.strip().rstrip(";")
    if not re.match(r"^(select|with)\b", q, re.I):
        return "ERRO: so SELECT/WITH e permitido (o caso e leitura; nada de escrita)."
    if re.search(r"\b(insert|update|delete|drop|alter|create|attach|pragma|replace)\b", q, re.I):
        return "ERRO: comando de escrita bloqueado."
    con = db(caso, create=False)
    try:
        con.execute("PRAGMA query_only=ON")
        cur = con.execute(q)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchmany(max(1, min(int(limit), 500)))
    except sqlite3.Error as e:
        con.close()
        return f"ERRO SQL: {e}"
    con.close()
    if not rows:
        return "Consulta valida, zero linhas."
    larg = [max(len(str(c)), *(len(str(r[i])) for r in rows)) for i, c in enumerate(cols)]
    larg = [min(w, 40) for w in larg]
    head = " | ".join(str(c)[:w].ljust(w) for c, w in zip(cols, larg))
    sep = "-+-".join("-" * w for w in larg)
    corpo = "\n".join(" | ".join(str(v)[:w].ljust(w) for v, w in zip(r, larg)) for r in rows)
    return f"{head}\n{sep}\n{corpo}\n({len(rows)} linhas)"


@mcp.tool()
def schema(caso: str) -> str:
    """Mostra as tabelas e colunas disponiveis para forense_sql neste caso."""
    con = db(caso, create=False)
    out = []
    for (nome,) in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        cols = [f"{r[1]}" for r in con.execute(f"PRAGMA table_info({nome})")]
        n = con.execute(f"SELECT COUNT(*) FROM {nome}").fetchone()[0]
        out.append(f"{nome} ({n} linhas): {', '.join(cols)}")
    con.close()
    return "\n".join(out)


@mcp.tool()
def entities(caso: str, tipo: str = "", contains: str = "", limit: int = 40) -> str:
    """Lista entidades extraidas, com quantas mencoes e em quantos documentos aparecem."""
    con = db(caso, create=False)
    q = ("SELECT e.type, e.value, COUNT(m.id), COUNT(DISTINCT m.doc_id) "
         "FROM entities e LEFT JOIN mentions m ON m.entity_id=e.id WHERE 1=1")
    par: list = []
    if tipo:
        q += " AND e.type=?"
        par.append(tipo)
    if contains:
        q += " AND e.norm LIKE ?"
        par.append(f"%{contains.lower()}%")
    q += " GROUP BY e.id ORDER BY 4 DESC, 3 DESC LIMIT ?"
    par.append(max(1, min(int(limit), 200)))
    rows = con.execute(q, par).fetchall()
    con.close()
    if not rows:
        return "Nenhuma entidade bate com o filtro."
    return "tipo | valor | mencoes | documentos\n" + "\n".join(
        f"{t} | {v} | {c} | {d}" for t, v, c, d in rows)


@mcp.tool()
def mentions(valor: str, caso: str, limit: int = 30) -> str:
    """PROVENIENCIA: toda ocorrencia de um valor, com arquivo, linha, contexto e hash.

    E a ferramenta que sustenta qualquer afirmacao. Use antes de concluir.
    """
    con = db(caso, create=False)
    n = re.sub(r"\D", "", valor) if re.search(r"\d", valor) else valor.lower()
    rows = con.execute(
        "SELECT e.type, e.value, d.path, m.line, m.context, m.method, m.confidence, "
        "d.sha256, d.source_label FROM mentions m "
        "JOIN entities e ON e.id=m.entity_id JOIN documents d ON d.id=m.doc_id "
        "WHERE e.norm=? OR e.value=? OR e.norm=? ORDER BY d.path, m.line LIMIT ?",
        (valor.lower(), valor, n, max(1, min(int(limit), 200)))).fetchall()
    con.close()
    if not rows:
        return (f"'{valor}' nao consta como entidade extraida. "
                f"Tente forense_search_exact para buscar o literal no texto.")
    out = [f"PROVENIENCIA de '{valor}' — {len(rows)} mencoes"]
    for t, v, p, ln, ctx, met, conf, sha, lab in rows:
        out.append(f"\n[{t}] {v}\n  fonte: {p}:{ln}"
                   f"{(' | origem=' + lab) if lab else ''}\n"
                   f"  metodo: {met} (confianca {conf})\n  sha256: {sha[:32]}...\n"
                   f"  contexto: {ctx}")
    return "\n".join(out)


def _grafo(con: sqlite3.Connection) -> nx.Graph:
    g = nx.Graph()
    for eid, t, v in con.execute("SELECT id, type, value FROM entities"):
        g.add_node(eid, type=t, value=v)
    for a, b, doc, ln, kind in con.execute(
        "SELECT a_id,b_id,doc_id,line,kind FROM relations"):
        if g.has_edge(a, b):
            g[a][b]["peso"] += 1
        else:
            g.add_edge(a, b, peso=1, doc=doc, line=ln, kind=kind)
    return g


def _achar_id(con: sqlite3.Connection, valor: str) -> tuple[int, str, str] | None:
    n = re.sub(r"\D", "", valor) if re.search(r"\d", valor) else valor.lower()
    r = con.execute(
        "SELECT id, type, value FROM entities WHERE norm=? OR value=? OR norm=? LIMIT 1",
        (valor.lower(), valor, n)).fetchone()
    return r


@mcp.tool()
def connections(valor: str, caso: str, profundidade: int = 2, limit: int = 40) -> str:
    """Vizinhanca de uma entidade no grafo de vinculos, ate N saltos.

    Aresta = duas entidades na MESMA linha/registro de um documento. Cada aresta
    tem documento e linha, entao o vinculo e sempre rastreavel ate a fonte.
    """
    con = db(caso, create=False)
    alvo = _achar_id(con, valor)
    if not alvo:
        con.close()
        return f"'{valor}' nao esta no grafo do caso."
    eid, tipo, v = alvo
    g = _grafo(con)
    if eid not in g:
        con.close()
        return f"'{v}' sem vinculos registrados."
    prof = max(1, min(int(profundidade), 4))
    dist = nx.single_source_shortest_path_length(g, eid, cutoff=prof)
    docs = {r[0]: r[1] for r in con.execute("SELECT id, path FROM documents")}
    itens = sorted(dist.items(), key=lambda kv: (kv[1], -g.degree(kv[0])))
    out = [f"Vinculos de [{tipo}] {v} — ate {prof} salto(s), {len(dist)-1} entidades"]
    for nid, d in itens[: max(1, min(int(limit), 200))]:
        if nid == eid:
            continue
        nd = g.nodes[nid]
        ev = ""
        if d == 1:
            e = g[eid][nid]
            ev = f"  <- {docs.get(e['doc'],'?')}:{e['line']} ({e['kind']}, {e['peso']}x)"
        out.append(f"  {d} salto | [{nd['type']}] {nd['value']}{ev}")
    con.close()
    return "\n".join(out)


@mcp.tool()
def path(origem: str, destino: str, caso: str) -> str:
    """Menor caminho entre duas entidades no grafo — mostra COMO uma liga na outra,
    com o documento e a linha de cada elo. Use para 'existe relacao entre X e Y?'."""
    con = db(caso, create=False)
    a, b = _achar_id(con, origem), _achar_id(con, destino)
    if not a or not b:
        con.close()
        falta = origem if not a else destino
        return f"'{falta}' nao esta no grafo do caso."
    g = _grafo(con)
    docs = {r[0]: r[1] for r in con.execute("SELECT id, path FROM documents")}
    try:
        cam = nx.shortest_path(g, a[0], b[0])
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        con.close()
        return (f"NENHUM caminho entre [{a[1]}] {a[2]} e [{b[1]}] {b[2]} nos vinculos "
                f"registrados. Ausencia de caminho NAO prova ausencia de relacao — "
                f"prova apenas que este acervo nao a registra.")
    out = [f"Caminho de [{a[1]}] {a[2]} ate [{b[1]}] {b[2]} — {len(cam)-1} elo(s):"]
    for i in range(len(cam) - 1):
        x, y = cam[i], cam[i + 1]
        e = g[x][y]
        out.append(f"  [{g.nodes[x]['type']}] {g.nodes[x]['value']}"
                   f"\n      --({e['kind']}, {e['peso']}x)-- {docs.get(e['doc'],'?')}:{e['line']}"
                   f"\n  [{g.nodes[y]['type']}] {g.nodes[y]['value']}")
    con.close()
    return "\n".join(out)


@mcp.tool()
def shared(caso: str, tipo: str = "ipv4", min_entidades: int = 2, limit: int = 30) -> str:
    """Pontos de convergencia: valores de um tipo que ligam MAIS DE UMA entidade.

    E o achado classico da investigacao — o mesmo IP/dispositivo/telefone
    aparecendo em contas diferentes. Ordena pelo que mais concentra vinculos.
    """
    con = db(caso, create=False)
    g = _grafo(con)
    docs = {r[0]: r[1] for r in con.execute("SELECT id, path FROM documents")}
    alvos = [(i, d["value"]) for i, d in g.nodes(data=True) if d.get("type") == tipo]
    linhas = []
    for eid, val in alvos:
        viz = list(g.neighbors(eid))
        contas = [v for v in viz if g.nodes[v]["type"] in
                  ("usuario", "email", "telefone", "cpf", "imei", "mac")]
        if len(contas) >= max(2, int(min_entidades)):
            # Cada vinculo sai com arquivo:LINHA, nao so com o nome do arquivo.
            # Medido em 24/08/2026: entregando apenas o caminho, o modelo local
            # completou o numero da linha por conta propria e enxertou a linha de
            # um arquivo no nome de outro. Citacao inventada tem a mesma aparencia
            # da verdadeira — a unica defesa e a ferramenta ja entregar a linha.
            det = []
            for v in contas[:8]:
                e = g[eid][v]
                det.append(f"\n       [{g.nodes[v]['type']}] {g.nodes[v]['value']}"
                           f"  <- {docs.get(e['doc'], '?')}:{e['line']}")
            linhas.append((len(contas),
                           f"[{tipo}] {val} liga {len(contas)} entidade(s):"
                           + "".join(det)))
    con.close()
    if not linhas:
        return (f"Nenhum valor do tipo '{tipo}' liga {min_entidades}+ entidades neste acervo.")
    linhas.sort(reverse=True)
    return (f"{len(linhas)} ponto(s) de convergencia do tipo '{tipo}':\n"
            + "\n".join("  " + t for _, t in linhas[: max(1, min(int(limit), 100))]))


@mcp.tool()
def timeline(caso: str, valor: str = "", limit: int = 40) -> str:
    """Linha do tempo: carimbos de data/hora extraidos, opcionalmente so das linhas
    onde um valor aparece. Util para ordenar acessos e cruzar janelas temporais."""
    con = db(caso, create=False)
    if valor:
        alvo = _achar_id(con, valor)
        if not alvo:
            con.close()
            return f"'{valor}' nao consta no caso."
        rows = con.execute(
            "SELECT e2.value, d.path, m2.line, m2.context FROM mentions m1 "
            "JOIN mentions m2 ON m2.doc_id=m1.doc_id AND m2.line=m1.line "
            "JOIN entities e2 ON e2.id=m2.entity_id JOIN documents d ON d.id=m1.doc_id "
            "WHERE m1.entity_id=? AND e2.type='timestamp' ORDER BY e2.value LIMIT ?",
            (alvo[0], max(1, min(int(limit), 200)))).fetchall()
    else:
        rows = con.execute(
            "SELECT e.value, d.path, m.line, m.context FROM mentions m "
            "JOIN entities e ON e.id=m.entity_id JOIN documents d ON d.id=m.doc_id "
            "WHERE e.type='timestamp' ORDER BY e.value LIMIT ?",
            (max(1, min(int(limit), 200)),)).fetchall()
    con.close()
    if not rows:
        return "Nenhum carimbo de data/hora extraido para esse filtro."
    cab = f"Linha do tempo{(' de ' + valor) if valor else ''} — {len(rows)} registros"
    return cab + "\n" + "\n".join(
        f"  {ts} | {Path(p).name}:{ln} | {ctx[:120]}" for ts, p, ln, ctx in rows)


@mcp.tool()
def custodia(caso: str) -> str:
    """Verifica a cadeia de custodia: re-hasheia cada arquivo e compara com o hash
    da ingestao. Acusa arquivo alterado, movido ou ausente. Emite SHA-512."""
    con = db(caso, create=False)
    rows = con.execute(
        "SELECT path, sha256, sha512, ingested_at, source_label FROM documents ORDER BY path"
    ).fetchall()
    out, ok, alt, sumiu = [], 0, 0, 0
    for p, s256, s512, ts, lab in rows:
        f = Path(p)
        if not f.exists():
            out.append(f"  AUSENTE   {p}")
            sumiu += 1
            continue
        atual = hashlib.sha256(f.read_bytes()).hexdigest()
        if atual == s256:
            out.append(f"  INTEGRO   {p}\n            sha512={s512}\n"
                       f"            ingerido={ts}{(' origem=' + lab) if lab else ''}")
            ok += 1
        else:
            out.append(f"  ALTERADO  {p}\n            era  {s256}\n            agora {atual}")
            alt += 1
    con.close()
    cab = (f"CADEIA DE CUSTODIA — caso '{caso}': {ok} integros, {alt} ALTERADOS, "
           f"{sumiu} AUSENTES\nVerificado em {_now()}")
    return cab + "\n" + "\n".join(out)


@mcp.tool()
def registrar(caso: str, acao: str, nota: str) -> str:
    """Registra um passo da investigacao (o que foi feito e por que).

    Chame ao concluir cada etapa relevante: o historico e o que torna a
    investigacao reproduzivel por outra pessoa.
    """
    con = db(caso)
    _log(con, acao, nota)
    con.commit()
    n = con.execute("SELECT COUNT(*) FROM steps").fetchone()[0]
    con.close()
    return f"Passo {n} registrado no caso '{caso}': {acao}"


@mcp.tool()
def historico(caso: str, limit: int = 50) -> str:
    """Mostra os passos ja executados na investigacao, em ordem."""
    con = db(caso, create=False)
    rows = con.execute("SELECT id, ts, acao, nota FROM steps ORDER BY id LIMIT ?",
                       (max(1, min(int(limit), 300)),)).fetchall()
    con.close()
    if not rows:
        return "Nenhum passo registrado ainda."
    return "\n".join(f"#{i:03d} {ts} | {a} | {n}" for i, ts, a, n in rows)


# ------------------------------------- ponte para a biblioteca forense do usuario
# Estas ferramentas NAO reimplementam nada: chamam os mesmos scripts que o usuario
# ja roda a mao, no mesmo Python do sistema (que tem as dependencias deles).

def _py_sistema() -> str:
    """Python DO SISTEMA — nunca o do uv.

    Este servidor roda num ambiente isolado do uv, que nao tem pandas/openpyxl.
    As CLIs da biblioteca forense precisam do interpretador onde o usuario as
    instalou; pegar o do uv faz a CLI falhar por dependencia ausente.
    """
    env = os.environ.get("ACERVO_PYTHON")
    if env and Path(env).exists():
        return env
    # Candidatos das duas maquinas. No macOS a pilha forense vive num venv
    # dedicado (~/ACERVO/.venv) porque o Python do Homebrew e externally managed
    # e recusa pip install global — e e nesse venv que estao pandas e openpyxl.
    for cand in (r"C:\Python314\python.exe", r"C:\Python312\python.exe",
                 str(Path.home() / "AppData/Local/Programs/Python/Python312/python.exe"),
                 str(Path.home() / "ACERVO" / ".venv" / "bin" / "python"),
                 "/opt/homebrew/bin/python3"):
        if Path(cand).exists():
            return cand
    achado = shutil.which("python3") or shutil.which("python")
    if achado and "uv" not in achado.lower() and "builds-v0" not in achado.lower():
        return achado
    return shutil.which("py") or "python"


def _rodar(args: list[str], timeout: int = 1800) -> str:
    try:
        r = subprocess.run(args, capture_output=True, timeout=timeout,
                           env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    except FileNotFoundError:
        return f"ERRO: executavel nao encontrado: {args[0]}"
    except subprocess.TimeoutExpired:
        return f"ERRO: excedeu {timeout}s: {' '.join(args[:3])}"
    saida = (r.stdout + r.stderr).decode("utf-8", "replace")
    return f"[codigo de saida {r.returncode}]\n{saida[-6000:]}"


@mcp.tool()
def validar_lote(alvo: str, chave: str = "", ddds: str = "", recursivo: bool = False) -> str:
    """PORTA DE VALIDACAO — obrigatoria antes de qualquer peca sobre um lote novo.

    Roda o validar_lote.py da biblioteca forense: DV de CPF/CNPJ, DDD, nono digito,
    IMEI/Luhn, CGNAT, mojibake, duplicata. Gera VALIDACAO_<lote>.md e .csv ao lado do
    insumo, com SHA512. Codigo de saida 2 = erro bloqueante: nao emita peca.

    alvo:      arquivo .csv/.xlsx ou pasta do lote
    chave:     colunas-chave, ex.: "CPF,Telefone"
    ddds:      DDDs esperados, ex.: "31,33"
    recursivo: varre subpastas
    """
    cli = FERRAMENTAS / "validar_lote.py"
    if not cli.exists():
        return f"ERRO: {cli} nao encontrado."
    args = [_py_sistema(), str(cli), alvo]
    if recursivo:
        args.append("-r")
    if chave:
        args += ["--chave", chave]
    if ddds:
        args += ["--ddds", ddds]
    return _rodar(args)


@mcp.tool()
def resumo_alvo(raiz_caso: str, identificador: str, detalhe: bool = False) -> str:
    """Consulta o INDICE do acervo por um identificador — responde em decimos de segundo.

    Use ANTES de varrer pastas: a varredura ad-hoc custa minutos e reimplementa a
    normalizacao, que e onde ela erra em silencio. Exige indice ja construido
    (forense_indexar_acervo).

    raiz_caso:     raiz do caso, ex.: C:/Casos/operacao-x
    identificador: telefone, CPF, IMEI, e-mail, IP...
    detalhe:       True lista ocorrencia por ocorrencia
    """
    cli = FERRAMENTAS / "resumo_alvo.py"
    if not cli.exists():
        return f"ERRO: {cli} nao encontrado."
    args = [_py_sistema(), str(cli), raiz_caso, identificador]
    if detalhe:
        args.append("--detalhe")
    return _rodar(args, timeout=600)


@mcp.tool()
def indexar_acervo(raiz_caso: str, incremental: bool = True, amostra: int = 0) -> str:
    """Constroi/atualiza o indice de identificadores do acervo (passe unico).

    Rode apos cada lote novo. amostra=3 confere o leitor em segundos ANTES de gastar
    o passe completo — faca isso quando a fonte for de layout novo.
    """
    cli = FERRAMENTAS / "indexar_identificadores.py"
    if not cli.exists():
        return f"ERRO: {cli} nao encontrado."
    args = [_py_sistema(), str(cli), raiz_caso]
    if amostra:
        args += ["--amostra", str(int(amostra))]
    elif incremental:
        args.append("--incremental")
    return _rodar(args, timeout=3600)


@mcp.tool()
def geocodificar(logradouro: str, numero: str, municipio: str, uf: str, cep: str = "") -> str:
    """Endereco -> coordenada, OFFLINE, pelo CNEFE/IBGE. Nunca chuta.

    Consultar servico online publicaria o endereco investigado em servidor de
    terceiro — por isso e offline por decisao. Sem correspondencia, devolve
    NAO_GEOCODIFICADO em vez de estimar. O grau (ENDERECO_EXATO, ENDERECO_VIZINHO,
    LOGRADOURO, CEP) precisa constar na peca.
    """
    try:
        import geocodificar_cnefe as gc
    except Exception as e:
        return f"ERRO: geocodificar_cnefe indisponivel ({e}). Confira {FERRAMENTAS}."
    try:
        r = gc.geocodificar(logradouro, numero, municipio, uf, cep)
    except Exception as e:
        return f"ERRO na geocodificacao: {e}"
    if r.get("lat") is None:
        return (f"NAO_GEOCODIFICADO — {logradouro}, {numero}, {municipio}/{uf}"
                f"\nmotivo: {r.get('detalhe','')}\n"
                f"Municipio sem CSV do CNEFE? Baixe com baixar_cnefe.py '{municipio}/{uf}'.")
    return (f"lat={r['lat']} lon={r['lon']}\ngrau={r['grau']}\ndetalhe={r.get('detalhe','')}")


@mcp.tool()
def biblioteca_status() -> str:
    """Diz se os validadores em uso sao os da biblioteca forense ou o fallback local.

    Importa porque validador divergente entre agente e pipeline produz achados que
    nao se reproduzem — confira quando um resultado do agente destoar do pipeline.
    """
    linhas = [f"Biblioteca forense: {'ATIVA' if LIB['disponivel'] else 'INDISPONIVEL'}",
              f"  {LIB['motivo']}",
              f"  caminho: {FERRAMENTAS} ({'existe' if FERRAMENTAS.is_dir() else 'AUSENTE'})",
              f"  python do sistema: {_py_sistema()}"]
    for nome in ("validar_lote.py", "resumo_alvo.py", "indexar_identificadores.py",
                 "geocodificar_cnefe.py"):
        linhas.append(f"  {nome}: {'ok' if (FERRAMENTAS / nome).exists() else 'AUSENTE'}")
    if not LIB["disponivel"]:
        linhas.append("\nATENCAO: extracao usando validadores locais de fallback. "
                      "Eles conferem DV de CPF/CNPJ e Luhn de IMEI, mas NAO cobrem "
                      "nono digito, CGNAT nem mojibake. Rode forense_validar_lote "
                      "antes de emitir peca.")
    return "\n".join(linhas)


# ------------------------------------------------- auditoria de citacoes

# Entre o nome do arquivo e o numero da linha pode haver fechamento de crase,
# aspas ou parenteses — o modelo formata em markdown e escreve `arquivo.csv`:68.
# A primeira versao desta regex exigia os dois-pontos colados a extensao e por
# isso NAO viu as citacoes da resposta fabricada, que era o caso que ela existia
# para pegar. Auditor que nao pega o caso conhecido e pior que nenhum: ele
# devolve "nada a apontar" e isso e lido como aprovacao.
_RX_CITACAO = re.compile(
    r"([A-Za-z0-9_][A-Za-z0-9 _.\\/():-]*\.[A-Za-z0-9]{2,5})[`'\"\)\]]*\s*:\s*(\d{1,7})")


def _docs_do_caso(con: sqlite3.Connection) -> dict:
    return {r[0]: r[1] for r in con.execute("SELECT id, path FROM documents")}


def _casa_documento(citado: str, docs: dict) -> tuple[int, str] | None:
    """Casa o arquivo citado com um documento do caso, por sufixo de caminho.

    O agente costuma citar so o nome do arquivo; o banco guarda o caminho
    inteiro. Casar por sufixo aceita as duas formas sem aceitar nome parecido
    de outra pasta.
    """
    alvo = citado.replace("\\", "/").strip().lower()
    candidatos = []
    for did, caminho in docs.items():
        c = caminho.replace("\\", "/").lower()
        if c == alvo or c.endswith("/" + alvo):
            candidatos.append((did, caminho))
    if len(candidatos) == 1:
        return candidatos[0]
    if len(candidatos) > 1:
        return None  # ambiguo: mesmo nome em pastas diferentes
    return None


@mcp.tool()
def auditar_citacoes(texto: str, caso: str) -> str:
    """Confere TODA citacao arquivo:linha de um texto contra o banco do caso.

    Para cada citacao responde se ela existe de fato e QUAIS entidades estao
    naquela linha — de modo que citacao certa em linha errada tambem apareca.

    Rode SEMPRE antes de entregar uma analise. Citacao inventada tem a mesma
    aparencia da verdadeira: nenhuma leitura humana distingue as duas, so a
    conferencia contra o banco distingue.

    texto: a resposta/relatorio a auditar (cole o texto inteiro)
    caso:  o caso contra o qual conferir
    """
    con = db(caso, create=False)
    docs = _docs_do_caso(con)

    achadas = []
    vistas = set()
    for m in _RX_CITACAO.finditer(texto or ""):
        arq, linha = m.group(1).strip(" .,;|`\"'"), int(m.group(2))
        chave = (arq.lower(), linha)
        if chave in vistas:
            continue
        vistas.add(chave)
        achadas.append((arq, linha))

    if not achadas:
        con.close()
        return ("Nenhuma citacao no formato arquivo:linha foi encontrada no texto.\n"
                "Se o texto faz afirmacao factual sobre o acervo, isso ja e um defeito: "
                "afirmacao sem proveniencia nao se confere.")

    ok, erro = [], []
    # A mesma linha citada com caminho completo e com nome curto e UMA citacao,
    # nao duas. Sem deduplicar depois de resolver o documento, o relatorio infla
    # a contagem — e contagem inflada de "verificadas" da falsa sensacao de
    # cobertura, que e justamente o que este auditor existe para nao fazer.
    ja_resolvidas = set()
    for arq, linha in achadas:
        casado = _casa_documento(arq, docs)
        if casado is not None and (casado[0], linha) in ja_resolvidas:
            continue
        if casado is not None:
            ja_resolvidas.add((casado[0], linha))
        if casado is None:
            erro.append(f"  [FORA DO CASO]  {arq}:{linha}\n"
                        f"      nenhum documento do caso '{caso}' corresponde a esse nome "
                        f"(ou ha mais de um com o mesmo nome).")
            continue
        did, caminho = casado
        linhas_doc = con.execute("SELECT lines FROM documents WHERE id=?", (did,)).fetchone()[0]
        if linha > linhas_doc:
            erro.append(f"  [LINHA INEXISTENTE]  {arq}:{linha}\n"
                        f"      o documento tem {linhas_doc} linhas.")
            continue
        ents = con.execute(
            "SELECT e.type, e.value FROM mentions m JOIN entities e ON e.id=m.entity_id "
            "WHERE m.doc_id=? AND m.line=? ORDER BY e.type", (did, linha)).fetchall()
        if not ents:
            erro.append(f"  [SEM MENCAO]  {arq}:{linha}\n"
                        f"      a linha existe, mas nenhuma entidade foi extraida dela. "
                        f"Citacao provavelmente enxertada de outro arquivo.")
            continue
        resumo = ", ".join(f"[{t}] {v}" for t, v in ents[:6])
        if len(ents) > 6:
            resumo += f" (+{len(ents)-6})"
        ok.append(f"  [VERIFICADA]  {arq}:{linha}\n      contem: {resumo}")

    con.close()
    cab = (f"AUDITORIA DE CITACOES — caso '{caso}'\n"
           f"{len(ok) + len(erro)} citacao(oes) distinta(s) | {len(ok)} verificada(s) | {len(erro)} com problema")
    corpo = "\n".join(erro + ok)
    if erro:
        corpo += ("\n\nHA CITACAO QUE NAO SE SUSTENTA. Nao entregue o texto assim: "
                  "corrija a citacao a partir de forense_mentions, ou remova a afirmacao. "
                  "Se a proveniencia nao existe, a secao de evidencia recebe "
                  "'Sem proveniencia confirmada'.")
    else:
        corpo += ("\n\nTodas as citacoes existem e a linha citada contem entidade extraida. "
                  "Isso NAO garante que a entidade citada seja a que o texto afirma — "
                  "confira acima se o valor bate com o que foi escrito.")
    return cab + "\n" + corpo


if __name__ == "__main__":
    import sys as _sys
    if "--auditar" in _sys.argv:
        i = _sys.argv.index("--auditar")
        try:
            _arq, _caso = _sys.argv[i + 1], _sys.argv[i + 2]
        except IndexError:
            print("uso: forense_server.py --auditar <arquivo-de-texto> <caso>")
            raise SystemExit(2)
        # "-" le da entrada padrao: permite auditar a saida de outro processo
        # sem arquivo temporario, que e como o investigar.py encadeia.
        if _arq == "-":
            _txt = _sys.stdin.buffer.read().decode("utf-8", "replace")
        else:
            _txt = Path(_arq).read_text(encoding="utf-8", errors="replace")
        _r = auditar_citacoes(_txt, _caso)
        print(_r)
        raise SystemExit(2 if "com problema" in _r and "0 com problema" not in _r else 0)
    mcp.run()
