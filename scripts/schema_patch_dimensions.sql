-- Fix vector dimensions: voyage-3 outputs 1024 dims, not 1536.
-- Safe to run on an empty DB — drops and recreates embedding columns + indexes + functions.

-- decisions
alter table decisions drop column embedding;
alter table decisions add column embedding vector(1024);
drop index if exists decisions_embedding_idx;
create index on decisions using ivfflat (embedding vector_cosine_ops) with (lists = 100);

-- conversations
alter table conversations drop column embedding;
alter table conversations add column embedding vector(1024);
drop index if exists conversations_embedding_idx;
create index on conversations using ivfflat (embedding vector_cosine_ops) with (lists = 100);

-- ideas
alter table ideas drop column embedding;
alter table ideas add column embedding vector(1024);
drop index if exists ideas_embedding_idx;
create index on ideas using ivfflat (embedding vector_cosine_ops) with (lists = 100);

-- Recreate match functions with correct dimensions

create or replace function match_decisions(
  query_embedding vector(1024),
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
  query_embedding vector(1024),
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
  query_embedding vector(1024),
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
