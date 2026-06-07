-- Add specs table (referenced by qa_reports.spec_ref)

create table specs (
  id            uuid primary key default gen_random_uuid(),
  profile       text not null check (profile in ('personal','work')),
  title         text not null,
  objective     text not null,
  scope         text not null,
  out_of_scope  text,
  decisions_ref text[],
  criteria      jsonb not null default '[]',
  coding_notes  text,
  status        text not null default 'pending'
                check (status in ('pending','in_progress','qa','approved','done')),
  created_at    timestamptz default now()
);

-- Also add RLS to match other tables
alter table specs enable row level security;
create policy "allow all" on specs for all using (true) with check (true);
