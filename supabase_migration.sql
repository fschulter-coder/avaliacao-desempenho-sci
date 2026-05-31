-- Execute no Supabase > SQL Editor

create table if not exists public.analises (
  id           uuid primary key default gen_random_uuid(),
  user_id      uuid not null references auth.users(id) on delete cascade,
  job_id       text not null unique,
  colaboradores jsonb,
  arquivos      jsonb,
  created_at   timestamptz default now()
);

-- Só o próprio usuário enxerga suas análises
alter table public.analises enable row level security;

create policy "owner only" on public.analises
  for all using (auth.uid() = user_id);
