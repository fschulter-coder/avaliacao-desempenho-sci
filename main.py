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
    allow_credentials=True,
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

# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/analisar")
async def analisar(files: List[UploadFile] = File(...)):
    """
    Recebe N PDFs de avaliação, chama o Claude, executa o Node.js gerado
    e devolve os .docx gerados.
    """
    if not files:
        raise HTTPException(status_code=400, detail="Nenhum arquivo enviado")

    # 1. Lê todos os PDFs e converte para base64
    pdf_blocks = []
    for f in files:
        data = await f.read()
        b64 = base64.standard_b64encode(data).decode("utf-8")
        pdf_blocks.append({
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": b64,
            },
            "title": f.filename,
        })

    # 2. Chama o Claude com todos os PDFs
    pdf_blocks.append({
        "type": "text",
        "text": (
            "Analise os PDFs de avaliação de desempenho acima seguindo o sistema prompt. "
            "Retorne APENAS um objeto JSON com a estrutura:\n"
            "{\n"
            '  "colaboradores": ["Nome Completo 1", "Nome Completo 2"],\n'
            '  "analises": [\n'
            "    {\n"
            '      "nome": "Nome Completo",\n'
            '      "sobrenome": "Sobrenome",\n'
            '      "ciclo_mais_recente": "mar2025",\n'
            '      "script_relatorio": "...código Node.js completo para gerar relatorio_sobrenome_ciclo.docx...",\n'
            '      "script_avaliador": "...código Node.js completo para gerar analise_avaliador_sobrenome_ciclo.docx..."\n'
            "    }\n"
            "  ]\n"
            "}\n\n"
            "Cada script deve ser Node.js completo e auto-suficiente usando o pacote 'docx'. "
            "Os arquivos de assets estão em: " + str(ASSETS_DIR) + "\n"
            "Salve os .docx em: " + str(OUTPUTS_DIR) + "\n"
            "Não inclua markdown, apenas JSON puro."
        ),
    })

    response = anthropic_client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": pdf_blocks}],
    )

    # 3. Extrai e parseia o JSON
    raw = response.content[0].text.strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail=f"Claude retornou JSON inválido: {e}\n\n{raw[:500]}")

    # 4. Executa cada script Node.js e coleta os arquivos
    job_id  = str(uuid.uuid4())
    job_dir = OUTPUTS_DIR / job_id
    job_dir.mkdir()

    generated_files = []

    for analise in result.get("analises", []):
        sobrenome = analise["sobrenome"].lower()
        ciclo     = analise["ciclo_mais_recente"]

        for script_key, file_prefix in [
            ("script_relatorio", f"relatorio_{sobrenome}_{ciclo}"),
            ("script_avaliador", f"analise_avaliador_{sobrenome}_{ciclo}"),
        ]:
            script_code = analise.get(script_key, "")
            if not script_code:
                continue

            script_path = job_dir / f"{file_prefix}.js"
            script_path.write_text(script_code, encoding="utf-8")

            proc = subprocess.run(
                ["node", str(script_path)],
                capture_output=True, text=True, timeout=120,
            )
            if proc.returncode != 0:
                raise HTTPException(
                    status_code=500,
                    detail=f"Erro ao gerar {file_prefix}.docx:\n{proc.stderr[-1000:]}",
                )

            # O script salva em OUTPUTS_DIR — move para a pasta do job
            docx_path = OUTPUTS_DIR / f"{file_prefix}.docx"
            if docx_path.exists():
                dest = job_dir / f"{file_prefix}.docx"
                shutil.move(str(docx_path), str(dest))

            generated_files.append({
                "nome":     analise["nome"],
                "tipo":     "relatorio" if "relatorio" in file_prefix else "avaliador",
                "filename": f"{file_prefix}.docx",
                "job_id":   job_id,
            })

    return {
        "job_id":        job_id,
        "colaboradores": result.get("colaboradores", []),
        "arquivos":      generated_files,
    }


@app.get("/download/{job_id}/{filename}")
def download(job_id: str, filename: str):
    """Download de um .docx gerado."""
    # Sanitiza para evitar path traversal
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
