---
name: codex-herder-analysis-context
description: Use when working inside the Codex Herder analysis workspace. Applies the filesystem-first project/analysis/iteration conventions, required iteration layout, output placement rules, and Lab Data Access raw-data policy for Codex-driven analysis work.
---

# Codex Herder Analysis Context

Use this skill when the user is working inside the Codex Herder repo/workspace and provides a project root, project name, analysis name, and iteration id.

## Working model

The workspace is organized as:

`projects/<project_id>/analyses/<analysis_id>/iterations/<iteration_id>/`

Each project contains analyses. Each analysis contains iterations. Treat the current iteration as the main working unit unless explicitly told otherwise.

Projects may also define experiment groups in project metadata. Analyses may include one or more of those experiment groups. An experiment group is a named list of experiment references, where each reference contains:

- `expID`
- optional `userID`

When experiment groups are included in an analysis, treat them as the declared experiment sets available to that analysis.

## Required iteration layout

Every iteration must contain:

- `code/`
- `output/figures/`
- `output/videos/`
- `output/processed_data/`
- `output/stats/`
- `logs/`
- `task.md`
- `notes.md`
- `iteration.yaml`

The iteration folder is the only location where Codex may write. Codex may read elsewhere as needed.

If any required iteration directories or files are missing, create them before doing substantive work.

## File placement rules

- Put code, scripts, notebooks, configs, and helper files in `code/`.
- Put viewable figures in `output/figures/`.
- Every generated figure must have a sidecar Markdown file with the same base name in the same folder (for example, `response_curve.png` and `response_curve.md`).
- Each figure sidecar must begin with a brief summary of what is plotted, followed by a detailed reproducibility description covering source data, processing steps, code or commands, parameters, units, environment, code version, and generation date and time.
- If a figure is changed or regenerated, update its sidecar with a dated explanation of what changed and why.
- Organize figures hierarchically inside `output/figures/` when related outputs belong together.
- Use clear subfolder names so the figure folder structure reflects logical groups of related outputs within the iteration.
- When making multiple iterative small changes to an existing figure, keep the same base figure name and append a version suffix of the form `Vx`, where `x` is the version number.
- Before creating a new versioned figure file, ask the user whether they want a new version.
- If the user does not want a new version, assume the latest version should be overwritten rather than creating an additional file.
- Put viewable videos in `output/videos/`.
- Every generated video or video dataset must have a sidecar Markdown file with the same base name in the same folder (for example, `activity_movie.mp4` and `activity_movie.md`).
- Each video sidecar must begin with a brief summary of the content, followed by a detailed reproducibility description covering source data, processing steps, code or commands, parameters, units, frame rate, dimensions, environment, code version, and generation date and time.
- If a video or video dataset is changed or regenerated, update its sidecar with a dated explanation of what changed and why.
- Organize videos hierarchically inside `output/videos/` using the same grouping conventions as figures when related outputs belong together.
- Use clear subfolder names so the video folder structure reflects logical groups of related outputs within the iteration.
- Save videos as both a `.npy` file and a matching `.mp4` file with the same base name.
- Treat the `.npy` file as the primary GUI preview representation and the `.mp4` file as the portable playback/export version.
- Store array-based video representations such as montage movies, aligned movie stacks intended for direct playback, or GUI-preview movie tensors in `output/videos/`, not `output/processed_data/`.
- When making multiple iterative small changes to an existing video, keep the same base video name and append a version suffix of the form `Vx`, where `x` is the version number.
- Before creating a new versioned video file, ask the user whether they want a new version.
- If the user does not want a new version, assume the latest version should be overwritten rather than creating an additional file.
- Default video output format should be `mp4`.
- Put derived intermediate data in `output/processed_data/`.
- Give reusable intermediate data specific, descriptive names that identify the processing stage or data type (for example, `average_responses_by_condition`).
- Store each named processed-data result in its own subfolder under `output/processed_data/`.
- Each specific processed-data subfolder must contain its own `description.md` file alongside that subfolder's data files. A general description at the `output/processed_data/` root does not satisfy this requirement.
- The `description.md` inside each specific processed-data subfolder must contain all information required to reproduce the files in that subfolder exactly. Include the source project, analysis, and iteration; source experiment IDs and input file paths; processing scripts, notebooks, functions, or complete commands; code version or Git commit; environment and package versions; all parameters, filters, exclusions, units, and random seeds; transformations in execution order; output filenames and schema; and the date and time of generation.
- If any file in a specific processed-data subfolder is changed, update that same subfolder's `description.md` with the date and time and a clear explanation of what changed and why.
- Keep a dated change history in each subfolder's `description.md`; do not silently overwrite the processing description.
- Put statistical outputs, summaries, reports, and result tables in `output/stats/`.
- Put run logs and execution history in `logs/`.
- Update `notes.md` as work progresses.
- Keep metadata aligned with the filesystem layout.
- Code that reads or writes processed data must accept `project_id`, `analysis_id`, and `iteration_id` as parameters and resolve the iteration path from them.
- Do not hardcode absolute paths or embed a specific project, analysis, or iteration name in reusable analysis code.
- Keep processed-data references relative to the resolved iteration folder so code and processed data can be moved between analyses and iterations.
- Do not use processed data from another iteration, analysis, or project by default; use only the current iteration's `output/processed_data/` to avoid mixing or confusing datasets.
- Use processed data from elsewhere only when explicitly requested, and record its source project, analysis, and iteration in the current dataset's `description.md`.

## Experiment groups

Experiment groups are project-level metadata. They are used to declare which experiments belong together for reuse across analyses.

Expected metadata model:

- project metadata may contain named experiment groups
- each group contains one or more experiment references
- each reference has an `expID` and may also have a `userID`
- analysis metadata may contain a list of included experiment-group names

When working inside an analysis iteration:

- inspect analysis metadata to determine which experiment groups are included
- inspect project metadata to resolve those group names into concrete `expID` / `userID` entries
- treat only the included experiment groups as the default experiment sets for that analysis unless told otherwise
- if the user is already working with a supplied dataset or a clearly specified local analysis input, work with that supplied dataset and do not go searching for experiments on your own
- `Lab Data Access` may be used freely to explore and inspect data for the explicitly indicated `expID`s
- do not use experiment groups or `Lab Data Access` to look for additional experiments beyond the explicitly indicated `expID`s unless the user explicitly asks for that
- if the user asks for analysis without specifying which included groups to use, ask which included groups should be analyzed
- if exactly one included group is present and the user request is otherwise clear, you may infer that group, but state that inference

Do not invent experiment groups or silently substitute experiments from groups that are not included in the current analysis.

## Raw data policy

- Do not copy raw data into iteration folders.
- Raw data stays outside the analysis workspace.
- Use `Lab Data Access` to determine where raw data lives, how it is formatted, and how to access it correctly.
- Never use raw `FrameEvents.csv` files for stimulus alignment unless the user explicitly asks for that source. Prefer the processed Timeline-time trial onsets and saved microscope frame-time arrays; if those are unavailable, report the limitation rather than substituting raw `FrameEvents.csv` timing.
- Only write derived or intermediate data into `output/processed_data/`.

When experiments are specified through experiment groups, use the `expID` / `userID` values from those included groups together with `Lab Data Access` to locate and inspect the corresponding raw data.
If the user has already supplied the dataset or working files for the task, prefer those and do not go looking for additional experiments unless explicitly instructed.

## Operating rules

- Keep work contained to the specified iteration unless explicitly told otherwise.
- By default, do not inspect code, outputs, notes, or data from other analyses in the project.
- Treat the current analysis as the boundary for normal work unless the user explicitly asks for cross-analysis comparison or reuse.
- Prefer understandable, filesystem-first organization over hidden state.
- At startup, do not make suggestions. Inspect the existing iteration state only as needed, then wait for instructions.
- Do not modify `task.md`, `notes.md`, `iteration.yaml`, code, or outputs unless explicitly instructed.
- Do not improve or rewrite placeholder files on your own.
- When reporting progress or completed work, use brief functional summaries focused on behavior and outputs rather than narrating code modifications line by line.
- Rename the thread to the analysis name when starting a session.
- If the iteration is newly created, initialize it cleanly and then proceed with the task.
- On startup, confirm which experiment groups are included in the current analysis if that information is available in metadata or the launch context.
