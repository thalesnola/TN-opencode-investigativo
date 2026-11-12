#!/usr/bin/env python3
"""
Roda o agente investigador e audita a resposta dele — sem depender da boa vontade
do modelo.

Por que existe: o prompt manda auditar as citacoes como passo final obrigatorio.
Medido em 24/08/2026: o agente **nao chamou** a ferramenta de auditoria e mesmo
assim escreveu na resposta "forense_auditar_citacoes confirmou todas as
referencias como validas". As citacoes por acaso estavam certas, mas a afirmacao
de ter conferido era falsa.

Instrucao obrigatoria dentro do prompt produz **alegacao de cumprimento**, nao
cumprimento. A conferencia so vale se acontecer fora do modelo — e este script e
esse fora. Mesma logica dos hooks de git do vault: a regra que depende de alguem
lembrar nao e uma regra, e um pedido.

    python investigar.py <caso> "<pergunta>"
    python investigar.py <caso> "<pergunta>" --modelo ollama/qwen3-coder:30b

Sai 0 quando a auditoria passa, 2 quando ha citacao que nao se sustenta, e 3
quando o agente ALEGA ter auditado sem ter chamado a ferramenta.
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

AQUI = Path(__file__).resolve().parent
SERVIDOR = AQUI / "rag" / "forense_server.py"
ANSI = re.compile(r"\x1b\[[0-9;]*m")
# linha de chamada de ferramenta no transcript do opencode
CHAMADA = re.compile(r"^\s*⚙\s*(\S+)", re.M)


def _saida_utf8() -> None:
    """O console do Windows e cp1252 e derruba o processo no primeiro caractere
    fora da tabela (medido: uma seta vinda da saida do agente matou este script
    com UnicodeEncodeError, depois de o agente ja ter rodado 6 minutos).

    Reconfigurar aqui, e nao confiar em variavel de ambiente, porque quem chama
    este script pode ser qualquer coisa — terminal, tarefa agendada, outro script.
    """
    for fluxo in (sys.stdout, sys.stderr):
        try:
            fluxo.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main() -> int:
    _saida_utf8()
    ap = argparse.ArgumentParser(description="Agente investigador com auditoria imposta.")
    ap.add_argument("caso")
    ap.add_argument("pergunta")
    ap.add_argument("--modelo", default=None, help="sobrescreve o modelo do agente")
    ap.add_argument("--agente", default="investigador")
    ap.add_argument("--salvar", default=None, help="grava a resposta neste arquivo")
    ap.add_argument("--somente-auditar", metavar="ARQUIVO", default=None,
                    help="audita um transcript ja salvo, sem rodar o agente")
    args = ap.parse_args()

    if args.somente_auditar:
        bruto = ANSI.sub("", Path(args.somente_auditar).read_text(
            encoding="utf-8", errors="replace"))
        return _auditar(bruto, args.caso)

    exe = shutil.which("opencode")
    if not exe:
        print("[ERRO] opencode nao esta no PATH.")
        return 1
    if not SERVIDOR.exists():
        print(f"[ERRO] {SERVIDOR} nao encontrado.")
        return 1

    cmd = [exe, "run", "--agent", args.agente]
    if args.modelo:
        cmd += ["--model", args.modelo]
    cmd.append(f"No caso '{args.caso}': {args.pergunta}")

    print(f"[..] rodando o agente '{args.agente}' sobre o caso '{args.caso}'...\n")
    r = subprocess.run(cmd, capture_output=True)
    bruto = ANSI.sub("", (r.stdout + r.stderr).decode("utf-8", "replace"))

    # Gravar ANTES de imprimir: uma execucao custa minutos e ja se perdeu uma
    # inteira porque o print morreu no console cp1252 antes de qualquer registro.
    if args.salvar:
        Path(args.salvar).write_text(bruto, encoding="utf-8")
    print(bruto)
    if args.salvar:
        print(f"[ok] resposta gravada em {args.salvar}")

    return _auditar(bruto, args.caso)


def _auditar(bruto: str, caso: str) -> int:
    chamadas = set(CHAMADA.findall(bruto))
    alegou = "auditar_citacoes" in bruto
    chamou = any("auditar_citacoes" in c for c in chamadas)

    print("\n" + "=" * 72)
    print("AUDITORIA IMPOSTA (fora do modelo)")
    print("=" * 72)

    # A auditoria roda SEMPRE, tenha o agente chamado ou nao.
    aud = subprocess.run(
        [shutil.which("uv") or "uv", "run", "--script", str(SERVIDOR),
         "--auditar", "-", caso],
        input=bruto.encode("utf-8"), capture_output=True)
    saida = (aud.stdout + aud.stderr).decode("utf-8", "replace")
    print(saida.strip())

    codigo = 2 if aud.returncode == 2 else 0

    if alegou and not chamou:
        print("\n" + "!" * 72)
        print("O AGENTE ALEGOU TER AUDITADO SEM CHAMAR A FERRAMENTA.")
        print("A frase sobre a auditoria na resposta dele e infundada — apague-a")
        print("antes de reaproveitar o texto. Vale o resultado acima, nao o dele.")
        print("!" * 72)
        codigo = max(codigo, 3)

    if codigo == 0:
        print("\n[ok] citacoes conferidas contra o banco. Confira ainda, a mao:")
        print("     contagem de fontes, secao LACUNAS e grau de sustentacao.")
    return codigo


if __name__ == "__main__":
    sys.exit(main())
