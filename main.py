import os
import uuid
import base64
import json
import subprocess
import tempfile
import shutil
from pathlib import Path
from typing import List

import anthropic
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

# ── App setup ─────────────────────────────────────────────────────────────────
app = FastAPI(title="Portal Avaliações SCI")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Clients ───────────────────────────────────────────────────────────────────
anthropic_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

# ── Paths ─────────────────────────────────────────────────────────────────────
ASSETS_DIR  = Path(__file__).parent / "assets"
OUTPUTS_DIR = Path(tempfile.gettempdir()) / "sci_outputs"
OUTPUTS_DIR.mkdir(exist_ok=True)

# ── System prompt (skill embutida) ────────────────────────────────────────────
SYSTEM_PROMPT = open(Path(__file__).parent / "system_prompt.txt").read()


def chamar_claude(pdf_blocks: list, instrucao: str) -> str:
    """Chama o Claude com os PDFs e uma instrução, retorna o script Node.js."""
    messages = pdf_blocks + [{"type": "text", "text": instrucao}]
    response = anthropic_client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": messages}],
    )
    raw = response.content[0].text.strip()
    # Remove possíveis backticks
    raw = raw.replace("```javascript", "").replace("```js", "").replace("```", "").strip()
    return raw


def executar_script(script_code: str, job_dir: Path, file_prefix: str) -> str:
    """Executa um script Node.js e retorna o path do .docx gerado."""
    script_path = job_dir / f"{file_prefix}.js"
    script_path.write_text(script_code, encoding="utf-8")

    proc = subprocess.run(
        ["node", str(script_path)],
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"Erro ao gerar {file_prefix}.docx:\n{proc.stderr[-2000:]}",
        )

    # O script salva em OUTPUTS_DIR — move para a pasta do job
    docx_path = OUTPUTS_DIR / f"{file_prefix}.docx"
    dest = job_dir / f"{file_prefix}.docx"
    if docx_path.exists():
        shutil.move(str(docx_path), str(dest))
    elif not dest.exists():
        raise HTTPException(
            status_code=500,
            detail=f"Arquivo {file_prefix}.docx não foi gerado.\nStdout: {proc.stdout[-1000:]}",
        )
    return f"{file_prefix}.docx"


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/analisar")
async def analisar(files: List[UploadFile] = File(...)):
    if not files:
        raise HTTPException(status_code=400, detail="Nenhum arquivo enviado")

    # 1. Converte PDFs para base64
    pdf_blocks = []
    for f in files:
        data = await f.read()
        b64 = base64.standard_b64encode(data).decode("utf-8")
        pdf_blocks.append({
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": b64},
            "title": f.filename,
        })

    # 2. Primeira chamada: identifica colaboradores
    identificacao_raw = chamar_claude(pdf_blocks, (
        "Leia os PDFs e retorne APENAS um JSON puro (sem markdown) com:\n"
        '{"colaboradores": [{"nome": "Nome Completo", "sobrenome": "sobrenome", "ciclo_mais_recente": "mar2026"}]}\n'
        "Nada mais, apenas o JSON."
    ))
    identificacao_raw = identificacao_raw.replace("```json", "").replace("```", "").strip()
    try:
        identificacao = json.loads(identificacao_raw)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail=f"Erro ao identificar colaboradores: {e}\n{identificacao_raw[:300]}")

    colaboradores = identificacao.get("colaboradores", [])
    if not colaboradores:
        raise HTTPException(status_code=500, detail="Nenhum colaborador identificado nos PDFs")

    job_id  = str(uuid.uuid4())
    job_dir = OUTPUTS_DIR / job_id
    job_dir.mkdir()

    generated_files = []

    # 3. Para cada colaborador, duas chamadas separadas
    for colab in colaboradores:
        nome      = colab["nome"]
        sobrenome = colab["sobrenome"].lower()
        ciclo     = colab["ciclo_mais_recente"]

        # 3a. Gera script do relatório do colaborador
        script_relatorio = chamar_claude(pdf_blocks, (
            f"Analise os PDFs de avaliação de desempenho do colaborador '{nome}' seguindo o sistema prompt.\n"
            f"Gere APENAS o código Node.js completo e auto-suficiente usando o pacote 'docx' para criar o arquivo "
            f"relatorio_{sobrenome}_{ciclo}.docx.\n"
            f"Assets em: {ASSETS_DIR}\n"
            f"Salve o arquivo em: {OUTPUTS_DIR}/relatorio_{sobrenome}_{ciclo}.docx\n"
            "Retorne APENAS o código JavaScript, sem explicações, sem markdown."
        ))
        filename_rel = executar_script(script_relatorio, job_dir, f"relatorio_{sobrenome}_{ciclo}")
        generated_files.append({"nome": nome, "tipo": "relatorio", "filename": filename_rel, "job_id": job_id})

        # 3b. Gera script da análise do avaliador
        script_avaliador = chamar_claude(pdf_blocks, (
            f"Analise os PDFs de avaliação de desempenho do colaborador '{nome}' seguindo o sistema prompt.\n"
            f"Gere APENAS o código Node.js completo e auto-suficiente usando o pacote 'docx' para criar o arquivo "
            f"analise_avaliador_{sobrenome}_{ciclo}.docx.\n"
            f"Assets em: {ASSETS_DIR}\n"
            f"Salve o arquivo em: {OUTPUTS_DIR}/analise_avaliador_{sobrenome}_{ciclo}.docx\n"
            "Retorne APENAS o código JavaScript, sem explicações, sem markdown."
        ))
        filename_av = executar_script(script_avaliador, job_dir, f"analise_avaliador_{sobrenome}_{ciclo}")
        generated_files.append({"nome": nome, "tipo": "avaliador", "filename": filename_av, "job_id": job_id})

    return {
        "job_id":        job_id,
        "colaboradores": [c["nome"] for c in colaboradores],
        "arquivos":      generated_files,
    }


@app.get("/download/{job_id}/{filename}")
def download(job_id: str, filename: str):
    if ".." in job_id or ".." in filename:
        raise HTTPException(status_code=400, detail="Caminho inválido")

    file_path = OUTPUTS_DIR / job_id / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Arquivo não encontrado")

    return FileResponse(
        path=str(file_path),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=filename,
    )
