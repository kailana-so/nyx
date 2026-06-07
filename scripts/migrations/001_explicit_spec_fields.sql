alter table specs
  add column if not exists files_to_touch text[] not null default '{}',
  add column if not exists do_not_change text not null default '',
  add column if not exists requires_thinking boolean not null default true;
