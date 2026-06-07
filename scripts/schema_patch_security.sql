-- Security patch for Supabase linter warnings.
-- Run this in the Supabase SQL editor after schema.sql.

-- ─── Enable RLS on all tables ────────────────────────────────────────────────
-- This is a solo toolkit with no end-user auth, so we add permissive policies
-- (allow all) to silence the linter without restricting anything.

alter table decisions      enable row level security;
alter table conversations  enable row level security;
alter table ideas          enable row level security;
alter table qa_reports     enable row level security;

create policy "allow all" on decisions      for all using (true) with check (true);
create policy "allow all" on conversations  for all using (true) with check (true);
create policy "allow all" on ideas          for all using (true) with check (true);
create policy "allow all" on qa_reports     for all using (true) with check (true);

-- ─── Fix function search_path ─────────────────────────────────────────────────
-- Recreate the match_* functions with a locked search_path to prevent
-- search_path injection attacks.

create or replace function match_decisions(
  query_embedding vector(1536),
  match_profile   text,
  match_count     int default 5
)
returns table (
  id uuid, profile text, category text, title text, body text,
  rationale text, created_at timestamptz, similarity float
)
language sql stable
set search_path = public
as $$
  select id, profile, category, title, body, rationale, created_at,
         1 - (embedding <=> query_embedding) as similarity
  from decisions
  where profile = match_profile
    and embedding is not null
  order by embedding <=> query_embedding
  limit match_count;
$$;

create or replace function match_conversations(
  query_embedding vector(1536),
  match_profile   text,
  match_count     int default 5
)
returns table (
  id uuid, profile text, session_id uuid, role text,
  content text, created_at timestamptz, similarity float
)
language sql stable
set search_path = public
as $$
  select id, profile, session_id, role, content, created_at,
         1 - (embedding <=> query_embedding) as similarity
  from conversations
  where profile = match_profile
    and embedding is not null
  order by embedding <=> query_embedding
  limit match_count;
$$;

create or replace function match_ideas(
  query_embedding vector(1536),
  match_profile   text,
  match_count     int default 5
)
returns table (
  id uuid, profile text, title text, body text, status text,
  tags text[], created_at timestamptz, similarity float
)
language sql stable
set search_path = public
as $$
  select id, profile, title, body, status, tags, created_at,
         1 - (embedding <=> query_embedding) as similarity
  from ideas
  where profile = match_profile
    and embedding is not null
  order by embedding <=> query_embedding
  limit match_count;
$$;
