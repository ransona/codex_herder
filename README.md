# codex_herder

`codex_herder` is a minimal desktop app for filesystem-first orchestration of Codex-driven data analysis work.

The app is built around one repo containing:

- projects
- analyses within projects
- iterations within analyses
- stable Codex session ids linked to analyses

It does not require a database. The GUI is a thin layer over a repo layout that remains understandable from the filesystem alone.

## Features

- Browse `Project -> Analysis -> Iteration` in a left hierarchy pane
- Link one analysis to multiple Codex sessions
- Reuse an existing session or launch a new one
- Launch new sessions with a bootstrap message that describes the repo layout and Lab Data Access expectations
- Resume the selected session inside a dedicated `CLI` tab
- Choose between an ANSI-rendered terminal view and a raw stream view for Codex output
- Inspect Codex launch diagnostics and the exact command used to start the CLI
- View figures directly from `output/figures`
- Browse processed data, stats outputs, and code files
- Edit notes and YAML metadata in dynamic right-hand inspector tabs
- Ask Codex to create new iterations instead of having the GUI create them directly
- Delete analyses and iterations from the GUI

## Repo Layout

The default workspace root is `workspace/projects`.

```text
workspace/
  projects/
    project_001/
      project.yaml
      notes.md
      analyses/
        analysis_001/
          analysis.yaml
          notes.md
          iterations/
            iter_001/
              iteration.yaml
              task.md
              notes.md
              code/
              output/
                figures/
                processed_data/
                stats/
              logs/
```

Raw data should stay outside iteration folders. Iterations are for code, derived data, figures, stats outputs, notes, and logs.

## Session Model

Session ids are app-owned stable identifiers, for example:

- `proj_001_supervisor`
- `proj_001_analysis_001_main`
- `proj_001_analysis_001_alt_01`

When the app launches a new Codex session it:

1. Generates the stable session id
2. Persists it in analysis metadata
3. Starts the Codex CLI in a PTY-backed terminal tab using:

   `codex --no-alt-screen -C /home/adamranson/code/codex_herder`

4. Sends a bootstrap message that includes a `/rename <session_id>` command and the current project/analysis/iteration context

This avoids depending on informal manual naming.

## Install

```bash
cd /home/adamranson/code/codex_herder
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
```

## Run

```bash
cd /home/adamranson/code/codex_herder
. .venv/bin/activate
codex-herder
```

## Tests

```bash
cd /home/adamranson/code/codex_herder
. .venv/bin/activate
pytest
```

## Notes

- The app writes only inside this repository by default.
- The Codex executable can be overridden with `CODEX_HERDER_CODEX_BIN`.
- The app verifies the real Codex CLI path on startup and shows whether PTY launch works.
- Tests use a repo-local fake Codex CLI for deterministic session-flow verification, while the app also checks the real installed `codex` binary locally.
