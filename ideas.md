# Ideas

- **Sequence diagram for mode lifecycle**: Add a sequence diagram showing a full user flow through /plan → /code → /test to complement the class and flowchart diagrams. The file write was rejected earlier; worth revisiting inline or in a different path.

- **Wiring /visualise as a first-class REPL mode**: The `visualise.md` agent profile exists but there is no explicit mode handler in the REPL mode switcher. Consider registering it so `/visualise` can be invoked like /plan, /code, etc.

- **Component diagram for tool registry**: Extend the architecture doc with a diagram mapping the four tool families (core file ops, search, web, git) and their relationship to the tool dispatcher in `repl.py`.
