# Nyx Project Architecture

```mermaid
classDiagram
    class CLI {
        +typer.App app
        +commands: repl, cost
        +entry point
    }

    class REPL {
        +Session management
        +Mode switching
        +Tool execution
        +LLM integration
    }

    class Session {
        +mode: chat|plan|code|test|validate-plan|visualise|code-validator
        +model: LLM
        +history: message history
        +auto_approve: flag
    }

    class Mode {
        +chat: default interaction
        +plan: produce specs
        +code: implement features
        +test: write tests
        +validate-plan: review specs
        +visualise: create diagrams
        +code-validator: validate code
    }

    class Tools {
        +Core tools: file operations, search, etc.
        +Read-only tools: plan/validate modes
        +Full tools: code/test modes
    }

    class AgentFiles {
        +nyx.md: Base agent config
        +Plan mode spec
        +Code mode spec
        +Test mode spec
        +Visualise mode spec
    }

    class LLM {
        +Model selection
        +Tier management
        +Side model for cheap tasks
    }

    CLI --> REPL : launches
    REPL --> Session : manages
    Session --> Mode : switches between
    Mode --> AgentFiles : loads configuration
    REPL --> Tools : executes
    REPL --> LLM : uses for responses
```

The architecture shows:
- CLI entry point that launches the REPL
- REPL manages sessions with different modes
- Session maintains state including mode, model, and history
- Modes define behavior specialization
- Tools are categorized based on mode permissions
- LLM handles the AI responses
- Agent files configure each mode's behavior