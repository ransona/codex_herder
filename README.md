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
- Copy a complete startup prompt for a ChatGPT GUI Codex thread
- Keep optional CLI session support for existing projects
- View figures directly from `output/figures`
- Browse processed data, stats outputs, and code files
- Edit notes in the GUI and inspect iteration files
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

## Basic Workflow

1. Create or select a project.
2. Create an analysis and choose the experiment groups that belong to it.
3. Create or select an iteration. Each iteration is an isolated working area for one analysis step.
4. Open the `Codex` tab and copy the generated startup prompt.
5. In the ChatGPT GUI, start a Codex task on the remote host using the exact `ITERATION FOLDER` shown by Codex Herder, then paste the prompt.
6. Codex reads the broader filesystem as needed but should write only inside that iteration. At startup it waits for the task instructions rather than making suggestions.
7. Store reusable intermediate results in specifically named folders under `output/processed_data/`. Each folder must contain its own `description.md` documenting the exact inputs, commands, code version, environment, parameters, transformations, schema, and generation time.
8. Store figures and videos with same-base-name Markdown sidecars. For example:

   ```text
   output/
     figures/
       response_curve.png
       response_curve.md
     videos/
       activity_movie.mp4
       activity_movie.md
     processed_data/
       average_responses_by_condition/
         responses.csv
         description.md
   ```

   The figure and video descriptions begin with a brief summary and then provide detailed reproduction information. Update them whenever the associated output changes.
9. Use the `Processed Data` tab to create, inspect, rename, delete, copy, and paste named processed-data sets between iterations. Paste operations show transfer progress and keep the current tab selected.
10. Use `Overview` → `Validate Iteration` before treating an iteration as complete. Validation checks the required layout and reports missing processed-data, figure, or video descriptions.

By default, analysis code should use only the current iteration's processed data. If data from another iteration, analysis, or project is explicitly needed, record its source in the current dataset's `description.md`.

## Session Model

Session ids are app-owned stable identifiers, for example:

- `proj_001_supervisor`
- `proj_001_analysis_001_main`
- `proj_001_analysis_001_alt_01`

When the app launches a new Codex session, or generates a GUI startup prompt, it:

1. Generates the stable session id
2. Persists it in analysis metadata
3. Uses the selected iteration as the working directory
4. Includes the current project/analysis/iteration context, experiment groups, output rules, and reproducibility requirements

This avoids depending on informal manual naming.

## Install

### Conda

Build the project environment from the repository definition:

```bash
cd /home/adamranson/code/codex_herder
conda env create -f environment.yml
conda activate codex_herder
```

If the environment already exists, update it after changing dependencies with:

```bash
conda env update -f environment.yml --prune
```

### Python virtual environment

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

## Codex Credit Usage Tracker

The separate `codex-usage` program samples the currently logged-in Codex
account through `codex app-server` once per minute and stores samples in the
SQLite database `.codex_herder/codex_usage.sqlite3`. The GUI shows remaining
capacity for the 5-hour and 7-day windows as pie charts, plus usage line charts
for the past 5 hours, past 24 hours, and past 7 days.

```bash
conda activate codex_herder
codex-usage
```

To take one sample without opening the GUI:

```bash
codex-usage --once
```

### Run the sampler in the background

On Linux, the repository includes a systemd user service and timer. The timer
runs the one-shot sampler once per minute, so the GUI does not need to remain
open. The included unit files target this machine's paths
(`/home/adamranson/code/codex_herder` and `/home/adamranson/miniconda3`); edit
`systemd/codex-usage.service` before copying it if your paths differ.

Install and start it after activating the `codex_herder` environment at least
once:

```bash
cd /home/adamranson/code/codex_herder
conda activate codex_herder
python -m pip install -e '.[dev]'
mkdir -p ~/.config/systemd/user
cp systemd/codex-usage.service systemd/codex-usage.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now codex-usage.timer
```

Check that it is active and inspect sampler logs with:

```bash
systemctl --user status codex-usage.timer
systemctl --user list-timers codex-usage.timer
journalctl --user -u codex-usage.service -f
```

The service uses the existing Codex CLI login and writes to the same
`.codex_herder/codex_usage.sqlite3` database. Verify the login if sampling
fails:

```bash
codex login status
```

To stop and remove the background sampler:

```bash
systemctl --user disable --now codex-usage.timer
rm ~/.config/systemd/user/codex-usage.service ~/.config/systemd/user/codex-usage.timer
systemctl --user daemon-reload
```

The timer does not backfill samples missed while the computer was off. It
resumes on the next scheduled minute after startup. User timers normally run
while the user session is active; to keep sampling when logged out, enable
lingering for your account (if permitted):

```bash
loginctl enable-linger "$USER"
```

The tracker requires an active Codex CLI login (`codex login status`). It
records the account email, plan, primary and secondary rate-limit windows,
reset times, and available credit balance; it does not store authentication
tokens.

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
