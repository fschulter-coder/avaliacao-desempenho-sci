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
from fastapi import FastAPI, UploadFile, File, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import supabase as sb

# ── App setup ─────────────────────────────────────────────────────────────────
app = FastAPI(title="Portal Avaliações SCI")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # Lovable vai definir a origem real
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Clients ───────────────────────────────────────────────────────────────────
anthropic_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

supabase_client = sb.create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_SERVICE_KEY"],     # service role — só no backend
)

security = HTTPBearer()

# ── Auth helper ───────────────────────────────────────────────────────────────
def get_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> dict:
    """Valida o JWT do Supabase e retorna o user dict."""
    token = credentials.credentials
    try:
        user = supabase_client.auth.get_user(token)
        if not user or not user.user:
            raise HTTPException(status_code=401, detail="Token inválido")
        return user.user
    except Exception:
        raise HTTPException(status_code=401, detail="Token inválido ou expirado")

# ── Paths ─────────────────────────────────────────────────────────────────────
ASSETS_DIR   = Path(__file__).parent / "assets"
OUTPUTS_DIR  = Path(tempfile.gettempdir()) / "sci_outputs"
OUTPUTS_DIR.mkdir(exist_ok=True)

# ── System prompt (skill embutida) ────────────────────────────────────────────
SYSTEM_PROMPT = open(Path(__file__).parent / "system_prompt.txt").read()

# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/analisar")
async def analisar(
    files: List[UploadFile] = File(...),
    user: dict = Depends(get_user),
):
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
    # Remove possíveis backticks que escapem do prompt
    raw = raw.replace("```json", "").replace("```", "").strip()
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail=f"Claude retornou JSON inválido: {e}\n\n{raw[:500]}")

    # 4. Executa cada script Node.js e coleta os arquivos
    job_id    = str(uuid.uuid4())
    job_dir   = OUTPUTS_DIR / job_id
    job_dir.mkdir()

    generated_files = []

    for analise in result.get("analises", []):
        sobrenome = analise["sobrenome"].lower()
        ciclo     = analise["ciclo_mais_recente"]

        for script_key, file_prefix in [
            ("script_relatorio",  f"relatorio_{sobrenome}_{ciclo}"),
            ("script_avaliador",  f"analise_avaliador_{sobrenome}_{ciclo}"),
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

    # 5. Salva metadados no Supabase para o histórico do usuário
    supabase_client.table("analises").insert({
        "user_id":    user.id,
        "job_id":     job_id,
        "arquivos":   json.dumps(generated_files),
        "colaboradores": json.dumps(result.get("colaboradores", [])),
    }).execute()

    return {
        "job_id": job_id,
        "colaboradores": result.get("colaboradores", []),
        "arquivos": generated_files,
    }


@app.get("/download/{job_id}/{filename}")
def download(
    job_id: str,
    filename: str,
    user: dict = Depends(get_user),
):
    """Download de um .docx gerado, verificando que pertence ao usuário."""
    # Confirma que o job pertence ao usuário
    rows = (
        supabase_client.table("analises")
        .select("job_id")
        .eq("user_id", user.id)
        .eq("job_id", job_id)
        .execute()
    )
    if not rows.data:
        raise HTTPException(status_code=403, detail="Acesso negado")

    file_path = OUTPUTS_DIR / job_id / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Arquivo não encontrado")

    return FileResponse(
        path=str(file_path),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=filename,
    )


@app.get("/historico")
def historico(user: dict = Depends(get_user)):
    """Retorna todas as análises anteriores do usuário."""
    rows = (
        supabase_client.table("analises")
        .select("*")
        .eq("user_id", user.id)
        .order("created_at", desc=True)
        .execute()
    )
    return rows.data
