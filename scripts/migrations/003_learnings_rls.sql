-- Disable RLS on learnings to match the posture of the other tables in this CLI
-- (single-user personal app; RLS adds no value here and was blocking inserts).
alter table learnings disable row level security;
