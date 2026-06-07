from __future__ import annotations
import os
from dataclasses import dataclass
from supabase import create_client, Client

_client: Client | None = None

def _db() -> Client:
    global _client
    if _client is None:
        _client = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    return _client

# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class Decision:
    id: str
    category: str
    title: str
    body: str
    rationale: str
    created_at: str

@dataclass
class Idea:
    id: str
    title: str
    body: str
    status: str
    tags: list[str]
    created_at: str

@dataclass
class Conversation:
    id: str
    role: str
    content: str
    created_at: str

@dataclass
class Learning:
    id: str
    title: str
    body: str
    topic: str
    language: str | None
    kind: str
    tags: list[str]
    source: str | None
    created_at: str
    updated_at: str = ""

@dataclass
class UsageRecord:
    id: str
    profile: str
    project: str
    agent: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_write_tokens: int
    cache_read_tokens: int
    cost_usd: float
    created_at: str


@dataclass
class Spec:
    id: str
    title: str
    objective: str
    scope: str
    out_of_scope: str
    criteria: list[dict]
    coding_notes: str
    status: str
    created_at: str
    files_to_touch: list[str]
    do_not_change: str
    requires_thinking: bool
    project: str = ""

# ── Decisions ──────────────────────────────────────────────────────────────────

def get_decisions(profile: str, project: str) -> list[Decision]:
    rows = (_db().table("decisions")
        .select("id,category,title,body,rationale,created_at")
        .eq("profile", profile).eq("project", project)
        .order("created_at", desc=True)
        .execute().data)
    return [Decision(**r) for r in rows]

def save_decision(profile: str, project: str, category: str, title: str,
                  body: str, rationale: str, embedding: list[float]) -> None:
    _db().table("decisions").insert({
        "profile": profile, "project": project,
        "category": category, "title": title,
        "body": body, "rationale": rationale,
        "embedding": embedding,
    }).execute()

def search_decisions(embedding: list[float], profile: str,
                     limit: int = 5, project: str | None = None) -> list[Decision]:
    params = {"query_embedding": embedding, "match_count": limit,
              "match_profile": profile, "match_project": project or None}
    rows = _db().rpc("match_decisions", params).execute().data or []
    return [Decision(id=r["id"], category=r.get("category",""), title=r["title"],
                     body=r["body"], rationale=r.get("rationale",""),
                     created_at=r.get("created_at","")) for r in rows]

# ── Ideas ──────────────────────────────────────────────────────────────────────

def get_ideas(profile: str, status: str, project: str) -> list[Idea]:
    rows = (_db().table("ideas")
        .select("id,title,body,status,tags,created_at")
        .eq("profile", profile).eq("project", project).eq("status", status)
        .order("created_at", desc=True).execute().data)
    return [Idea(**r) for r in rows]

def save_idea(profile: str, project: str, title: str, body: str,
              tags: list[str], embedding: list[float]) -> str:
    row = _db().table("ideas").insert({
        "profile": profile, "project": project,
        "title": title, "body": body, "status": "parked",
        "tags": tags, "embedding": embedding,
    }).execute().data[0]
    return row["id"]

def update_idea_status(idea_id: str, status: str) -> None:
    _db().table("ideas").update({"status": status}).eq("id", idea_id).execute()

def list_all_ideas(profile: str, project: str) -> list[Idea]:
    rows = (_db().table("ideas")
        .select("id,title,body,status,tags,created_at")
        .eq("profile", profile).eq("project", project)
        .order("created_at", desc=True).execute().data)
    return [Idea(**r) for r in rows]

# ── Conversations ──────────────────────────────────────────────────────────────

def get_recent_conversations(profile: str, project: str,
                              limit: int = 8) -> list[Conversation]:
    rows = (_db().table("conversations")
        .select("id,role,content,created_at")
        .eq("profile", profile).eq("project", project)
        .order("created_at", desc=True).limit(limit)
        .execute().data)
    return [Conversation(**r) for r in reversed(rows)]

def save_conversation(profile: str, project: str, role: str,
                      content: str, embedding: list[float]) -> None:
    _db().table("conversations").insert({
        "profile": profile, "project": project,
        "role": role, "content": content, "embedding": embedding,
    }).execute()

def search_conversations(embedding: list[float], profile: str,
                         limit: int = 6, project: str | None = None) -> list[Conversation]:
    params = {"query_embedding": embedding, "match_count": limit,
              "match_profile": profile, "match_project": project or None}
    rows = _db().rpc("match_conversations", params).execute().data or []
    return [Conversation(id=r["id"], role=r["role"], content=r["content"],
                         created_at=r.get("created_at","")) for r in rows]

# ── Specs ──────────────────────────────────────────────────────────────────────

def create_spec(profile: str, project: str, title: str, objective: str,
                scope: str, out_of_scope: str, criteria: list[dict],
                coding_notes: str, files_to_touch: list[str],
                do_not_change: str, requires_thinking: bool) -> Spec:
    row = _db().table("specs").insert({
        "profile": profile, "project": project,
        "title": title, "objective": objective,
        "scope": scope, "out_of_scope": out_of_scope,
        "criteria": criteria, "coding_notes": coding_notes,
        "files_to_touch": files_to_touch, "do_not_change": do_not_change,
        "requires_thinking": requires_thinking,
        "status": "pending",
    }).execute().data[0]
    return Spec(id=row["id"], title=row["title"], objective=row["objective"],
                scope=row["scope"], out_of_scope=row["out_of_scope"],
                criteria=row["criteria"], coding_notes=row["coding_notes"],
                status=row["status"], created_at=row["created_at"],
                files_to_touch=row.get("files_to_touch", []),
                do_not_change=row.get("do_not_change", ""),
                requires_thinking=row.get("requires_thinking", True),
                project=row.get("project", project))

def get_spec(spec_id: str) -> Spec:
    row = _db().table("specs").select("*").eq("id", spec_id).single().execute().data
    return Spec(**{k: row[k] for k in Spec.__dataclass_fields__})

def get_pending_specs(profile: str, project: str) -> list[Spec]:
    rows = (_db().table("specs")
        .select("id,title,objective,scope,out_of_scope,criteria,coding_notes,status,created_at,files_to_touch,do_not_change,requires_thinking,project")
        .eq("profile", profile).eq("project", project)
        .in_("status", ["pending", "in_progress"])
        .order("created_at", desc=True).execute().data)
    return [Spec(**r) for r in rows]

def update_spec_status(spec_id: str, status: str) -> None:
    _db().table("specs").update({"status": status}).eq("id", spec_id).execute()

def list_all_specs(profile: str, project: str) -> list[Spec]:
    rows = (_db().table("specs")
        .select("id,title,objective,scope,out_of_scope,criteria,coding_notes,status,created_at,files_to_touch,do_not_change,requires_thinking,project")
        .eq("profile", profile).eq("project", project)
        .order("created_at", desc=True).execute().data)
    return [Spec(**r) for r in rows]

def delete_spec(spec_id: str) -> None:
    _db().table("specs").delete().eq("id", spec_id).execute()

# ── Learnings ──────────────────────────────────────────────────────────────────

_LEARNING_FIELDS = "id,title,body,topic,language,kind,tags,source,created_at,updated_at"


def _row_to_learning(row: dict) -> Learning:
    return Learning(
        id=row["id"], title=row["title"], body=row["body"],
        topic=row["topic"], language=row.get("language"),
        kind=row["kind"], tags=row.get("tags", []) or [],
        source=row.get("source"),
        created_at=row.get("created_at", ""),
        updated_at=row.get("updated_at", ""),
    )


def save_learning(profile: str, title: str, body: str, topic: str, kind: str,
                  tags: list[str], language: str | None, source: str | None,
                  embedding: list[float]) -> str:
    row = _db().table("learnings").insert({
        "profile": profile,
        "title": title, "body": body,
        "topic": topic, "language": language,
        "kind": kind, "tags": tags, "source": source,
        "embedding": embedding,
    }).execute().data[0]
    return row["id"]


def update_learning(learning_id: str, **fields) -> None:
    fields = {k: v for k, v in fields.items() if v is not None}
    if not fields:
        return
    _db().table("learnings").update(fields).eq("id", learning_id).execute()


def delete_learning(learning_id: str) -> None:
    _db().table("learnings").delete().eq("id", learning_id).execute()


def get_learning(learning_id: str) -> Learning:
    row = (_db().table("learnings")
        .select(_LEARNING_FIELDS)
        .eq("id", learning_id).single().execute().data)
    return _row_to_learning(row)


def list_learnings(profile: str, topic: str | None = None,
                   language: str | None = None, kind: str | None = None,
                   limit: int = 100) -> list[Learning]:
    q = (_db().table("learnings")
        .select(_LEARNING_FIELDS)
        .eq("profile", profile))
    if topic:
        q = q.eq("topic", topic)
    if language:
        q = q.eq("language", language)
    if kind:
        q = q.eq("kind", kind)
    rows = q.order("created_at", desc=True).limit(limit).execute().data or []
    return [_row_to_learning(r) for r in rows]


def search_learnings(embedding: list[float], profile: str, limit: int = 5,
                     topic: str | None = None, kind: str | None = None) -> list[Learning]:
    params = {
        "query_embedding": embedding,
        "match_count": limit,
        "match_profile": profile,
        "match_topic": topic,
        "match_kind": kind,
    }
    rows = _db().rpc("match_learnings", params).execute().data or []
    return [_row_to_learning(r) for r in rows]


def list_learning_topics(profile: str) -> list[tuple[str, int]]:
    """Distinct topics with row counts, ordered by count desc."""
    rows = (_db().table("learnings")
        .select("topic")
        .eq("profile", profile).execute().data or [])
    counts: dict[str, int] = {}
    for r in rows:
        t = r.get("topic")
        if t:
            counts[t] = counts.get(t, 0) + 1
    return sorted(counts.items(), key=lambda kv: -kv[1])


# ── Projects ───────────────────────────────────────────────────────────────────

def get_projects(profile: str) -> list[str]:
    """Distinct project labels across specs / ideas / decisions / conversations."""
    tables = ("specs", "ideas", "decisions", "conversations")
    seen: set[str] = set()
    for t in tables:
        rows = _db().table(t).select("project").eq("profile", profile).execute().data or []
        for r in rows:
            if r.get("project"):
                seen.add(r["project"])
    return sorted(seen)

def delete_project(profile: str, project: str) -> dict[str, int]:
    """Delete all rows tagged with this project across all tables. Returns row counts per table."""
    counts: dict[str, int] = {}
    for t in ("specs", "ideas", "decisions", "conversations"):
        rows = (_db().table(t)
            .delete()
            .eq("profile", profile).eq("project", project)
            .execute().data) or []
        counts[t] = len(rows)
    return counts

# ── Usage records ──────────────────────────────────────────────────────────────

def insert_usage(profile: str, project: str, agent: str, model: str,
                 input_tokens: int, output_tokens: int, cost_usd: float) -> None:
    resp = _db().table("usage_records").insert({
        "profile": profile, "project": project,
        "agent": agent, "model": model,
        "input_tokens": input_tokens, "output_tokens": output_tokens,
        "cache_write_tokens": 0, "cache_read_tokens": 0,
        "cost_usd": cost_usd,
    }).execute()
    if not resp.data:
        raise RuntimeError(
            f"insert returned no rows — likely missing INSERT grant for anon role. "
            f"profile={profile!r} agent={agent!r} model={model!r}"
        )


def load_usage_records(profile: str) -> list[UsageRecord]:
    rows = (_db().table("usage_records")
        .select("id,profile,project,agent,model,input_tokens,output_tokens,cache_write_tokens,cache_read_tokens,cost_usd,created_at")
        .eq("profile", profile)
        .order("created_at", desc=True)
        .execute().data) or []
    return [UsageRecord(**r) for r in rows]


def get_project_last_activity(profile: str) -> dict[str, str]:
    """Most recent created_at per project, drawn from conversations + specs."""
    latest: dict[str, str] = {}
    for t in ("conversations", "specs"):
        rows = (_db().table(t)
            .select("project,created_at")
            .eq("profile", profile)
            .execute().data) or []
        for r in rows:
            proj = r.get("project")
            ts = r.get("created_at")
            if proj and ts and (proj not in latest or ts > latest[proj]):
                latest[proj] = ts
    return latest
