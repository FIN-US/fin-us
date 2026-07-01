# Fin-Us

Fin-Us is a local-first investment assistant that coordinates market data, account data, agents, and user-facing command surfaces for analysis and trading support.

## Language

**Local Operator**:
A person who runs Fin-Us on their own machine for demo, study, or personal operation without needing to edit project configuration files directly.
_Avoid_: Developer, end user, administrator

**Initial Setup Flow**:
A guided first-run conversation that collects the values needed for a **Local Operator** to start Fin-Us locally.
_Avoid_: Installer, configuration editor, startup script

## Relationships

- A **Local Operator** provides setup values before Fin-Us starts.
- The **Initial Setup Flow** prepares local configuration for a **Local Operator**.

## Example dialogue

> **Dev:** "Should the **Local Operator** edit `.env` before the first run?"
> **Domain expert:** "No. They should use the **Initial Setup Flow** and let the system prepare the local configuration."

## Flagged ambiguities

- "사용자" was used broadly; resolved: the startup setup flow targets the **Local Operator**, not project maintainers who prefer direct file editing.
