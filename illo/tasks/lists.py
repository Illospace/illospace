"""Load the team's lists and agents without knowledge of outside tools."""


def load_task_files(root, keys, config):
    from illo.workspace import AgentsFile, TaskList, WorkspaceError, _front, _model, _read, _yaml

    agents_path = root / "agents.yaml"
    agents = (_model(AgentsFile, _yaml(agents_path, _read(agents_path)), agents_path).agents
              if agents_path.exists() else [])
    if len({a.key for a in agents}) != len(agents):
        raise WorkspaceError(f"{agents_path}: key: duplicate agent")
    for agent in agents:
        if agent.owner not in keys:
            raise WorkspaceError(f"{agents_path}: owner: unknown or inactive person {agent.owner}")
    lists = [_model(TaskList, {**_front(path), "key": path.stem,
                              "path": str(path.relative_to(root))}, path)
             for path in sorted(root.glob("lists/*.md"))]
    if not (root / "lists").exists():
        lists = [TaskList(key="tasks", name="Tasks")]
    from illo.tasks.connectors import wake_types

    warnings = [f"{agents_path}: unknown wake type {a.wake.type}" for a in agents
                if a.wake.type not in wake_types]
    holders = {"person:" + key for key in keys} | {"agent:" + a.key for a in agents}
    from illo.tasks.checks import known

    for entry in lists:
        path = root / entry.path
        for ref in [*entry.holders, entry.default_holder, entry.reviewer]:
            if ref and ref not in holders:
                warnings.append(f"{path}: unknown holder {ref}")
        if entry.owner and entry.owner not in keys:
            raise WorkspaceError(f"{path}: owner: unknown or inactive person")
        if entry.slack_channel and entry.slack_channel.removeprefix("#") not in {v for c in config.slack.channels for v in (c.name, c.id)}:
            warnings.append(f"{path}: channel {entry.slack_channel} is not listed. Illo checks it in Slack at run time.")
        if isinstance(entry.done, list):
            if not entry.done:
                raise WorkspaceError(f"{path}: done: give at least one check")
            for name in entry.done:
                if not known(name):
                    warnings.append(f"{path}: unknown check {name}")
    return agents, lists, warnings
