-- Personal study log. NOT tied to a project; pivots on topic / language / kind.
-- Matches existing voyage-3 1024-dim embeddings used elsewhere.

create table if not exists learnings (
    id uuid primary key default gen_random_uuid(),
    profile text not null,
    title text not null,
    body text not null,
    topic text not null,
    language text,
    kind text not null check (kind in ('concept', 'pattern', 'gotcha', 'exercise', 'definition')),
    tags text[] not null default '{}',
    source text,
    embedding vector(1024),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists learnings_profile_topic_idx on learnings (profile, topic);
create index if not exists learnings_profile_language_idx on learnings (profile, language);
create index if not exists learnings_profile_kind_idx on learnings (profile, kind);
create index if not exists learnings_embedding_idx on learnings using ivfflat (embedding vector_cosine_ops);

-- Auto-bump updated_at on edits
create or replace function set_updated_at() returns trigger as $$
begin
  new.updated_at := now();
  return new;
end;
$$ language plpgsql;

drop trigger if exists learnings_set_updated_at on learnings;
create trigger learnings_set_updated_at
  before update on learnings
  for each row execute procedure set_updated_at();

-- Semantic search RPC, parallels match_decisions / match_conversations
create or replace function match_learnings(
    query_embedding vector(1024),
    match_count int default 5,
    match_profile text default null,
    match_topic text default null,
    match_kind text default null
)
returns table (
    id uuid,
    title text,
    body text,
    topic text,
    language text,
    kind text,
    tags text[],
    source text,
    created_at timestamptz,
    similarity float
)
language sql stable
as $$
  select id, title, body, topic, language, kind, tags, source, created_at,
         1 - (embedding <=> query_embedding) as similarity
  from learnings
  where (match_profile is null or profile = match_profile)
    and (match_topic is null or topic = match_topic)
    and (match_kind is null or kind = match_kind)
    and embedding is not null
  order by embedding <=> query_embedding
  limit match_count;
$$;
