#!/usr/bin/env python3
"""
Deriva o `opencode.json.template` a partir do `opencode.json` desta maquina.

Por que existe: o template e a fonte da verdade do repositorio, mas quem se edita
no dia a dia e o `opencode.json` — porque e ele que roda. Sem este script, toda
mudanca de prompt ou de agente fica so na maquina onde foi feita, e a outra recebe
uma versao velha **sem que nada avise**. E a deriva silenciosa que o proprio
desenho template+instalador introduz; este script e o antidoto.

Fluxo: editar o `opencode.json`, testar, rodar este script, commitar o template.

    python sincronizar_template.py            # grava o template
    python sincronizar_template.py --conferir # so mostra se ha divergencia
"""
from __future__ import annotations

import argparse
import json
import shutil
import pathlib
import platform
import sys
from pathlib import Path

AQUI = Path(__file__).resolve().parent
CONFIG = AQUI / "opencode.json"
TEMPLATE = AQUI / "opencode.json.template"

# O que e especifico de maquina volta a ser marcador. Precisa espelhar
# exatamente o que o instalar.py sabe substituir.
MARCADORES = {
    "__CONFIG_DIR__": AQUI.as_posix(),
    "__MODEL_PEQUENO__": None,   # resolvido do proprio config
    "__MODEL__": None,
    "__FERRAMENTAS__": None,
    "__PYTHON_SISTEMA__": None,
    "__UV__": shutil.which("uv") or "uv",
}


def para_template(cfg: dict) -> dict:
    c = json.loads(json.dumps(cfg))  # copia profunda

    modelo = c.get("model", "")
    pequeno = c.get("agent", {}).get("title", {}).get("model", "")

    for chave in ("rag", "forense"):
        mcp = c.get("mcp", {}).get(chave)
        if not mcp:
            continue
        mcp["command"] = [x.replace(AQUI.as_posix(), "__CONFIG_DIR__") for x in mcp["command"]]
        if mcp["command"] and pathlib.Path(mcp["command"][0]).name in ("uv", "uv.exe"):
            mcp["command"][0] = "__UV__"
        env = mcp.setdefault("environment", {})
        if "ACERVO_FERRAMENTAS" in env:
            env["ACERVO_FERRAMENTAS"] = "__FERRAMENTAS__"
        if "ACERVO_PYTHON" in env:
            env["ACERVO_PYTHON"] = "__PYTHON_SISTEMA__"

    c["model"] = "__MODEL__"
    for nome, ag in c.get("agent", {}).items():
        if "model" not in ag:
            continue
        ag["model"] = "__MODEL_PEQUENO__" if ag["model"] == pequeno and nome == "title" \
            else ("__MODEL__" if ag["model"] == modelo else ag["model"])
    return c


def main() -> int:
    ap = argparse.ArgumentParser(description="Deriva o template do config local.")
    ap.add_argument("--conferir", action="store_true",
                    help="nao escreve; sai 1 se o template estiver desatualizado")
    args = ap.parse_args()

    if not CONFIG.exists():
        print(f"[ERRO] {CONFIG.name} nao existe. Rode instalar.py primeiro.")
        return 1

    novo = para_template(json.loads(CONFIG.read_text(encoding="utf-8")))
    texto = json.dumps(novo, indent=2, ensure_ascii=False) + "\n"

    atual = TEMPLATE.read_text(encoding="utf-8") if TEMPLATE.exists() else ""
    if atual.strip() == texto.strip():
        print("[ok] template ja esta em dia com o opencode.json")
        return 0

    if args.conferir:
        print("[DIVERGENTE] o opencode.json mudou e o template nao acompanhou.")
        print("             rode: python sincronizar_template.py")
        # mostra quais chaves diferem, para a conferencia ser util
        try:
            velho = json.loads(atual) if atual else {}
        except json.JSONDecodeError:
            velho = {}

        def achatar(d, pref=""):
            out = {}
            for k, v in (d or {}).items():
                n = f"{pref}.{k}" if pref else k
                out.update(achatar(v, n)) if isinstance(v, dict) else out.update({n: v})
            return out

        a, b = achatar(velho), achatar(novo)
        for k in sorted(set(a) | set(b)):
            if a.get(k) != b.get(k):
                print(f"             - {k}")
        return 1

    TEMPLATE.write_text(texto, encoding="utf-8")
    print(f"[ok] {TEMPLATE.name} atualizado a partir do opencode.json")
    print("     nao esqueca de commitar — e ele que a outra maquina recebe.")
    if platform.system() != "Windows":
        print("     (gerado em maquina nao-Windows: confira os marcadores antes de commitar)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
