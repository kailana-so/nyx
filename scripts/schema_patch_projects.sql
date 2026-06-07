-- Add project column to all tables.
-- Default is 'misc' for non-project-specific work.
-- Run in Supabase SQL editor.

alter table decisions     add column if not exists project text not null default 'misc';
alter table conversations add column if not exists project text not null default 'misc';
alter table ideas         add column if not exists project text not null default 'misc';
alter table qa_reports    add column if not exists project text not null default 'misc';
alter table specs         add column if not exists project text not null default 'misc';

-- Update match functions to optionally filter by project

create or replace function match_conversations(
  query_embedding vector(1024),
  match_profile   text,
  match_count     int default 5,
  match_project   text default null
)
returns table (
  id uuid, profile text, project text, session_id uuid, role text,
  content text, created_at timestamptz, similarity float
)
language sql stable
set search_path = public
as $$
  select id, profile, project, session_id, role, content, created_at,
         1 - (embedding <=> query_embedding) as similarity
  from conversations
  where profile = match_profile
    and (match_project is null or project = match_project)
    and embedding is not null
  order by embedding <=> query_embedding
  limit match_count;
$$;

create or replace function match_decisions(
  query_embedding vector(1024),
  match_profile   text,
  match_count     int default 5,
  match_project   text default null
)
returns table (
  id uuid, profile text, project text, category text, title text, body text,
  rationale text, created_at timestamptz, similarity float
)
language sql stable
set search_path = public
as $$
  select id, profile, project, category, title, body, rationale, created_at,
         1 - (embedding <=> query_embedding) as similarity
  from decisions
  where profile = match_profile
    and (match_project is null or project = match_project)
    and embedding is not null
  order by embedding <=> query_embedding
  limit match_count;
$$;

create or replace function match_ideas(
  query_embedding vector(1024),
  match_profile   text,
  match_count     int default 5,
  match_project   text default null
)
returns table (
  id uuid, profile text, project text, title text, body text, status text,
  tags text[], created_at timestamptz, similarity float
)
language sql stable
set search_path = public
as $$
  select id, profile, project, title, body, status, tags, created_at,
         1 - (embedding <=> query_embedding) as similarity
  from ideas
  where profile = match_profile
    and (match_project is null or project = match_project)
    and embedding is not null
  order by embedding <=> query_embedding
  limit match_count;
$$;
