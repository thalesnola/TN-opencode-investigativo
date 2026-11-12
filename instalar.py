#!/usr/bin/env python3
"""
Instalador por maquina do agente investigativo local.

Gera o `opencode.json` a partir do `opencode.json.template`, resolvendo o que muda
de maquina para maquina: caminho absoluto dos servidores MCP, modelo padrao e a
localizacao da biblioteca forense.

Por que existe em vez de simplesmente versionar o `opencode.json`: o arquivo guarda
**caminho absoluto** no comando dos servidores MCP. Versiona-lo faria a maquina macOS
procurar `C:~ e falhar - a mesma classe de erro ja registrada na vault para
os manifestos de plugin do Claude Code. O template e a fonte da verdade; o
`opencode.json` e artefato local e nao entra no repositorio.

Uso:
    python instalar.py                        # detecta tudo
    python instalar.py --modelo qwen3-coder:30b
    python instalar.py --conferir             # so diagnostica, nao escreve
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

AQUI = Path(__file__).resolve().parent
TEMPLATE = AQUI / "opencode.json.template"
DESTINO = AQUI / "opencode.json"
OLLAMA = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")

# Modelos que a configuracao pressupoe.
MODELO_GRANDE = "qwen3-coder:30b"
MODELO_MEDIO = "qwen3:8b"
# Hibrido Mamba-2 + MoE (3 B ativos): transborda para a CPU (81%) e mesmo assim sustenta
# ~30 tok/s ate 31k de contexto, onde o qwen3:8b cai a 4,7 (medido em 12/09/2026, GPU de
# 8 GB). Precisa de ~25 GB de RAM livres. Carga fria 33 s contra 7 s do 8b.
MODELO_CONTEXTO_LONGO = "nemotron-3.5-lightning:30b-a3b"
MODELO_PEQUENO = "qwen2.5-coder:7b"
EMBEDDING = "nomic-embed-text"


def ok(msg: str) -> None:
    print(f"  [ok]    {msg}")


def aviso(msg: str) -> None:
    print(f"  [aviso] {msg}")


def erro(msg: str) -> None:
    print(f"  [ERRO]  {msg}")


# --------------------------------------------------------------------- deteccao

def memoria_gb() -> float:
    """RAM total. No Apple Silicon isso e memoria unificada, que a GPU usa."""
    try:
        if platform.system() == "Darwin":
            saida = subprocess.run(["sysctl", "-n", "hw.memsize"],
                                   capture_output=True, timeout=10)
            return int(saida.stdout.strip()) / 1e9
        if platform.system() == "Windows":
            saida = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory"],
                capture_output=True, timeout=30)
            return int(saida.stdout.strip()) / 1e9
        return int(subprocess.run(["free", "-b"], capture_output=True, timeout=10)
                   .stdout.split()[7]) / 1e9
    except Exception:
        return 0.0


def vram_gb() -> float:
    """VRAM dedicada (NVIDIA). 0 quando nao ha GPU discreta detectavel."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return 0.0
    try:
        r = subprocess.run([exe, "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                           capture_output=True, timeout=30)
        return max(int(x) for x in r.stdout.decode().split()) / 1024
    except Exception:
        return 0.0


def escolher_modelo() -> tuple[str, str]:
    """Devolve (modelo, motivo).

    A regra vem de medicao, nao de tamanho: modelo DENSO que transborda para a CPU torna
    o laco agentico inutilizavel (medido: 71% CPU -> ~56 s por resposta trivial,
    contra ~10 s do modelo que cabe inteiro na GPU). A excecao medida em 12/09/2026 e o
    MoE hibrido (MODELO_CONTEXTO_LONGO): 3 B ativos e cache de estado Mamba nao crescem com
    o contexto, entao ele transborda e ainda assim vence o 8b denso a partir de ~5k tokens.
    """
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        mem = memoria_gb()
        if mem >= 32:
            return MODELO_GRANDE, f"Apple Silicon com {mem:.0f} GB unificados - o 30b roda em GPU"
        return MODELO_MEDIO, f"Apple Silicon com {mem:.0f} GB - o 30b nao caberia com folga"
    v = vram_gb()
    if v >= 24:
        return MODELO_GRANDE, f"{v:.0f} GB de VRAM comportam o 30b"
    if v and memoria_gb() >= 48:
        return MODELO_CONTEXTO_LONGO, (f"{v:.0f} GB de VRAM - o 30b denso transbordaria; "
                                       "o MoE hibrido sustenta 30 tok/s ate 31k de contexto")
    if v:
        return MODELO_MEDIO, f"{v:.0f} GB de VRAM e pouca RAM para o MoE de 25 GB"
    return MODELO_MEDIO, "sem GPU discreta detectada - escolha conservadora"


def achar_ferramentas() -> str:
    for c in (os.environ.get("ACERVO_FERRAMENTAS"),
              r"C:\ACERVO\Ferramentas",
              str(Path.home() / "ACERVO" / "Ferramentas")):
        if c and Path(c).is_dir():
            return Path(c).as_posix()
    return ""


def achar_python(ferramentas: str) -> str:
    """Interpretador que tem as dependencias das CLIs da biblioteca forense.

    NUNCA o do `uv`: o servidor MCP roda num ambiente isolado que nao tem
    pandas/openpyxl, e chamar a CLI com ele falha por dependencia ausente.
    """
    cands = [os.environ.get("ACERVO_PYTHON")]
    if platform.system() == "Darwin":
        cands += [str(Path.home() / "ACERVO" / ".venv" / "bin" / "python"),
                  "/opt/homebrew/bin/python3"]
    else:
        cands += [r"C:\Python314\python.exe", r"C:\Python312\python.exe",
                  str(Path.home() / "AppData/Local/Programs/Python/Python312/python.exe")]
    for c in cands:
        if c and Path(c).exists():
            return Path(c).as_posix()
    achado = shutil.which("python3") or shutil.which("python")
    if achado and "uv" not in achado.lower():
        return Path(achado).as_posix()
    return ""


def modelos_ollama() -> list[str] | None:
    try:
        with urllib.request.urlopen(f"{OLLAMA}/api/tags", timeout=15) as r:
            return [m["name"] for m in json.loads(r.read()).get("models", [])]
    except (urllib.error.URLError, OSError, ValueError):
        return None


# ----------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description="Instala a configuracao nesta maquina.")
    ap.add_argument("--modelo", help=f"forca o modelo padrao (ex.: {MODELO_GRANDE})")
    ap.add_argument("--conferir", action="store_true", help="so diagnostica, nao escreve")
    args = ap.parse_args()

    print(f"\nAgente investigativo local - instalacao em {platform.system()} "
          f"({platform.machine()})")
    print(f"Configuracao: {AQUI}\n")

    if not TEMPLATE.exists():
        erro(f"{TEMPLATE.name} nao encontrado - repositorio incompleto.")
        return 1

    print("Pre-requisitos:")
    falta_grave = False
    caminhos = {}
    for exe, nota in (("uv", "roda os servidores MCP"),
                      ("opencode", "o proprio CLI"),
                      ("ollama", "modelos locais")):
        caminho = shutil.which(exe)
        if caminho:
            caminhos[exe] = caminho
            ok(f"{exe} - {caminho}")
        else:
            erro(f"{exe} AUSENTE ({nota})")
            falta_grave = True

    if args.modelo:
        modelo, motivo = args.modelo, "escolhido na linha de comando"
    else:
        modelo, motivo = escolher_modelo()

    print("\nModelos:")
    disponiveis = modelos_ollama()
    if disponiveis is None:
        aviso(f"Ollama nao respondeu em {OLLAMA} - nao deu para conferir os modelos.")
    else:
        base = {m.split(":")[0]: m for m in disponiveis}
        for m in (modelo, MODELO_PEQUENO, EMBEDDING):
            if m in disponiveis or m.split(":")[0] in base:
                ok(m)
            else:
                aviso(f"{m} ausente - rode: ollama pull {m}")
    ok(f"modelo padrao: {modelo} ({motivo})")

    print("\nBiblioteca forense:")
    ferramentas = achar_ferramentas()
    if ferramentas:
        ok(f"encontrada em {ferramentas}")
    else:
        aviso("nao encontrada - o agente cai nos validadores locais de fallback, "
              "que nao cobrem nono digito, CGNAT nem mojibake.")
    py = achar_python(ferramentas)
    if py:
        ok(f"python do sistema: {py}")
    else:
        aviso("nenhum Python do sistema identificado - as CLIs da biblioteca podem falhar.")

    texto = TEMPLATE.read_text(encoding="utf-8")
    texto = (texto.replace("__CONFIG_DIR__", AQUI.as_posix())
                  .replace("__MODEL_PEQUENO__", f"ollama/{MODELO_PEQUENO}")
                  .replace("__MODEL__", f"ollama/{modelo}")
                  .replace("__FERRAMENTAS__", ferramentas)
                  .replace("__PYTHON_SISTEMA__", Path(py).as_posix() if py else "")
                  .replace("__UV__", Path(caminhos.get("uv", "uv")).as_posix()))
    try:
        json.loads(texto)
    except json.JSONDecodeError as e:
        erro(f"o resultado nao e JSON valido: {e}")
        return 1

    print()
    if args.conferir:
        print("--conferir: nada foi escrito.")
        return 0 if not falta_grave else 1

    if DESTINO.exists():
        backup = DESTINO.with_suffix(".json.anterior")
        shutil.copy2(DESTINO, backup)
        ok(f"configuracao anterior salva em {backup.name}")
    DESTINO.write_text(texto, encoding="utf-8")
    ok(f"{DESTINO.name} gerado")

    print("\nPronto. Teste com:")
    print("    opencode run --agent investigador \"rode forense_biblioteca_status\"\n")
    if falta_grave:
        aviso("ha pre-requisito ausente acima - resolva antes de usar.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
