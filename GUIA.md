# Agentic RAG + Investigative Agent — local, no OpenCode

Instalado e validado em 23/08/2026 na máquina Windows. Nada sai desta máquina:
LLM, embeddings, índice, banco e grafo são todos locais.

---

## Arranque rápido

Para investigação que vai virar peça, use o invólucro — ele **impõe** a auditoria de
citações, independentemente do que o modelo fizer:

```bash
python investigar.py <caso> "<pergunta>"
```

Códigos de saída: **0** limpo · **2** citação que não se sustenta · **3** o agente alegou
ter auditado sem chamar a ferramenta. Para auditar um transcript já salvo, sem rodar o
agente de novo: `python investigar.py <caso> x --somente-auditar transcript.txt`.

Para exploração interativa, sem imposição:

```bash
opencode --agent investigador
```

Primeira pergunta de um caso novo:

```
Ingira C:\Casos\operacao-x\trabalho no caso 'operacao-x', origem "META".
Depois: quais contas convergem no mesmo IP?
```

Uma pergunta só, sem sessão interativa:

```bash
opencode run --agent investigador "No caso 'operacao-x': existe relacao entre @conta_a e @conta_b?"
```

---

## As três camadas

| Nível | Agente | Pergunta típica |
|---|---|---|
| 1–2 — RAG agêntico | `--agent rag` | "O que a base diz sobre X?" |
| 3 — investigativo | `--agent investigador` | "Investigue as conexões entre X, Y e Z." |

O `rag` trabalha sobre bases de estudo (vault, documentação, código) em **coleções**.
O `investigador` trabalha sobre acervo de caso em **casos** — com custódia, entidades,
SQL e grafo. Não misture os dois.

---

## O que roda por baixo

```
                    opencode --agent investigador
                         (qwen3:8b, local)
                                │
        ┌───────────────┬───────┴───────┬────────────────┐
        ▼               ▼               ▼                ▼
   busca híbrida     SQL real      grafo de vínculos   proveniência
   semântica +     (tabelas de     (NetworkX sobre     (arquivo:linha
   literal          CSV/TSV)        co-ocorrência)      + SHA-256/512)
        └───────────────┴───────┬───────┴────────────────┘
                                ▼
                    SQLite por caso + embeddings
                       (nomic-embed-text, local)
```

**A extração de identificadores é determinística**, não do modelo: regex com dígito
verificador (CPF, CNPJ), Luhn (IMEI) e validação de faixa (IPv4). Reprodutível e auditável
— "o modelo achou" não é.

Tipos extraídos: `cpf`, `cnpj`, `email`, `ipv4`, `ipv6`, `imei`, `telefone`, `mac`,
`placa`, `processo_cnj`, `url`, `hash`, `btc`, `usuario`, `timestamp`.

IPs privados, loopback e multicast são descartados — poluiriam o grafo sem indicar vínculo.

---

## Ferramentas do investigador

| Ferramenta | Para quê |
|---|---|
| `forense_ingest` | ingere pasta/arquivo: hash de custódia, texto, entidades, índice, tabelas SQL |
| `forense_status` | o que o caso contém |
| `forense_search_exact` | busca literal — IP, IMEI, CPF, hash, usuário |
| `forense_search_semantic` | busca por significado — conceitos |
| `forense_sql` / `forense_schema` | SELECT sobre CSV/TSV ingeridos e sobre as tabelas do sistema |
| `forense_path` | como X liga em Y, elo por elo, com documento e linha |
| `forense_connections` | vizinhança de uma entidade, até N saltos |
| `forense_shared` | pontos de convergência — mesmo IP/dispositivo em contas distintas |
| `forense_timeline` | carimbos de tempo, opcionalmente de uma entidade |
| `forense_mentions` | **proveniência**: toda ocorrência com arquivo, linha, método, confiança, hash |
| `forense_doc_read` | lê a fonte bruta numa faixa de linhas |
| `forense_custodia` | re-hasheia tudo e acusa arquivo alterado/ausente |
| `forense_registrar` / `forense_historico` | diário reproduzível da investigação |
| **`forense_auditar_citacoes`** | **confere toda citação `arquivo:linha` contra o banco — passo final obrigatório** |

### Ponte com `C:\ACERVO\Ferramentas` (seus scripts, chamados pelo agente)

| Ferramenta | Para quê |
|---|---|
| `forense_validar_lote` | porta de validação obrigatória — DV, DDD, nono dígito, IMEI, CGNAT, mojibake |
| `forense_resumo_alvo` | pergunta ao índice do acervo (décimos de segundo, em vez de minutos de `os.walk`) |
| `forense_indexar_acervo` | constrói/atualiza o índice; `amostra=3` confere o leitor antes do passe completo |
| `forense_geocodificar` | endereço → coordenada **offline** pelo CNEFE; devolve o grau, nunca chuta |
| `forense_biblioteca_status` | diz se os validadores em uso são os da biblioteca ou o fallback |

Os validadores de CPF/CNPJ/IMEI da extração **são importados** de `forense_correlacao` e
`forense_validacao` — não há segunda implementação divergindo do seu pipeline.

Ferramentas do `rag`: `rag_index`, `rag_search`, `rag_grep`, `rag_expand`,
`rag_collections`, `rag_sources`, `rag_forget`.

---

## Formatos aceitos

PDF (camada de texto via `pdftotext`; sem ela, `ocr=true` usa a calibração medida — 300 dpi,
cinza, 2× LANCZOS, contraste 3.0, `--psm 4 -l por`), HTML, CSV/TSV (viram **tabela SQL**),
TXT, MD, LOG, JSON, XML, EML, VCF.

---

## Desempenho medido nesta máquina (8 GB de VRAM)

| Modelo | Resposta trivial | Uso |
|---|---|---|
| `qwen3:8b` | ~10 s | **padrão** — cabe inteiro na VRAM |
| `qwen3-coder:30b` | ~56 s (71% em CPU) | redação final, análise densa |

Um ciclo investigativo completo com `qwen3:8b`: **~5 minutos**. Com o 30b seria 3–4× isso.

Trocar de cérebro numa pergunta:

```bash
opencode run --agent investigador --model ollama/qwen3-coder:30b "..."
```

> No MacBook M5 (48 GB unificados) o 30b roda 100% em GPU — lá vale usá-lo como padrão.
> Basta copiar `~/.config/opencode/` e trocar `"model"` para `ollama/qwen3-coder:30b`.

---

## Auditoria de citação

⚠️ **O prompt manda o agente auditar, e ele nem sempre audita — pior, já escreveu que
auditou sem ter chamado a ferramenta** (medido em 24/08/2026). Por isso a auditoria de
verdade é a do `investigar.py`, que roda por fora. Trate a frase do agente sobre a própria
auditoria como não escrita. Para conferir um
relatório já pronto, fora do agente:

```bash
uv run --script ~/.config/opencode/rag/forense_server.py --auditar relatorio.md <caso>
```

Sai com código **2** quando há citação que não se sustenta, então encadeia num pipeline:
`... --auditar peca.md caso-x && gerar_pdf.py`.

Três veredictos: `VERIFICADA` (linha existe e tem entidade extraída), `SEM MENCAO` (linha
existe, nada foi extraído dela — citação provavelmente enxertada de outro arquivo) e
`LINHA INEXISTENTE` (o arquivo não tem essa linha).

⚠️ `VERIFICADA` diz que a linha existe e contém entidade — **não** que a entidade seja a que
o texto afirma. O auditor lista o que há na linha justamente para você comparar.

## Limitações reais, medidas nos testes

- **O modelo inventava citação `arquivo:linha`** — corrigido em 24/08 fazendo a ferramenta
  entregar a linha pronta, mais a auditoria obrigatória acima. Antes da correção, duas de
  duas citações eram falsas; depois, quatro de quatro corretas.
- **O modelo de 8b às vezes inventa lacuna** — declarou "não há IMEI no acervo" quando havia.
  Há regra no prompt mandando confirmar com `forense_status` antes de afirmar ausência, mas
  **confira toda seção LACUNAS**.
- **Modelo local erra a contagem de fontes.** Duas linhas do mesmo arquivo não são duas
  fontes. Há regra explícita no prompt; ainda assim, verifique.
- **Vínculo é por co-ocorrência na mesma linha/registro.** Documento cujo layout separa o
  dado em linhas distintas (alguns PDFs) gera menos arestas. Se o grafo vier pobre,
  confira com `forense_doc_read` como o texto foi extraído.
- **Ausência de caminho no grafo ≠ ausência de relação.** Significa que este acervo não a
  registra.

---

## Custódia

`forense_ingest` grava SHA-256 e SHA-512 de cada arquivo no momento da ingestão.
`forense_custodia` re-hasheia e acusa alterado/ausente. A vault exige **SHA-512** na peça
final — o comando entrega.

Trabalhar sempre sobre cópia em `C:\Casos\<caso>\`, nunca sobre a mídia original.

---

## Arquivos

```
~/.config/opencode/
├── opencode.json          provedor Ollama, modelos, MCP, agentes
├── AGENTS.md              doutrina de operação (carregada em toda sessão)
├── GUIA.md                este arquivo
└── rag/
    ├── rag_server.py      MCP: RAG agêntico genérico
    ├── forense_server.py  MCP: camada investigativa
    └── store/
        ├── *.db           coleções do rag
        └── casos/*.db     um banco por caso
```

Backup: estes arquivos ficam fora do `~/.claude`, então **não** entram no repositório
`claude-config`. Versione-os à parte se quiser sincronizar com o Mac.
