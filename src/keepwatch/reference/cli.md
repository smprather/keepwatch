# Command-line reference

Global options come before the command: `keepwatch [--config PATH] COMMAND ...`. Every command accepts `-h`/`--help`. Exit status: 0 for success, 1 for problems found or a failed poll, 2 for wrong usage. When stdout is not a terminal, output is plain text with no colour or box drawing; `validate`, `poll`, `status` and `logs` also accept `--json`.
