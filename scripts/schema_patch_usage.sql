-- Usage tracking for cost reporting.
-- Run in Supabase SQL editor.

create table usage_records (
  id                 uuid primary key default gen_random_uuid(),
  profile            text not null check (profile in ('personal', 'work')),
  project            text not null default 'misc',
  agent              text not null,
  model              text not null,
  input_tokens       int  not null default 0,
  output_tokens      int  not null default 0,
  cache_write_tokens int  not null default 0,
  cache_read_tokens  int  not null default 0,
  cost_usd           numeric(10, 6) not null default 0,
  created_at         timestamptz default now()
);

alter table usage_records enable row level security;
create policy "allow all" on usage_records for all using (true) with check (true);
