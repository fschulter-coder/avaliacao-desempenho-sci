# Portal Avaliações de Desempenho SCI — Backend

API FastAPI que recebe PDFs de avaliação de desempenho, analisa com o Claude
e devolve os relatórios `.docx` no padrão visual SCI.

---

## Pré-requisitos

- Conta no [Railway](https://railway.app)
- Projeto no [Supabase](https://supabase.com) (o mesmo usado pelo Lovable)
- Chave da [Anthropic API](https://console.anthropic.com)

---

## Passo 1 — Supabase: criar a tabela

1. Acesse seu projeto no Supabase
2. Vá em **SQL Editor**
3. Cole e execute o conteúdo de `supabase_migration.sql`

---

## Passo 2 — Railway: criar o serviço

1. Acesse [railway.app](https://railway.app) → **New Project → Deploy from GitHub**
2. Conecte este repositório
3. O Railway detecta o `Dockerfile` automaticamente

### Variáveis de ambiente (obrigatórias)

Vá em **Settings → Variables** e adicione:

| Variável | Onde encontrar |
|---|---|
| `ANTHROPIC_API_KEY` | console.anthropic.com → API Keys |
| `SUPABASE_URL` | Supabase → Settings → API → Project URL |
| `SUPABASE_SERVICE_KEY` | Supabase → Settings → API → service_role key |

> ⚠️ Use a **service_role key** (não a anon key) — ela permite ao backend verificar JWTs e acessar dados sem RLS.

### URL pública

Após o deploy, o Railway gera uma URL como:
```
https://portal-sci-backend-production.up.railway.app
```
Guarde essa URL — ela vai no Lovable como `VITE_API_URL`.

---

## Passo 3 — Lovable: configurar a URL da API

No projeto Lovable, vá em **Settings → Environment Variables** e adicione:

```
VITE_API_URL=https://portal-sci-backend-production.up.railway.app
```

---

## Endpoints

| Método | Rota | Descrição |
|--------|------|-----------|
| GET | `/health` | Healthcheck |
| POST | `/analisar` | Envia PDFs → recebe lista de arquivos gerados |
| GET | `/download/{job_id}/{filename}` | Download do .docx |
| GET | `/historico` | Lista análises anteriores do usuário |

Todos os endpoints (exceto `/health`) exigem header:
```
Authorization: Bearer <supabase_jwt>
```

---

## Fluxo de dados

```
Lovable (React)
  → POST /analisar  (multipart, JWT no header)
  → Backend lê PDFs, chama Claude
  → Claude analisa + gera scripts Node.js
  → Backend executa scripts → .docx gerados
  → Retorna lista de arquivos com job_id

Lovable exibe botões de download
  → GET /download/{job_id}/{filename}
  → Backend verifica propriedade via Supabase
  → Retorna arquivo
```
