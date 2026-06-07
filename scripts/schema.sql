-- Lilith v2 Engineering Toolkit
-- Run this in the Supabase SQL editor for your project.

-- Enable pgvector
create extension if not exists vector;

-- decisions
-- Source of truth for architectural policy.
-- The orchestrator reads this when briefing the coding agent.
-- The rationale column enables the advisor to push back intelligently.
create table decisions (
  id          uuid primary key default gen_random_uuid(),
  profile     text not null check (profile in ('personal','work')),
  category    text not null check (category in ('pattern','standard','rejected')),
  title       text not null,
  body        text not null,
  rationale   text,
  embedding   vector(1536),
  created_at  timestamptz default now()
);
create index on decisions using ivfflat (embedding vector_cosine_ops) with (lists = 100);

-- conversations
-- Full advisor chat history. Grouped by session_id.
-- Embeddings allow semantic recall of relevant past discussions.
create table conversations (
  id          uuid primary key default gen_random_uuid(),
  profile     text not null check (profile in ('personal','work')),
  session_id  uuid not null default gen_random_uuid(),
  role        text not null check (role in ('user','assistant')),
  content     text not null,
  embedding   vector(1536),
  created_at  timestamptz default now()
);
create index on conversations using ivfflat (embedding vector_cosine_ops) with (lists = 100);

-- ideas
-- The parking lot. Ideas surface during advisor discussions and get tagged.
-- The advisor retrieves parked ideas when related topics come up.
create table ideas (
  id          uuid primary key default gen_random_uuid(),
  profile     text not null check (profile in ('personal','work')),
  title       text not null,
  body        text not null,
  status      text not null default 'parked'
              check (status in ('parked','exploring','adopted','dropped')),
  tags        text[] default '{}',
  embedding   vector(1536),
  created_at  timestamptz default now()
);
create index on ideas using ivfflat (embedding vector_cosine_ops) with (lists = 100);

-- qa_reports
-- QA output. approved_by and approved_at are NULL until you run approve.ts.
-- The coding agent must check approved_at before executing any changes.
create table qa_reports (
  id                 uuid primary key default gen_random_uuid(),
  profile            text not null check (profile in ('personal','work')),
  spec_ref           uuid,
  findings           jsonb not null default '{}',
  orchestrator_note  text,
  approved_by        text,
  approved_at        timestamptz,
  created_at         timestamptz default now()
);

-- ─── pgvector similarity search functions ────────────────────────────────────
-- These are called by the TypeScript memory layer.

create or replace function match_decisions(
  query_embedding vector(1536),
  match_profile   text,
  match_count     int default 5
)
returns table (
  id uuid, profile text, category text, title text, body text,
  rationale text, created_at timestamptz, similarity float
)
language sql stable as $$
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
language sql stable as $$
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
language sql stable as $$
  select id, profile, title, body, status, tags, created_at,
         1 - (embedding <=> query_embedding) as similarity
  from ideas
  where profile = match_profile
    and embedding is not null
  order by embedding <=> query_embedding
  limit match_count;
$$;
